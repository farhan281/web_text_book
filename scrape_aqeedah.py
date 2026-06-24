#!/usr/bin/env python3
"""Scrape the Aqeedah Courses static library into lossless UTF-8 JSON files.

The site is a hash-routed SPA, but its canonical content is published as JSON.
This scraper discovers the live catalog and downloads those JSON datasets rather
than scraping rendered HTML. Every volume/site entry becomes one JSON file containing clean lesson text.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import sys
import threading
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin
from urllib.request import Request, urlopen


DEFAULT_BASE = "https://aqeedah-courses.pages.dev/"
CORE_DATASETS = (
    "course.json",
    "modules.json",
    "lessons/index.json",
    "source_chunks.json",
)
AUX_DATASETS = (
    "glossary.json",
    "clean_glossary.json",
    "search_index.json",
    "quiz_bank.json",
    "review_queue.json",
    "audit.json",
    "infographics.json",
    "activities.json",
    "concept_edges.json",
)
_print_lock = threading.Lock()


@dataclass(frozen=True)
class CatalogEntry:
    number: int
    category_id: str
    category_name: str
    path: str
    folder: str
    kitab: str
    expected_lessons: int | None
    expected_modules: int | None
    display_metadata: dict[str, str]

    @property
    def app_url(self) -> str:
        raise AttributeError("app_url depends on the selected base URL")


class ScrapeError(RuntimeError):
    pass


def log(message: str) -> None:
    with _print_lock:
        print(message, flush=True)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def safe_name(value: str, limit: int = 150) -> str:
    value = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "_", value)
    value = re.sub(r"\s+", " ", value).strip(" ._") or "untitled"
    if len(value) <= limit:
        return value
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]
    return value[: limit - 10].rstrip() + "__" + digest


def extract_balanced(text: str, start: int, opener: str, closer: str) -> str:
    """Extract a JS bracketed expression while respecting quoted strings."""
    depth = 0
    quote_char = ""
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if quote_char:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote_char:
                quote_char = ""
            continue
        if char in "'\"`":
            quote_char = char
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise ScrapeError(f"Unbalanced {opener}{closer} expression in homepage")


def js_properties(object_text: str) -> dict[str, str]:
    """Read flat string/integer properties from a catalog object literal."""
    values: dict[str, str] = {}
    pattern = re.compile(
        r"(?:^|[{,\s])([A-Za-z_]\w*)\s*:\s*(?:'((?:\\.|[^'\\])*)'|\"((?:\\.|[^\"\\])*)\"|(-?\d+))"
    )
    for match in pattern.finditer(object_text):
        key = match.group(1)
        raw = next((x for x in match.groups()[1:] if x is not None), "")
        values[key] = raw.replace("\\'", "'").replace('\\"', '"').replace("\\\\", "\\")
    return values


def parse_display_metadata(homepage: str) -> dict[str, dict[str, str]]:
    marker = homepage.find("const BOOK_META")
    if marker < 0:
        return {}
    start = homepage.find("{", marker)
    block = extract_balanced(homepage, start, "{", "}")
    result: dict[str, dict[str, str]] = {}
    entry_re = re.compile(r'"((?:\\.|[^"\\])+)"\s*:\s*\{([^{}]*)\}', re.S)
    for match in entry_re.finditer(block):
        result[match.group(1)] = js_properties(match.group(2))
    return result


def parse_catalog(homepage: str) -> list[CatalogEntry]:
    marker = homepage.find("const PORTAL_DATA")
    if marker < 0:
        raise ScrapeError("Could not find PORTAL_DATA in homepage")
    start = homepage.find("[", marker)
    block = extract_balanced(homepage, start, "[", "]")
    display_meta = parse_display_metadata(homepage)

    categories: list[tuple[int, str, str]] = []
    for match in re.finditer(r"\bid:'([^']+)',name:'([^']+)'", block):
        categories.append((match.start(), match.group(1), match.group(2)))
    if not categories:
        raise ScrapeError("Could not parse catalog categories")

    entries: list[CatalogEntry] = []
    # Catalog volume/standalone records are flat objects even though their parent
    # arrays are nested, so this intentionally ignores objects with nested braces.
    for match in re.finditer(r"\{[^{}]*\bpath:'[^']+/static_app/'[^{}]*\}", block):
        props = js_properties(match.group(0))
        if "path" not in props or "folder" not in props:
            continue
        category = categories[0]
        for candidate in categories:
            if candidate[0] <= match.start():
                category = candidate
            else:
                break
        entries.append(
            CatalogEntry(
                number=len(entries) + 1,
                category_id=category[1],
                category_name=category[2],
                path=props["path"],
                folder=props["folder"],
                kitab=props.get("kitab", props["folder"]),
                expected_lessons=int(props["lessons"]) if props.get("lessons", "").isdigit() else None,
                expected_modules=int(props["modules"]) if props.get("modules", "").isdigit() else None,
                display_metadata=display_meta.get(props["folder"], {}),
            )
        )
    if not entries:
        raise ScrapeError("No books/volumes found in PORTAL_DATA")
    return entries


class Downloader:
    def __init__(self, timeout: float, retries: int, delay: float, user_agent: str):
        self.timeout = timeout
        self.retries = retries
        self.delay = delay
        self.user_agent = user_agent

    def bytes(self, url: str) -> bytes:
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                request = Request(url, headers={"User-Agent": self.user_agent, "Accept": "application/json,text/html;q=0.9,*/*;q=0.1"})
                with urlopen(request, timeout=self.timeout) as response:
                    return response.read()
            except (HTTPError, URLError, TimeoutError, OSError) as exc:
                last_error = exc
                if attempt >= self.retries or isinstance(exc, HTTPError) and 400 <= exc.code < 500 and exc.code != 429:
                    break
                time.sleep(self.delay * (2**attempt))
        raise ScrapeError(f"Download failed after {self.retries + 1} attempts: {url}: {last_error}")

    def text(self, url: str) -> str:
        return self.bytes(url).decode("utf-8-sig")

    def json(self, url: str) -> Any:
        raw = self.bytes(url)
        try:
            return json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            preview = raw[:100].decode("utf-8", errors="replace").replace("\n", " ")
            raise ScrapeError(f"Expected JSON at {url}, received {preview!r}: {exc}") from exc


def module_filename(module_id: Any) -> str:
    # Mirrors app.js: String(...).replace(/[^\w\-]/g, '_'). JavaScript \w is ASCII.
    normalized = re.sub(r"[^A-Za-z0-9_-]", "_", str(module_id or "unassigned"))
    return f"lessons/mod_{quote(normalized, safe='')}.json"


def json_dump(value: Any, *, pretty: bool = True) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2 if pretty else None, sort_keys=False)


def clean_txt(val: Any) -> str:
    if val is None:
        return ""
    text = str(val).strip()
    return re.sub(r"<[^>]+>", "", text)


def page_label(item: dict[str, Any]) -> str:
    start, end = item.get("page_start"), item.get("page_end")
    if start is None and end is None:
        return ""
    if end is None or start == end:
        return f"p. {start}"
    return f"pp. {start}-{end}"


def format_lesson_txt(
    module_number: int,
    lesson_number: int,
    lesson: dict[str, Any],
    source: dict[str, Any] | None,
) -> str:
    """Format one lesson as a plain text string that exactly replicates the visual layout of the lesson web page."""
    page = lesson.get("page_start", "")
    pages_line = f"p. {page}" if page else ""
    module_title = clean_txt(lesson.get("module_title") or "")
    
    lines = [
        "← Prev",
        pages_line,
        f"Module {module_number} · Lesson {lesson_number}",
        "Next →",
        module_title,
        clean_txt(lesson.get("difficulty") or ""),
        f"{lesson.get('estimated_minutes') or 0} min",
        clean_txt(lesson.get("title") or "Untitled Lesson")
    ]
    
    orientation = clean_txt(lesson.get("passage_orientation") or "")
    if orientation:
        lines.append(orientation)
    
    lines.append("") # empty line after header info
    
    objective = clean_txt(lesson.get("learning_objective") or "")
    if objective:
        lines.extend([
            "Learning Objective",
            objective,
            ""
        ])
        
    core_idea = clean_txt(lesson.get("core_idea") or "")
    if core_idea:
        lines.extend([
            "Core Idea",
            core_idea,
            ""
        ])
        
    short_intro = clean_txt(lesson.get("short_intro") or "")
    if short_intro:
        lines.extend([
            short_intro,
            ""
        ])
        
    guided_path = lesson.get("guided_path")
    if isinstance(guided_path, list) and guided_path:
        lines.append("Guided Path")
        for i, step in enumerate(guided_path, 1):
            if not isinstance(step, dict):
                lines.append(str(i))
                lines.append(clean_txt(step))
            else:
                lines.append(str(i))
                title = step.get("step") or step.get("step_title") or "Step"
                lines.append(clean_txt(title))
                explanation = step.get("explanation") or step.get("step_text") or ""
                if explanation:
                    lines.append(clean_txt(explanation))
                note = step.get("note") or ""
                if note:
                    lines.append(clean_txt(note))
        lines.append("") # empty line after guided path
        
    if source:
        lines.extend([
            "Source Text",
            "⚠ Extracted via Advanced OCR ∼97% accuracy, minor errors may remain — always verify against original Arabic books"
        ])
        arabic = clean_txt(source.get("arabic_clean") or "")
        if arabic:
            lines.append(arabic)
        translation = clean_txt(source.get("translation_literal") or "")
        if translation:
            lines.append(translation)
        
        src_page = source.get("page_start") or page
        lines.extend([
            f"Source pages p. {src_page}",
            ""
        ])
        
        commentary = clean_txt(source.get("commentary_simple") or "")
        if commentary:
            lines.extend([
                "Commentary",
                commentary,
                ""
            ])
            
    key_terms = lesson.get("key_terms")
    if isinstance(key_terms, list) and key_terms:
        lines.append("Key Terms")
        for term in key_terms:
            lines.append(clean_txt(term))
        lines.append("") # empty line after key terms
        
    lines.extend([
        "Key Terms from Source Passage",
        ""
    ])
    
    misconception = lesson.get("misconception")
    if isinstance(misconception, dict):
        wrong = misconception.get("wrong_belief") or misconception.get("misconception")
        correction = misconception.get("correction")
        if wrong or correction:
            lines.append("Misconception & Correction")
            if wrong:
                lines.append(f"Misconception: {clean_txt(wrong)}")
            if correction:
                lines.append(f"Correction: {clean_txt(correction)}")
            lines.append("")
    elif lesson.get("common_misunderstanding"):
        lines.extend([
            "Misconception & Correction",
            f"Misconception: {clean_txt(lesson['common_misunderstanding'])}",
            ""
        ])
        
    check = lesson.get("quick_check")
    if isinstance(check, dict) and check:
        lines.append("Checkpoint")
        question = clean_txt(check.get("question") or "")
        if question:
            lines.append(question)
        choices = check.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                lines.append(clean_txt(choice))
        lines.append("")
        
    takeaway = clean_txt(lesson.get("key_takeaway") or "")
    if takeaway:
        lines.extend([
            "Key Takeaway",
            takeaway,
            ""
        ])
        
    reflections = lesson.get("reflect_questions")
    if isinstance(reflections, list) and reflections:
        lines.append("How This Lesson Applies — to MY Life?")
        for ref in reflections:
            lines.extend([
                "?",
                clean_txt(ref)
            ])
        lines.append("")
        
    lines.extend([
        f"Pages p. {page} · {module_title}" if page else f"Pages · {module_title}",
        "← Prev",
        "Mark Done",
        "Next →"
    ])
    
    cleaned_lines = []
    prev_blank = False
    for line in lines:
        is_blank = (line.strip() == "")
        if is_blank:
            if not prev_blank:
                cleaned_lines.append("")
                prev_blank = True
        else:
            cleaned_lines.append(line)
            prev_blank = False
            
    return "\n".join(cleaned_lines)


def get_cached_json(
    downloader: Downloader,
    url: str,
    cache_path: Path,
    use_cache: bool,
) -> Any:
    if use_cache and cache_path.is_file():
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    value = downloader.json(url)
    if use_cache:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache_path.with_suffix(cache_path.suffix + ".part")
        temporary.write_text(json_dump(value, pretty=False), encoding="utf-8")
        os.replace(temporary, cache_path)
    return value


def validate_list(name: str, value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise ScrapeError(f"{name} must be a JSON array, got {type(value).__name__}")
    return value


def scrape_entry(
    entry: CatalogEntry,
    args: argparse.Namespace,
    downloader: Downloader,
    output_root: Path,
    cache_root: Path,
) -> dict[str, Any]:
    prefix = f"[{entry.number:02d}] {entry.folder}"
    category_dir = output_root / safe_name(entry.category_id)
    output_file = category_dir / f"{entry.number:02d} - {safe_name(entry.folder)}.txt"
    sidecar = output_file.with_suffix(".manifest.json")
    if args.resume and output_file.is_file() and sidecar.is_file():
        try:
            old = json.loads(sidecar.read_text(encoding="utf-8"))
            if old.get("status") == "complete" and old.get("output_sha256"):
                log(f"{prefix}: already complete; skipped")
                return old
        except (OSError, json.JSONDecodeError):
            pass

    log(f"{prefix}: downloading")
    app_url = urljoin(args.base_url, quote(entry.path, safe="/"))
    data_url = urljoin(app_url, "data/")
    volume_cache = cache_root / f"{entry.number:02d}_{safe_name(entry.folder)}"

    datasets: dict[str, Any] = {}
    wanted = CORE_DATASETS + (() if args.no_aux else AUX_DATASETS)
    for relative in wanted:
        datasets[relative] = get_cached_json(
            downloader,
            urljoin(data_url, relative),
            volume_cache / relative,
            not args.no_cache,
        )

    course = datasets["course.json"]
    modules = validate_list("modules.json", datasets["modules.json"])
    lesson_index = validate_list("lessons/index.json", datasets["lessons/index.json"])
    sources = validate_list("source_chunks.json", datasets["source_chunks.json"])
    if not isinstance(course, dict):
        raise ScrapeError("course.json must be a JSON object")

    source_map = {item.get("source_chunk_id"): item for item in sources if isinstance(item, dict)}
    index_ids = [item.get("teaching_lesson_id") for item in lesson_index if isinstance(item, dict)]
    full_lessons: dict[str, dict[str, Any]] = {}
    module_payloads: dict[str, list[Any]] = {}
    for module in modules:
        module_id = module.get("module_id", "unassigned") if isinstance(module, dict) else "unassigned"
        relative = module_filename(module_id)
        payload = validate_list(
            relative,
            get_cached_json(downloader, urljoin(data_url, relative), volume_cache / relative, not args.no_cache),
        )
        module_payloads[str(module_id)] = payload
        for lesson in payload:
            if isinstance(lesson, dict) and lesson.get("teaching_lesson_id"):
                full_lessons[str(lesson["teaching_lesson_id"])] = lesson

    missing_full = [lesson_id for lesson_id in index_ids if lesson_id and lesson_id not in full_lessons]
    extra_full = [lesson_id for lesson_id in full_lessons if lesson_id not in set(index_ids)]
    checks = {
        "catalog_expected_lessons": entry.expected_lessons,
        "course_reported_lessons": course.get("lesson_count"),
        "index_lessons": len(lesson_index),
        "full_lessons": len(full_lessons),
        "missing_full_lesson_ids": missing_full,
        "extra_full_lesson_ids": extra_full,
        "catalog_expected_modules": entry.expected_modules,
        "course_reported_modules": course.get("module_count"),
        "modules": len(modules),
        "course_reported_sources": course.get("source_chunk_count"),
        "sources": len(sources),
    }
    expected_counts = [x for x in (entry.expected_lessons, course.get("lesson_count")) if isinstance(x, int)]
    if any(count != len(lesson_index) for count in expected_counts) or missing_full:
        raise ScrapeError(f"Lesson validation failed: {json_dump(checks, pretty=False)}")
    expected_modules = [x for x in (entry.expected_modules, course.get("module_count")) if isinstance(x, int)]
    if any(count != len(modules) for count in expected_modules):
        raise ScrapeError(f"Module validation failed: {json_dump(checks, pretty=False)}")

    book_text_parts = []
    for module_number, module in enumerate(modules, 1):
        module_id = str(module.get("module_id", "unassigned")) if isinstance(module, dict) else "unassigned"
        module_lessons = module_payloads.get(module_id, [])
        for lesson_number, lesson in enumerate(module_lessons, 1):
            if not isinstance(lesson, dict):
                continue
            lesson_id = lesson.get("teaching_lesson_id", "")
            source_id = lesson.get("source_chunk_id") if isinstance(lesson, dict) else None
            source = None
            if source_id:
                source = source_map.get(source_id)
                if source is None:
                    raise ScrapeError(f"Lesson {lesson_id} references missing source {source_id}")
            formatted_lesson = format_lesson_txt(module_number, lesson_number, lesson, source)
            book_text_parts.append(formatted_lesson)

    book_payload = "\n\n".join(book_text_parts) + "\n"

    category_dir.mkdir(parents=True, exist_ok=True)
    temporary = output_file.with_suffix(".txt.part")
    scraped_utc = utc_now()
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(book_payload)
    os.replace(temporary, output_file)

    digest = hashlib.sha256(output_file.read_bytes()).hexdigest()
    manifest = {
        "status": "complete",
        "scraped_utc": scraped_utc,
        "source_app_url": app_url,
        "output_file": str(output_file),
        "output_bytes": output_file.stat().st_size,
        "output_sha256": digest,
        "checks": checks,
    }
    sidecar.write_text(json_dump(manifest) + "\n", encoding="utf-8")
    if args.no_cache:
        shutil.rmtree(volume_cache, ignore_errors=True)
    log(f"{prefix}: complete ({len(lesson_index):,} lessons, {output_file.stat().st_size / 1_048_576:.1f} MiB)")
    return manifest


def select_entries(entries: list[CatalogEntry], filters: Iterable[str]) -> list[CatalogEntry]:
    needles = [item.casefold() for item in filters]
    if not needles:
        return entries
    selected = [
        entry
        for entry in entries
        if any(
            needle in " ".join((entry.folder, entry.kitab, entry.category_id, entry.category_name)).casefold()
            for needle in needles
        )
    ]
    if not selected:
        raise ScrapeError(f"No catalog entries matched: {', '.join(filters)}")
    return selected


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--base-url", default=DEFAULT_BASE, help="Portal base URL")
    parser.add_argument("--output", type=Path, default=Path("scraped_books"), help="Output directory")
    parser.add_argument("--workers", type=int, default=3, help="Volumes downloaded concurrently")
    parser.add_argument("--timeout", type=float, default=90, help="Per-request timeout in seconds")
    parser.add_argument("--retries", type=int, default=4, help="Retries after the first attempt")
    parser.add_argument("--retry-delay", type=float, default=1.0, help="Initial exponential retry delay")
    parser.add_argument("--book", action="append", default=[], help="Case-insensitive volume/book filter; repeatable")
    parser.add_argument("--list", action="store_true", help="List discovered entries and exit")
    parser.add_argument("--no-aux", action="store_true", help="Omit glossary/search/quiz/audit and other auxiliary datasets")
    parser.add_argument("--no-cache", action="store_true", help="Do not keep resumable raw JSON cache")
    parser.add_argument("--no-resume", dest="resume", action="store_false", help="Rebuild completed JSON files")
    parser.set_defaults(resume=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.workers < 1:
        raise ScrapeError("--workers must be at least 1")
    args.base_url = args.base_url.rstrip("/") + "/"
    downloader = Downloader(args.timeout, args.retries, args.retry_delay, "aqeedah-json-exporter/1.0 (+personal archival use)")
    homepage = downloader.text(args.base_url)
    entries = parse_catalog(homepage)
    selected = select_entries(entries, args.book)

    catalog = {
        "source": args.base_url,
        "discovered_utc": utc_now(),
        "entry_count": len(entries),
        "entries": [asdict(entry) for entry in entries],
    }
    if args.list:
        for entry in selected:
            print(f"{entry.number:02d}\t{entry.category_id}\t{entry.folder}\t{entry.expected_lessons or '?'} lessons")
        return 0

    output_root = args.output.resolve()
    cache_root = output_root / ".cache"
    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "catalog.json").write_text(json_dump(catalog) + "\n", encoding="utf-8")
    log(f"Discovered {len(entries)} volumes/books; selected {len(selected)}. Output: {output_root}")

    successes: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(args.workers, len(selected))) as pool:
        future_map = {
            pool.submit(scrape_entry, entry, args, downloader, output_root, cache_root): entry
            for entry in selected
        }
        for future in concurrent.futures.as_completed(future_map):
            entry = future_map[future]
            try:
                successes.append(future.result())
            except Exception as exc:  # keep other independent volumes running
                failures.append({"folder": entry.folder, "error": str(exc)})
                log(f"[{entry.number:02d}] {entry.folder}: FAILED: {exc}")

    run_manifest = {
        "status": "complete" if not failures else "partial_failure",
        "finished_utc": utc_now(),
        "selected_count": len(selected),
        "success_count": len(successes),
        "failure_count": len(failures),
        "failures": failures,
    }
    (output_root / "run_manifest.json").write_text(json_dump(run_manifest) + "\n", encoding="utf-8")
    log(f"Finished: {len(successes)} complete, {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted; cached downloads and completed volumes can be resumed.", file=sys.stderr)
        raise SystemExit(130)
    except ScrapeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
