# Islamic Sciences Multi-Site Course Library Scraper

A robust, generalized, zero-dependency Python command-line utility to scrape and archive structured course databases from three popular hash-routed Single Page Applications (SPAs) into beautifully formatted plain text (`.txt`) files.

## Supported Websites
The scraper downloads canonical course content from:
1. **Aqeedah Courses**: `https://aqeedah-courses.pages.dev/` (outputs to `scraped_books/aqeedah/`)
2. **Hanafi Fiqh Courses**: `https://hanafi-fiqh-courses.pages.dev/` (outputs to `scraped_books/hanafi_fiqh/`)
3. **Mantiq Courses**: `https://mantiq-courses.pages.dev/` (outputs to `scraped_books/mantiq/`)

---

## Features

- **Direct API Extraction**: Instead of parsing complex and fragile rendered HTML, the scraper discovers the catalog structure on the homepage (`PORTAL_DATA` and `BOOK_META` blocks) and pulls the underlying canonical JSON data files directly.
- **Lossless Visual Layout Formatting**: Converts structured JSON data into plain text documents that replicate the exact visual layout of the browser-based course platform (navigation cues, difficulty levels, timings, learning objectives, guided paths, source texts/Arabic commentary, misconceptions, checkpoints, key takeaways, and reflection questions).
- **GitHub-Safe 50-Lesson Splitting**: Automatically splits long modules into parts of at most 50 lessons. This keeps file sizes strictly under 1MB, ensuring that they can be fully rendered and previewed on GitHub's web interface without "file too large" warning flags.
- **Premium Key Terms Formatting**: Parses nested dictionary glossary metadata fields (Arabic term, English translation, transliteration, definition, and shift notes) and prints them as clean, human-readable entries rather than raw string representations of Python objects.
- **Robust Multithreaded Downloader**: Uses a thread pool (`concurrent.futures`) to scrape multiple books concurrently, complete with exponential backoff retry logic, customizable timeout flags, and resilient connection handling.
- **SHA-256 Integrity Verification**: Generates a `.manifest.json` sidecar for every volume, calculating the total file size and the combined SHA-256 hash of all text documents (sorted alphabetically) to ensure file integrity.
- **Zero Third-Party Dependencies**: Written entirely in pure Python, utilizing only the Standard Library. No external libraries, selenium, or headless browsers are required.

---

## Project Structure

```text
web_text_book/
├── README.md               # Detailed usage guide and documentation
├── requirements.txt         # Project requirements and compatibility notes
├── scrape_courses.py        # The unified scraper executable
└── scraped_books/           # Generated output archives
    ├── aqeedah/             # Scraped Aqeedah courses
    ├── hanafi_fiqh/         # Scraped Hanafi jurisprudence courses
    └── mantiq/              # Scraped Logic/Philosophy courses
```

Each book folder inside `scraped_books/<site>/<category>/` contains:
- `Module 01 - [Name].txt`
- `Module 02 - [Name] - Part 1.txt` (split parts if lessons count > 50)
- `Module 02 - [Name] - Part 2.txt`
- `[FolderName].manifest.json` # JSON manifest with sizes, SHA-256 hash, and metadata checks

---

## Getting Started

### Prerequisites
- Python 3.8 or higher.
- Stable internet connection.

### Installation
No installation or pip requirements are necessary! You can check the `requirements.txt` file for compatibility details.

### How to Run

By default, running the script with no arguments scrapes all books from all three sites, skipping any books that were already successfully scraped:

```bash
python3 scrape_courses.py
```

### Advanced Usage Examples

```bash
# 1. List all available books for a specific site without downloading them
python3 scrape_courses.py --site mantiq --list

# 2. Scrape a specific site only (choices: "aqeedah", "hanafi_fiqh", "mantiq", or "all")
python3 scrape_courses.py --site hanafi_fiqh

# 3. Scrape a specific book by name (case-insensitive filter, repeatable)
python3 scrape_courses.py --site mantiq --book "Husn Muhajja"

# 4. Scrape with custom worker concurrency and disable local caching to save disk space
python3 scrape_courses.py --site all --workers 5 --no-cache

# 5. Disable resumption and force-rebuild all files
python3 scrape_courses.py --site mantiq --no-resume
```

### Command Line Arguments

| Argument | Description | Default |
|---|---|---|
| `--site` | Select which site to scrape: `aqeedah`, `hanafi_fiqh`, `mantiq`, or `all`. | `all` |
| `--book` | Scrape only books matching this case-insensitive filter. | `[]` (all) |
| `--list` | Lists all volumes discovered on the site catalog and exits. | `False` |
| `--no-cache` | Processes files in memory and deletes temporary JSON caches. | `False` |
| `--workers` | Number of threads to run concurrently for volume downloads. | `3` |
| `--timeout` | Timeout in seconds for individual web requests. | `90` |
| `--retries` | Number of network retries with exponential delay backoff. | `4` |
| `--no-resume` | Disables manifest checks and forces re-scraping of already-completed books. | `False` (resumes by default) |

---

## License & Compliance
This utility is intended for personal archival use and offline reading. The text exports retain all source translation notices, OCR warnings, and academic disclaimers.
