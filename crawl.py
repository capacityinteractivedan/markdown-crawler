#!/usr/bin/env python3
"""
crawl.py — Archive website pages to clean Markdown for AI analysis.

Usage:
    python crawl.py urls.txt [--output website-export] [--screenshots]
                             [--no-images] [--delay 1.5] [--log-level INFO]
"""

import argparse
import asyncio
import dataclasses
import hashlib
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import aiohttp
import trafilatura
from bs4 import BeautifulSoup
from markdownify import markdownify
from playwright.async_api import async_playwright, Error as PlaywrightError
from slugify import slugify

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class Config:
    urls_file: Path
    output_dir: Path
    screenshots: bool
    images: bool
    delay: float
    log_level: str


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ImageRecord:
    original_url: str
    local_path: str
    alt_text: str


@dataclass
class PageResult:
    source_url: str
    title: str
    markdown_file: str
    screenshot_file: str | None
    images: list[ImageRecord]
    capture_date: str
    status: str
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def url_to_slug(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.strip("/")
    if not path:
        path = parsed.netloc
    slug = slugify(path.replace("/", "-"), max_length=80)
    return slug or "index"


def image_filename(page_slug: str, alt_text: str, url: str, ext: str) -> str:
    url_hash = hashlib.md5(url.encode()).hexdigest()[:6]
    alt_slug = slugify(alt_text or "image", max_length=40) or "image"
    return f"{page_slug[:40]}-{alt_slug}-{url_hash}{ext}"


def sanitize_frontmatter_value(value: str) -> str:
    return value.replace('"', '\\"').replace("\n", " ")


def build_frontmatter(title: str, url: str, description: str, canonical: str, capture_date: str) -> str:
    lines = ['---']
    lines.append(f'title: "{sanitize_frontmatter_value(title)}"')
    lines.append(f'source_url: "{sanitize_frontmatter_value(url)}"')
    lines.append(f'capture_date: "{capture_date}"')
    if description:
        lines.append(f'description: "{sanitize_frontmatter_value(description)}"')
    if canonical:
        lines.append(f'canonical_url: "{sanitize_frontmatter_value(canonical)}"')
    lines.append('---\n')
    return "\n".join(lines)


def load_urls(path: Path) -> list[str]:
    seen: set[str] = set()
    urls: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line not in seen:
            seen.add(line)
            urls.append(line)
    return urls


def setup_output_dirs(config: Config) -> None:
    config.output_dir.mkdir(parents=True, exist_ok=True)
    if config.images:
        (config.output_dir / "images").mkdir(exist_ok=True)
    if config.screenshots:
        (config.output_dir / "screenshots").mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# Content extraction
# ---------------------------------------------------------------------------

def extract_content(html: str, url: str, include_images: bool = True) -> str:
    result = trafilatura.extract(
        html,
        output_format="markdown",
        include_images=include_images,
        include_links=True,
        include_tables=True,
        url=url,
        no_fallback=False,
    )
    if result:
        return result

    logger.debug("Trafilatura returned None for %s, using BeautifulSoup fallback", url)
    return _bs4_extract(html)


def _bs4_extract(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")

    noise_tags = ["nav", "footer", "aside", "header", "script", "style", "noscript"]
    for tag in soup.find_all(noise_tags):
        tag.decompose()

    noise_keywords = ["cookie", "banner", "popup", "gdpr", "consent", "advertisement", "sidebar"]
    for tag in soup.find_all(True):
        classes = " ".join(tag.get("class", []))
        tag_id = tag.get("id", "")
        combined = (classes + " " + tag_id).lower()
        if any(kw in combined for kw in noise_keywords):
            tag.decompose()

    container = (
        soup.find("main")
        or soup.find("article")
        or _largest_div(soup)
        or soup.find("body")
    )

    if container is None:
        return ""

    html_str = str(container)
    return markdownify(html_str, heading_style="ATX", newline_style="backslash")


def _largest_div(soup: BeautifulSoup):
    best, best_len = None, 0
    for div in soup.find_all("div"):
        text_len = len(div.get_text())
        if text_len > best_len:
            best, best_len = div, text_len
    return best


# ---------------------------------------------------------------------------
# Image processing
# ---------------------------------------------------------------------------

IMAGE_RE = re.compile(r'!\[([^\]]*)\]\(([^)\s]+)\)')
CONTENT_TYPE_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "image/avif": ".avif",
}


def _ext_from_url(url: str) -> str:
    path = urlparse(url).path
    suffix = Path(path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".avif", ".bmp"}:
        return suffix if suffix != ".jpeg" else ".jpg"
    return ""


async def _download_image(
    session: aiohttp.ClientSession,
    img_url: str,
    local_path: Path,
    semaphore: asyncio.Semaphore,
) -> bool:
    async with semaphore:
        try:
            async with session.get(img_url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status >= 400:
                    logger.warning("Image download failed (%d): %s", resp.status, img_url)
                    return False
                data = await resp.read()
                local_path.write_bytes(data)
                return True
        except Exception as exc:
            logger.warning("Image download error for %s: %s", img_url, exc)
            return False


async def _ext_from_head(session: aiohttp.ClientSession, url: str) -> str:
    try:
        async with session.head(url, timeout=aiohttp.ClientTimeout(total=10), allow_redirects=True) as resp:
            ct = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
            return CONTENT_TYPE_EXT.get(ct, ".jpg")
    except Exception:
        return ".jpg"


async def process_images(
    markdown: str,
    base_url: str,
    page_slug: str,
    output_dir: Path,
    seen_images: dict[str, str],
    session: aiohttp.ClientSession,
) -> tuple[str, list[ImageRecord]]:
    matches = IMAGE_RE.findall(markdown)
    if not matches:
        return markdown, []

    semaphore = asyncio.Semaphore(5)
    records: list[ImageRecord] = []
    replacements: dict[str, str] = {}

    async def handle_image(alt_text: str, raw_url: str) -> None:
        abs_url = urljoin(base_url, raw_url)

        if abs_url in seen_images:
            replacements[raw_url] = seen_images[abs_url]
            return

        ext = _ext_from_url(abs_url)
        if not ext:
            ext = await _ext_from_head(session, abs_url)

        filename = image_filename(page_slug, alt_text, abs_url, ext)
        local_path = output_dir / "images" / filename
        rel_path = f"images/{filename}"

        ok = await _download_image(session, abs_url, local_path, semaphore)
        if ok:
            seen_images[abs_url] = rel_path
            replacements[raw_url] = rel_path
            records.append(ImageRecord(original_url=abs_url, local_path=rel_path, alt_text=alt_text))
        else:
            replacements[raw_url] = raw_url  # leave original on failure

    await asyncio.gather(*[handle_image(alt, url) for alt, url in matches])

    def rewrite(match: re.Match) -> str:
        alt, url = match.group(1), match.group(2)
        new_url = replacements.get(url, url)
        return f"![{alt}]({new_url})"

    return IMAGE_RE.sub(rewrite, markdown), records


# ---------------------------------------------------------------------------
# Page fetch
# ---------------------------------------------------------------------------

async def fetch_page(page, url: str, config: Config, screenshot_path: Path | None) -> dict:
    result = {
        "html": "",
        "title": "",
        "description": "",
        "canonical": "",
        "screenshot_file": None,
        "error": None,
    }

    try:
        response = await page.goto(url, wait_until="networkidle", timeout=30_000)
    except PlaywrightError as exc:
        result["error"] = f"Navigation error: {exc}"
        return result
    except Exception as exc:
        result["error"] = f"Unexpected error during navigation: {exc}"
        return result

    if response is None:
        result["error"] = "No response received"
        return result

    if response.status >= 400:
        result["error"] = f"HTTP {response.status}"
        return result

    await page.wait_for_timeout(500)

    result["title"] = await page.title()
    result["html"] = await page.content()
    result["description"] = await page.evaluate(
        "() => document.querySelector('meta[name=\"description\"]')?.content || ''"
    )
    result["canonical"] = await page.evaluate(
        "() => document.querySelector('link[rel=\"canonical\"]')?.href || ''"
    )

    if config.screenshots and screenshot_path is not None:
        try:
            await page.screenshot(path=str(screenshot_path), full_page=True)
            result["screenshot_file"] = str(screenshot_path)
        except Exception as exc:
            logger.warning("Screenshot failed for %s: %s", url, exc)

    return result


# ---------------------------------------------------------------------------
# Per-URL orchestration
# ---------------------------------------------------------------------------

async def process_url(
    url: str,
    page,
    session: aiohttp.ClientSession,
    config: Config,
    seen_images: dict[str, str],
) -> PageResult:
    capture_date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    slug = url_to_slug(url)
    md_filename = f"{slug}.md"
    md_path = config.output_dir / md_filename
    screenshot_rel: str | None = None
    screenshot_path: Path | None = None

    if config.screenshots:
        screenshot_rel = f"screenshots/{slug}.png"
        screenshot_path = config.output_dir / screenshot_rel

    result = PageResult(
        source_url=url,
        title="",
        markdown_file=md_filename,
        screenshot_file=screenshot_rel if config.screenshots else None,
        images=[],
        capture_date=capture_date,
        status="success",
        errors=[],
    )

    logger.info("Processing: %s", url)

    page_data = await fetch_page(page, url, config, screenshot_path)

    if page_data["error"]:
        result.status = "failed"
        result.errors.append(page_data["error"])
        _write_error_md(md_path, url, result.title, capture_date, page_data["error"])
        return result

    result.title = page_data["title"]

    if not page_data["html"]:
        result.status = "failed"
        result.errors.append("Empty HTML response")
        _write_error_md(md_path, url, result.title, capture_date, "Empty HTML response")
        return result

    try:
        markdown = extract_content(page_data["html"], url, include_images=config.images)
    except Exception as exc:
        logger.exception("Content extraction failed for %s", url)
        result.status = "failed"
        result.errors.append(f"Extraction error: {exc}")
        _write_error_md(md_path, url, result.title, capture_date, str(exc))
        return result

    if not markdown:
        result.status = "failed"
        result.errors.append("No content extracted")
        _write_error_md(md_path, url, result.title, capture_date, "No content extracted")
        return result

    if config.images:
        try:
            markdown, image_records = await process_images(
                markdown, url, slug, config.output_dir, seen_images, session
            )
            result.images = image_records
        except Exception as exc:
            logger.exception("Image processing failed for %s", url)
            result.errors.append(f"Image processing error: {exc}")
    else:
        markdown = IMAGE_RE.sub("", markdown)

    if not config.screenshots or screenshot_path is None or not screenshot_path.exists():
        result.screenshot_file = None

    frontmatter = build_frontmatter(
        title=result.title,
        url=url,
        description=page_data["description"],
        canonical=page_data["canonical"],
        capture_date=capture_date,
    )
    md_path.write_text(frontmatter + "\n" + markdown, encoding="utf-8")
    logger.info("Saved: %s", md_filename)

    return result


def _write_error_md(path: Path, url: str, title: str, capture_date: str, error: str) -> None:
    frontmatter = build_frontmatter(
        title=title or url,
        url=url,
        description="",
        canonical="",
        capture_date=capture_date,
    )
    path.write_text(
        frontmatter + f"\n> **Capture failed:** {error}\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

def write_manifest(pages: list[PageResult], output_dir: Path) -> None:
    successful = sum(1 for p in pages if p.status == "success")
    data = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total": len(pages),
        "successful": successful,
        "failed": len(pages) - successful,
        "pages": [dataclasses.asdict(p) for p in pages],
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Manifest written to %s", manifest_path)


# ---------------------------------------------------------------------------
# Main async loop
# ---------------------------------------------------------------------------

async def main_async(config: Config) -> None:
    urls = load_urls(config.urls_file)
    if not urls:
        logger.error("No URLs found in %s", config.urls_file)
        sys.exit(1)

    logger.info("Loaded %d URL(s)", len(urls))
    setup_output_dirs(config)

    pages: list[PageResult] = []
    seen_images: dict[str, str] = {}

    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 900},
            )
            page = await context.new_page()

            try:
                for i, url in enumerate(urls):
                    result = await process_url(url, page, session, config, seen_images)
                    pages.append(result)

                    if i < len(urls) - 1:
                        logger.debug("Waiting %.1fs before next request", config.delay)
                        await asyncio.sleep(config.delay)
            finally:
                await page.close()
                await context.close()
                await browser.close()

    write_manifest(pages, config.output_dir)

    failed = [p for p in pages if p.status == "failed"]
    if failed:
        logger.warning("%d page(s) failed:", len(failed))
        for p in failed:
            logger.warning("  %s — %s", p.source_url, "; ".join(p.errors))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> Config:
    parser = argparse.ArgumentParser(
        description="Archive website pages to clean Markdown for AI analysis."
    )
    parser.add_argument("urls_file", type=Path, help="Text file with one URL per line")
    parser.add_argument(
        "--output", type=Path, default=Path("website-export"),
        metavar="DIR", help="Output directory (default: website-export)"
    )
    parser.add_argument(
        "--screenshots", action="store_true",
        help="Save full-page screenshots for each URL"
    )
    parser.add_argument(
        "--no-images", action="store_true",
        help="Skip image downloads and strip image references from Markdown"
    )
    parser.add_argument(
        "--delay", type=float, default=1.5, metavar="SECONDS",
        help="Delay between requests in seconds (default: 1.5)"
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO)"
    )
    args = parser.parse_args()

    if not args.urls_file.exists():
        parser.error(f"URLs file not found: {args.urls_file}")

    return Config(
        urls_file=args.urls_file,
        output_dir=args.output,
        screenshots=args.screenshots,
        images=not args.no_images,
        delay=args.delay,
        log_level=args.log_level,
    )


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )


def main() -> None:
    config = parse_args()
    setup_logging(config.log_level)
    asyncio.run(main_async(config))


if __name__ == "__main__":
    main()
