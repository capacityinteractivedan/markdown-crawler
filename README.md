# markdown-crawler

Archive website pages to clean Markdown for AI analysis. Given a list of URLs, produces local Markdown files with stripped noise, downloaded images, optional full-page screenshots, and a `manifest.json` index.

## Requirements

- Python 3.11+
- pip

## Installation

```bash
# 1. Install Python dependencies
pip install -r requirements.txt

# 2. Install the Playwright browser (Chromium only)
playwright install chromium
```

## Usage

Create a `urls.txt` file with one URL per line:

```
https://example.com/about
https://example.com/products
# lines starting with # are ignored
```

Then run:

```bash
python crawl.py urls.txt
```

### Options

| Flag | Default | Description |
|---|---|---|
| `--output DIR` | `website-export` | Output directory |
| `--screenshots` | off | Save full-page PNG screenshots |
| `--no-images` | off | Skip image downloads; strips image references from Markdown |
| `--delay SECONDS` | `1.5` | Pause between requests |
| `--log-level LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, or `ERROR` |

### Examples

```bash
# Basic run
python crawl.py urls.txt

# With screenshots, custom output folder, faster crawl
python crawl.py urls.txt --output archive --screenshots --delay 0.5

# Verbose logging
python crawl.py urls.txt --log-level DEBUG
```

## Output structure

```
website-export/
  about.md                  # Markdown with YAML frontmatter
  products-widget.md
  images/
    about-team-photo-a1b2c3.jpg
    products-hero-d4e5f6.png
  screenshots/              # only when --screenshots is used
    about.png
    products-widget.png
  manifest.json             # machine-readable index of all pages
```

### Markdown frontmatter

Every `.md` file starts with:

```yaml
---
title: "Page Title"
source_url: "https://example.com/about"
capture_date: "2026-06-02T14:23:00Z"
description: "Meta description text"
canonical_url: "https://example.com/about"
---
```

### manifest.json

```json
{
  "generated_at": "2026-06-02T14:23:00Z",
  "total": 2,
  "successful": 2,
  "failed": 0,
  "pages": [
    {
      "source_url": "https://example.com/about",
      "title": "About Us",
      "markdown_file": "about.md",
      "screenshot_file": "screenshots/about.png",
      "images": [
        {
          "original_url": "https://example.com/img/team.jpg",
          "local_path": "images/about-team-a1b2c3.jpg",
          "alt_text": "Our team"
        }
      ],
      "capture_date": "2026-06-02T14:23:01Z",
      "status": "success",
      "errors": []
    }
  ]
}
```

## Converting to Word (.docx)

If you need a Word document from a captured Markdown file, [Pandoc](https://pandoc.org/) handles this in one command:

```bash
pandoc ~/Documents/client-report.md -o ~/Documents/client-report.docx
```

Install Pandoc via `brew install pandoc` on macOS or from [pandoc.org/installing](https://pandoc.org/installing.html).

## Notes

- Pages are loaded via Playwright (headless Chromium) so JavaScript-rendered content is captured.
- Content extraction uses [Trafilatura](https://trafilatura.readthedocs.io/) as the primary engine (trained model for noise removal), with a BeautifulSoup fallback.
- Images are deduplicated globally across all pages — if the same image URL appears on multiple pages, it is downloaded once.
- Failed pages are logged and recorded in `manifest.json` with `"status": "failed"`; the run continues for remaining URLs.
