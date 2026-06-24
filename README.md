# Aqeedah Courses JSON/TXT scraper

This workspace contains a complete plain-text exporter for
`https://aqeedah-courses.pages.dev/`. It discovers the live catalog, reads the static
app's canonical JSON, and creates one clean UTF-8 `.txt` file per listed book/volume.
The TXT files contain no HTML and no JSON braces, quotes, or commas. Each export has:

- portal and author/title metadata;
- complete course and module metadata;
- every full lesson and every linked Arabic/English source passage;
- all unknown/new fields preserved as readable plain-text key/value metadata;
- glossary, search index, quizzes, reviews, audit information, infographics,
  activities, and concept edges;
- validation counts and a SHA-256 manifest.

No browser or third-party Python package is required.

## Current archive

The checked workspace output in `scraped_books/` was generated from the live portal
on 2026-06-24. It contains 56 TXT exports covering 63,105 full lessons, 910 modules,
and 49,468 distinct source passages. See `scraped_books/run_manifest.json` for the
run result and each adjacent `*.manifest.json` file for counts and a SHA-256 digest.

## Run

```bash
python3 scrape_aqeedah.py
```

Outputs go to `scraped_books/`, grouped by portal category. Raw JSON is cached under
`scraped_books/.cache/`, so an interrupted run can simply be started again.

Useful commands:

```bash
# Show everything discovered from the live homepage
python3 scrape_aqeedah.py --list

# Scrape one book/volume (filters may be repeated)
python3 scrape_aqeedah.py --book "Sanusi 01"

# Lighter exports without large auxiliary search/glossary/audit datasets
python3 scrape_aqeedah.py --no-aux

# Rebuild output even when a completed sidecar manifest exists
python3 scrape_aqeedah.py --no-resume
```

Run `python3 scrape_aqeedah.py --help` for concurrency, timeout, retry, cache, and
output options. Keep the OCR and machine-generated-content warnings from the source
site in mind; the scraper preserves review flags but does not certify the material.
