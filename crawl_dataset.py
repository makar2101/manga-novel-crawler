#!/usr/bin/env python3
"""
Download/export light novels and manga into a local manga/data dataset.

Run `python crawl_dataset.py` without arguments. Runtime options live in
crawler_config.json.

Primary output:
  output/manga_dataset/data/10_raw/<novel-slug>/00001.txt
  output/manga_dataset/photos/<novel-slug>/...
  output/manga_dataset/manifest.json
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import html
import io
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote, urlparse


APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "crawler_config.json"
DEFAULT_OUT = APP_DIR / "output" / "manga_dataset"
DEFAULT_LNCRAWL_DATA = APP_DIR / ".lncrawl_data"
DEFAULT_NOVEL_TITLES = APP_DIR / "titles_novel.txt"
DEFAULT_MANGA_TITLES = APP_DIR / "titles_manga.txt"
FRESH_NEXT_RUN = APP_DIR / ".fresh_next_run"
LNCRAWL_RUNNER = APP_DIR / "run_lncrawl_with_workers.py"

MANGA_SOURCE_PRIORITY = [
    "mangabuddy",
    "readmanganato",
    "mangatx",
    "manga-tx",
    "topmanhua",
    "manhuaplus",
    "mangaread",
    "mangachill",
    "bato",
    "zinmanga",
    "harimanga",
    "coffeemanga",
    "kingmanga",
    "kissmanga",
]

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif"}
URL_RE = re.compile(r"https?://[^\s\]\)>'\"`]+")

GARBAGE_LINE_PATTERNS = [
    r"Failed to download chapter body",
    r"^\s*(?:hash|md5|sha1|sha256|checksum)\s*[:：]?\s*[0-9a-fA-F]{20,}\s*$",
    r"^\s*[0-9a-fA-F]{32,}\s*$",
    r"(?i).*(?:novelfull|webnovel|wuxiaworld|boxnovel|lightnovel|readlightnovel|allnovel|freewebnovel|novelbin|novelhall|novelbuddy|69shuba|qidian|mtlnovel).*",
    r"^https?://.*$",
    r"^www\..*$",
    r".*discord\.gg.*",
    r"^\s*Translator\s*:\s*",
    r"^\s*Editor\s*:\s*",
    r"^\s*Proofreader\s*:\s*",
    r"^\s*TLC\s*:\s*",
    r"^\s*(?:Author|Writer|Source|Credit|Credits)\s*[:：]\s*",
    r"^\s*\*?\s*(?:TL|T/L|Translator)\s+note\s*[:：].*$",
    r"^\s*(?:※|×|\*|＊|=|_|─){3,}\s*$",
    r"^\s*[—–\-─]{2,}\s*$",
    r"^\s*Search!?\s*$",
    r"(?i).*Atlas\s*Studios.*",
]
GARBAGE_PATTERNS = [re.compile(pattern, re.IGNORECASE | re.UNICODE) for pattern in GARBAGE_LINE_PATTERNS]

DEFAULT_CONFIG = {
    "input": {
        "manga_titles": ["titles_manga.txt"],
        "novel_titles": ["titles_novel.txt"],
    },
    "download": {
        "enabled": True,
        "chapter_limit": 0,
        "chapter_limit_from": "first",
        "candidates": 1,
        "search_limit": 5,
        "search_timeout_seconds": 30,
        "command_timeout_seconds": 21600,
        "manga_sources": [],
        "novel_sources": [],
    },
    "output": {
        "root": "output/manga_dataset",
        "fresh_start": False,
        "clean_title_before_export": True,
    },
    "cache": {
        "lncrawl_data": ".lncrawl_data",
    },
    "performance": {
        "lncrawl_workers": 16,
        "repair_workers": 6,
        "progress_interval_seconds": 10,
    },
    "processing": {
        "export_kind": "auto",
        "preserve_numbering": True,
        "repair_images": True,
        "repair_attempts": 5,
        "repair_timeout_seconds": 30,
        "min_text_chars": 250,
        "min_text_words": 40,
        "max_photos_per_title": 0,
        "body_image_min_bytes": 12000,
        "body_image_min_pixels": 120000,
        "export_all_sources": False,
    },
}

SIMPLE_CONFIG_TEMPLATE = {
    "input_file": "titles_manga.txt",
    "content_type": "manga",
    "chapters": 3,
    "fresh_start": False,
}

DUAL_CONFIG_TEMPLATE = {
    "manga": {
        "input_file": "titles_manga.txt",
        "content_type": "manga",
        "chapters": 3,
        "fresh_start": False,
    },
    "novel": {
        "input_file": "titles_novel.txt",
        "content_type": "novel",
        "chapters": 3,
        "fresh_start": False,
    },
}


@dataclass
class Chapter:
    title: str
    text: str
    source: str
    serial: int | None = None


@dataclass
class NovelResult:
    slug: str
    title: str
    source: str
    urls: list[str] = field(default_factory=list)
    kind: str = "text"
    chapters_written: int = 0
    chapters_dropped: int = 0
    photos_written: int = 0
    covers_written: int = 0
    extra_images_written: int = 0
    body_images_written: int = 0
    body_images_dropped: int = 0
    garbage_lines_dropped: int = 0
    text_score: int = 0
    validation_status: str = "not_checked"
    validation_errors: list[str] = field(default_factory=list)
    validation_warnings: list[str] = field(default_factory=list)
    validation_details: dict = field(default_factory=dict)
    notes: list[dict] = field(default_factory=list)


class HtmlTextExtractor(HTMLParser):
    block_tags = {
        "article",
        "blockquote",
        "br",
        "dd",
        "div",
        "dl",
        "dt",
        "figcaption",
        "figure",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "ol",
        "p",
        "pre",
        "section",
        "td",
        "th",
        "tr",
        "ul",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag.lower() in self.block_tags:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self.block_tags:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if data:
            self.parts.append(data)

    def get_text(self) -> str:
        return "".join(self.parts)


class PhotoStore:
    def __init__(self, root: Path, max_per_novel: int) -> None:
        self.root = root
        self.max_per_novel = max_per_novel
        self.hashes: set[str] = set()
        self.count_by_slug: dict[str, int] = {}
        self.count_by_folder: dict[str, int] = {}

    def add_file(
        self,
        slug: str,
        path: Path,
        label: str = "image",
        category: str = "extras",
        subdir: str | None = None,
        min_bytes: int = 0,
        min_pixels: int = 0,
    ) -> bool:
        if not path.is_file() or path.suffix.lower() not in IMAGE_EXTS:
            return False
        return self.add_bytes(slug, path.read_bytes(), path.suffix, label, category, subdir, min_bytes, min_pixels)

    def add_bytes(
        self,
        slug: str,
        data: bytes,
        suffix: str,
        label: str = "image",
        category: str = "extras",
        subdir: str | None = None,
        min_bytes: int = 0,
        min_pixels: int = 0,
    ) -> bool:
        if not data:
            return False

        meta = image_meta(data, suffix)
        if image_reject_reason(meta, min_bytes, min_pixels):
            return False

        digest = hashlib.sha256(data).hexdigest()
        dedupe_key = f"{slug}:{digest}"
        if dedupe_key in self.hashes:
            return False

        current = self.count_by_slug.get(slug, 0)
        if self.max_per_novel and current >= self.max_per_novel:
            return False

        self.hashes.add(dedupe_key)
        self.count_by_slug[slug] = current + 1

        suffix = suffix.lower() if suffix.lower() in IMAGE_EXTS else ".jpg"
        if category == "body" and subdir:
            target_dir = self.root / slug / safe_path_component(subdir)
        else:
            target_dir = self.root / slug / safe_path_component(category)
            if subdir:
                target_dir = target_dir / safe_path_component(subdir)

        folder_key = target_dir.as_posix()
        folder_count = self.count_by_folder.get(folder_key, 0) + 1
        self.count_by_folder[folder_key] = folder_count

        filename = f"{folder_count:05}_{slugify(label) or 'image'}{suffix}"
        output = target_dir / filename
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(data)
        return True


@dataclass
class ImageMeta:
    byte_size: int
    suffix: str
    width: int | None = None
    height: int | None = None

    @property
    def pixels(self) -> int | None:
        if self.width and self.height:
            return self.width * self.height
        return None


@dataclass
class RepairImageJob:
    image_id: str
    novel_id: str
    title: str
    url: str
    referer: str
    output_file: Path
    chapter_serial: int


def safe_path_component(value: str) -> str:
    return slugify(value.replace("/", "-").replace("\\", "-"))


def image_meta(data: bytes, suffix: str) -> ImageMeta:
    suffix = suffix.lower()
    width: int | None = None
    height: int | None = None

    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
    elif data[:3] in {b"GIF", b"gif"} and len(data) >= 10:
        width = int.from_bytes(data[6:8], "little")
        height = int.from_bytes(data[8:10], "little")
    elif data.startswith(b"BM") and len(data) >= 26:
        width = abs(int.from_bytes(data[18:22], "little", signed=True))
        height = abs(int.from_bytes(data[22:26], "little", signed=True))
    elif data.startswith(b"\xff\xd8"):
        width, height = jpeg_dimensions(data)
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        width, height = webp_dimensions(data)

    return ImageMeta(byte_size=len(data), suffix=suffix, width=width, height=height)


def jpeg_dimensions(data: bytes) -> tuple[int | None, int | None]:
    index = 2
    sof_markers = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
    while index + 9 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        while index < len(data) and data[index] == 0xFF:
            index += 1
        if index >= len(data):
            break
        marker = data[index]
        index += 1
        if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        if index + 2 > len(data):
            break
        length = int.from_bytes(data[index : index + 2], "big")
        if length < 2 or index + length > len(data):
            break
        if marker in sof_markers and length >= 7:
            height = int.from_bytes(data[index + 3 : index + 5], "big")
            width = int.from_bytes(data[index + 5 : index + 7], "big")
            return width, height
        index += length
    return None, None


def webp_dimensions(data: bytes) -> tuple[int | None, int | None]:
    if len(data) < 30:
        return None, None
    chunk = data[12:16]
    if chunk == b"VP8X" and len(data) >= 30:
        width = 1 + int.from_bytes(data[24:27], "little")
        height = 1 + int.from_bytes(data[27:30], "little")
        return width, height
    if chunk == b"VP8 " and len(data) >= 30:
        width = int.from_bytes(data[26:28], "little") & 0x3FFF
        height = int.from_bytes(data[28:30], "little") & 0x3FFF
        return width, height
    if chunk == b"VP8L" and len(data) >= 25:
        bits = int.from_bytes(data[21:25], "little")
        width = 1 + (bits & 0x3FFF)
        height = 1 + ((bits >> 14) & 0x3FFF)
        return width, height
    return None, None


def image_reject_reason(meta: ImageMeta, min_bytes: int, min_pixels: int) -> str | None:
    if min_bytes and meta.byte_size < min_bytes:
        return f"too_small bytes={meta.byte_size}"
    if min_pixels and meta.pixels is not None and meta.pixels < min_pixels:
        return f"too_low_resolution pixels={meta.pixels}"
    return None


def image_file_valid(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        data = path.read_bytes()
    except OSError:
        return False
    if not data:
        return False
    meta = image_meta(data, path.suffix)
    if meta.width is None or meta.height is None:
        return False
    return not image_reject_reason(meta, 1, 1)


def jpeg_bytes_from_image(data: bytes) -> bytes:
    from PIL import Image  # type: ignore

    with Image.open(io.BytesIO(data)) as image:
        image.load()
        if image.mode not in ("L", "RGB", "YCbCr", "RGBX"):
            if image.mode == "RGBa":
                image = image.convert("RGBA").convert("RGB")
            else:
                image = image.convert("RGB")

        output = io.BytesIO()
        image.save(output, "JPEG", quality=95, optimize=True)
        return output.getvalue()


def slugify(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = value.encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-").lower()
    return value or "novel"


def unique_slug(base: str, source_label: str, used: set[str]) -> str:
    if base not in used:
        used.add(base)
        return base

    source = slugify(source_label)
    candidate = f"{base}-{source}" if source else base
    if candidate not in used:
        used.add(candidate)
        return candidate

    index = 2
    while f"{candidate}-{index}" in used:
        index += 1
    candidate = f"{candidate}-{index}"
    used.add(candidate)
    return candidate


def typed_slug(title: str, kind: str) -> str:
    slug = slugify(title)
    if kind == "manga" and not re.search(r"(?:^|-)(?:manga|manhua|manhwa|comic)(?:-|$)", slug):
        slug = f"{slug}-manga"
    return slug


def html_to_text(markup: str) -> str:
    parser = HtmlTextExtractor()
    try:
        parser.feed(markup)
        parser.close()
        return parser.get_text()
    except Exception:
        return re.sub(r"<[^>]+>", "\n", markup)


def normalize_text(text: str) -> str:
    if re.search(r"</?(?:p|br|div|h\d|span|strong|em|i|body|html)\b", text, re.I):
        text = html_to_text(text)
    text = html.unescape(text)
    text = unicodedata.normalize("NFC", text)
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", text)
    text = text.replace("\u00a0", " ")
    text = re.sub(r"\.(?:[ \t]+\.){2,}", "...", text)
    text = re.sub(r"\.[ \t]+\.", ".", text)
    text = re.sub(r"[ \t]+([.,!?;:])", r"\1", text)
    text = re.sub(r"[ \t]+([\u201D\u2019»])", r"\1", text)
    return text


def repeated_spam(line: str) -> bool:
    stripped = line.strip()
    if any(len(word) > 100 for word in stripped.split()):
        return True
    if re.search(r"(\w{4,})\1{2,}", stripped, re.I):
        return True
    if re.search(r"\b(\w{3,})\s+(\1\s+){3,}", stripped, re.I):
        return True
    return False


def garbage_reason(line: str) -> str | None:
    stripped = line.strip()
    if not stripped:
        return None
    if repeated_spam(stripped):
        return "repeated_spam"
    for index, pattern in enumerate(GARBAGE_PATTERNS):
        if pattern.match(stripped) or pattern.search(stripped):
            return f"garbage_pattern_{index}"
    return None


def clean_chapter(text: str, title: str) -> tuple[str, list[dict]]:
    text = normalize_text(text)
    kept: list[str] = []
    notes: list[dict] = []
    previous_non_empty: str | None = None

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = re.sub(r"[ \t]+", " ", raw_line).strip()
        if not line:
            if kept and kept[-1] != "":
                kept.append("")
            continue

        reason = garbage_reason(line)
        if reason:
            notes.append({"line": line_number, "reason": reason, "text": line[:220]})
            continue

        if line == previous_non_empty:
            notes.append({"line": line_number, "reason": "duplicate_line", "text": line[:220]})
            continue

        kept.append(line)
        previous_non_empty = line

    while kept and not kept[0]:
        kept.pop(0)
    while kept and not kept[-1]:
        kept.pop()

    body = "\n".join(kept).strip()
    title = title.strip()
    if title and body and not body.lower().startswith(title.lower()):
        body = f"{title}\n\n{body}"
    return (f"{body}\n" if body else "", notes)


def reject_reason(text: str, min_chars: int, min_words: int) -> str | None:
    stripped = text.strip()
    if not stripped:
        return "empty"
    if "Failed to download chapter body" in stripped:
        return "failed_download"

    words = re.findall(r"[A-Za-zА-Яа-яІіЇїЄєҐґ0-9']+", stripped)
    if len(stripped) < min_chars and len(words) < min_words:
        return f"too_short chars={len(stripped)} words={len(words)}"

    visible = [char for char in stripped if not char.isspace()]
    if visible:
        allowed = ".,!?;:'\"()[]{}<>/\\-–—…“”‘’`~@#$%^&*_+=|"
        odd = sum(1 for char in visible if not (char.isalnum() or char in allowed))
        if odd / len(visible) > 0.25:
            return "symbol_noise"
    return None


def write_chapters(
    output_root: Path,
    result: NovelResult,
    chapters: Iterable[Chapter],
    min_chars: int,
    min_words: int,
    preserve_numbering: bool,
) -> None:
    novel_dir = output_root / result.slug
    novel_dir.mkdir(parents=True, exist_ok=True)

    next_serial = 1
    for chapter in chapters:
        cleaned, clean_notes = clean_chapter(chapter.text, chapter.title)
        result.garbage_lines_dropped += len(clean_notes)
        reason = reject_reason(cleaned, min_chars, min_words)
        if reason:
            result.chapters_dropped += 1
            result.notes.append({"source": chapter.source, "serial": chapter.serial, "reason": reason})
            continue

        serial = chapter.serial if preserve_numbering and chapter.serial else next_serial
        (novel_dir / f"{serial:05}.txt").write_text(cleaned, encoding="utf-8")
        result.chapters_written += 1
        next_serial += 1

    result.text_score = result.chapters_written * 1000 - result.chapters_dropped * 25 - result.garbage_lines_dropped


def read_lncrawl_text(path: Path) -> str:
    data = path.read_bytes()
    if data.startswith(b"\x28\xb5\x2f\xfd"):
        import zstd  # type: ignore

        data = zstd.decompress(data)
    return data.decode("utf-8", errors="ignore")


def read_lncrawl_chapters(
    connection: sqlite3.Connection,
    data_root: Path,
    novel_id: str,
    serial_filter: set[int] | None = None,
) -> list[Chapter]:
    rows = connection.execute(
        "SELECT serial, title FROM chapters WHERE novel_id = ? ORDER BY serial",
        (novel_id,),
    ).fetchall()
    chapters: list[Chapter] = []
    for row in rows:
        serial = int(row["serial"] or 0)
        if serial_filter is not None and serial not in serial_filter:
            continue
        chapter_file = data_root / "novels" / novel_id / "chapters" / f"{row['serial']:06}.zst"
        if chapter_file.is_file():
            chapters.append(Chapter(row["title"] or "", read_lncrawl_text(chapter_file), chapter_file.as_posix(), serial))
    return chapters


def score_text_chapters(chapters: Iterable[Chapter], min_chars: int, min_words: int) -> tuple[int, int, int, int]:
    written = 0
    dropped = 0
    garbage = 0
    for chapter in chapters:
        cleaned, notes = clean_chapter(chapter.text, chapter.title)
        garbage += len(notes)
        if reject_reason(cleaned, min_chars, min_words):
            dropped += 1
        else:
            written += 1
    score = written * 1000 - dropped * 25 - garbage
    return score, written, dropped, garbage


def score_manga_images(
    connection: sqlite3.Connection,
    data_root: Path,
    novel: sqlite3.Row,
    args: argparse.Namespace,
) -> tuple[int, str]:
    chapter_rows = connection.execute(
        "SELECT serial FROM chapters WHERE novel_id = ? ORDER BY serial",
        (novel["id"],),
    ).fetchall()
    serial_filter = limited_chapter_serials(
        [int(row["serial"] or 0) for row in chapter_rows],
        int(novel["chapter_count"] or 0),
        args,
        "manga",
    )

    image_rows = connection.execute(
        """
        SELECT chapter_images.id AS image_id, chapters.serial AS chapter_serial
        FROM chapter_images
        JOIN chapters ON chapters.id = chapter_images.chapter_id
        WHERE chapter_images.novel_id = ? AND chapter_images.is_done = 1
        ORDER BY chapters.serial, chapter_images.created_at, chapter_images.id
        """,
        (novel["id"],),
    ).fetchall()

    novel_root = data_root / "novels" / novel["id"]
    chapter_serials: set[int] = set()
    valid_images = 0
    total_pixels = 0
    total_bytes = 0
    known_resolution = 0

    for row in image_rows:
        serial = int(row["chapter_serial"] or 0)
        if serial_filter is not None and serial not in serial_filter:
            continue

        image_file = novel_root / "images" / f"{row['image_id']}.jpg"
        if not image_file.is_file():
            continue
        try:
            data = image_file.read_bytes()
        except OSError:
            continue

        meta = image_meta(data, image_file.suffix)
        if image_reject_reason(meta, args.body_image_min_bytes, args.body_image_min_pixels):
            continue

        valid_images += 1
        total_bytes += meta.byte_size
        chapter_serials.add(serial)
        if meta.pixels:
            total_pixels += meta.pixels
            known_resolution += 1

    target_count = len(serial_filter) if serial_filter is not None else int(novel["chapter_count"] or 0)
    avg_pixels = total_pixels // known_resolution if known_resolution else 0
    avg_bytes = total_bytes // valid_images if valid_images else 0
    coverage = len(chapter_serials)

    score = coverage * 10_000_000_000 + avg_pixels * 1_000 + avg_bytes + valid_images
    return (
        score,
        f"chapters_with_images={coverage}/{target_count or len(chapter_rows)} "
        f"valid_images={valid_images} avg_pixels={avg_pixels} avg_bytes={avg_bytes}",
    )


def lncrawl_candidate_score(
    connection: sqlite3.Connection,
    data_root: Path,
    novel: sqlite3.Row,
    args: argparse.Namespace,
) -> tuple[int, str]:
    if novel["manga"]:
        return score_manga_images(connection, data_root, novel, args)

    chapter_rows = connection.execute(
        "SELECT serial FROM chapters WHERE novel_id = ? ORDER BY serial",
        (novel["id"],),
    ).fetchall()
    serial_filter = limited_chapter_serials(
        [int(row["serial"] or 0) for row in chapter_rows],
        int(novel["chapter_count"] or 0),
        args,
        "text",
    )
    chapters = read_lncrawl_chapters(connection, data_root, novel["id"], serial_filter)
    score, written, dropped, garbage = score_text_chapters(chapters, args.min_chars, args.min_words)
    return score, f"chapters={written} dropped={dropped} cleaned_lines={garbage}"


def select_lncrawl_novels(
    connection: sqlite3.Connection,
    data_root: Path,
    novels: list[sqlite3.Row],
    args: argparse.Namespace,
) -> list[sqlite3.Row]:
    if args.export_all_sources:
        return novels

    groups: dict[tuple[str, bool], list[sqlite3.Row]] = {}
    for novel in novels:
        groups.setdefault((slugify(novel["title"]), bool(novel["manga"])), []).append(novel)

    selected: list[sqlite3.Row] = []
    for group in groups.values():
        if len(group) == 1:
            selected.append(group[0])
            continue

        ranked = [(lncrawl_candidate_score(connection, data_root, novel, args), novel) for novel in group]
        ranked.sort(key=lambda item: item[0][0], reverse=True)
        best_score, best = ranked[0]
        selected.append(best)
        skipped = ", ".join(f"{novel['domain']} ({score[1]})" for score, novel in ranked[1:])
        print(f"[select] {best['title']}: using {best['domain']} ({best_score[1]}); skipped {skipped}")

    selected.sort(key=lambda row: row["updated_at"], reverse=True)
    return selected


def include_novel_kind(novel: sqlite3.Row, export_kind: str) -> bool:
    if export_kind == "all":
        return True
    if export_kind == "manga":
        return bool(novel["manga"])
    return not bool(novel["manga"])


def chapter_mode_key(kind: str | None) -> str:
    return "manga" if kind == "manga" else "novel"


def chapter_limit_for_kind(args: argparse.Namespace, kind: str | None = None) -> int:
    limits = getattr(args, "chapter_limits", {}) or {}
    key = chapter_mode_key(kind)
    if key in limits:
        return int(limits[key] or 0)
    return int(getattr(args, "chapter_limit", 0) or 0)


def chapter_limit_from_for_kind(args: argparse.Namespace, kind: str | None = None) -> str:
    values = getattr(args, "chapter_limit_from_by_kind", {}) or {}
    key = chapter_mode_key(kind)
    if key in values:
        return values[key]
    return getattr(args, "chapter_limit_from", "first")


def limited_chapter_serials(
    all_serials: Iterable[int],
    expected_count: int,
    args: argparse.Namespace,
    kind: str | None = None,
) -> set[int] | None:
    limit = chapter_limit_for_kind(args, kind)
    if limit <= 0:
        return None

    ordered = sorted(set(serial for serial in all_serials if serial > 0))
    if not ordered and expected_count > 0:
        ordered = list(range(1, expected_count + 1))
    if not ordered:
        return set()

    if chapter_limit_from_for_kind(args, kind) == "last":
        return set(ordered[-limit:])
    return set(ordered[:limit])


def clean_result_outputs(args: argparse.Namespace, raw_root: Path, photos_root: Path, slug: str) -> None:
    if not getattr(args, "clean_export", True):
        return
    for path in (raw_root / slug, photos_root / slug):
        if path.is_dir():
            shutil.rmtree(path)


def label_from_url(url: str, fallback: str) -> str:
    try:
        path = unquote(urlparse(url).path)
    except Exception:
        return fallback
    name = Path(path).stem
    return name or fallback


def json_object(value) -> dict:  # noqa: ANN001
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        data = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def image_row_skipped(row: sqlite3.Row) -> bool:
    extra = json_object(row["extra"] if "extra" in row.keys() else {})
    return extra.get("skip_reason") in {"source_broken", "source_non_image"}


def copy_lncrawl_media(
    connection: sqlite3.Connection,
    data_root: Path,
    novel: sqlite3.Row,
    slug: str,
    photos: PhotoStore,
    args: argparse.Namespace,
    serial_filter: set[int] | None = None,
) -> dict:
    novel_root = data_root / "novels" / novel["id"]
    stats = {
        "covers": 0,
        "extras": 0,
        "body": 0,
        "body_dropped": 0,
        "body_by_chapter": {},
        "body_dropped_by_chapter": {},
    }

    cover_candidates = sorted({novel_root / "cover.jpg", *novel_root.glob("cover.*")})
    for cover in cover_candidates:
        if photos.add_file(slug, cover, f"{novel['domain']}-cover", category="covers"):
            stats["covers"] += 1

    image_rows = connection.execute(
        """
        SELECT
            chapter_images.id AS image_id,
            chapter_images.url AS image_url,
            chapter_images.extra AS extra,
            chapters.serial AS chapter_serial,
            chapters.title AS chapter_title
        FROM chapter_images
        JOIN chapters ON chapters.id = chapter_images.chapter_id
        WHERE chapter_images.novel_id = ?
        ORDER BY chapters.serial, chapter_images.created_at, chapter_images.id
        """,
        (novel["id"],),
    ).fetchall()

    page_by_chapter: dict[int, int] = {}
    is_manga = bool(novel["manga"])
    for row in image_rows:
        if image_row_skipped(row):
            continue
        chapter_serial = int(row["chapter_serial"] or 0)
        if serial_filter is not None and chapter_serial not in serial_filter:
            continue
        page_by_chapter[chapter_serial] = page_by_chapter.get(chapter_serial, 0) + 1
        page_number = page_by_chapter[chapter_serial]
        image_file = novel_root / "images" / f"{row['image_id']}.jpg"
        label = f"{page_number:04}_{label_from_url(row['image_url'] or '', row['image_id'])}"

        if is_manga:
            added = photos.add_file(
                slug,
                image_file,
                label,
                category="body",
                subdir=f"chapter-{chapter_serial:05}",
                min_bytes=args.body_image_min_bytes,
                min_pixels=args.body_image_min_pixels,
            )
            if added:
                stats["body"] += 1
                stats["body_by_chapter"][chapter_serial] = stats["body_by_chapter"].get(chapter_serial, 0) + 1
            else:
                stats["body_dropped"] += 1
                stats["body_dropped_by_chapter"][chapter_serial] = (
                    stats["body_dropped_by_chapter"].get(chapter_serial, 0) + 1
                )
        elif photos.add_file(slug, image_file, label, category="extras", subdir="illustrations"):
            stats["extras"] += 1

    return stats


def ranges(values: Iterable[int]) -> list[str]:
    ordered = sorted(set(value for value in values if value > 0))
    if not ordered:
        return []

    result: list[str] = []
    start = previous = ordered[0]
    for value in ordered[1:]:
        if value == previous + 1:
            previous = value
            continue
        result.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = value
    result.append(str(start) if start == previous else f"{start}-{previous}")
    return result


def describe_ranges(values: Iterable[int], limit: int = 12) -> str:
    compact = ranges(values)
    if not compact:
        return "none"
    shown = compact[:limit]
    suffix = "" if len(compact) <= limit else f", +{len(compact) - limit} more ranges"
    return ", ".join(shown) + suffix


def validate_lncrawl_novel(
    connection: sqlite3.Connection,
    data_root: Path,
    novel: sqlite3.Row,
    slug: str,
    media: dict,
    result: NovelResult,
    args: argparse.Namespace,
    serial_filter: set[int] | None = None,
) -> None:
    errors: list[str] = []
    warnings: list[str] = []
    details: dict = {}

    all_chapters = connection.execute(
        "SELECT id, serial, title, is_done FROM chapters WHERE novel_id = ? ORDER BY serial",
        (novel["id"],),
    ).fetchall()
    chapters = [
        row
        for row in all_chapters
        if serial_filter is None or int(row["serial"] or 0) in serial_filter
    ]
    all_serials = [int(row["serial"] or 0) for row in all_chapters if int(row["serial"] or 0) > 0]
    serials = [int(row["serial"] or 0) for row in chapters if int(row["serial"] or 0) > 0]
    full_expected_count = int(novel["chapter_count"] or 0)
    if serial_filter is None:
        expected_count = full_expected_count
        max_serial = max([expected_count, *serials], default=0)
        validation_serials = list(range(1, max_serial + 1)) if max_serial else []
    else:
        validation_serials = sorted(serial_filter)
        expected_count = len(validation_serials)
        max_serial = max(validation_serials, default=0)
    missing_serials = sorted(set(validation_serials) - set(serials)) if validation_serials else []
    not_done_chapters = [int(row["serial"] or 0) for row in chapters if not row["is_done"]]

    details["db_chapters"] = len(chapters)
    details["expected_chapters"] = expected_count
    details["db_chapters_total"] = len(all_chapters)
    details["expected_chapters_total"] = full_expected_count
    details["max_serial"] = max_serial
    details["target_chapter_ranges"] = ranges(validation_serials)
    result_kind = "manga" if novel["manga"] else "text"
    details["chapter_limit"] = chapter_limit_for_kind(args, result_kind)
    details["chapter_limit_from"] = chapter_limit_from_for_kind(args, result_kind)
    details["missing_serial_ranges"] = ranges(missing_serials)
    details["not_done_chapter_ranges"] = ranges(not_done_chapters)

    if expected_count and len(chapters) != expected_count:
        errors.append(f"chapter_count mismatch: db={len(chapters)} expected={expected_count}")
    if missing_serials:
        errors.append(f"missing chapter serials: {describe_ranges(missing_serials)}")
    if not_done_chapters:
        errors.append(f"chapters not marked done: {describe_ranges(not_done_chapters)}")

    if not novel["manga"]:
        if result.chapters_written == 0:
            errors.append("no text chapters exported")
        if result.chapters_dropped:
            warnings.append(f"text chapters dropped by cleaner: {result.chapters_dropped}")
        details["text_chapters_written"] = result.chapters_written
        details["text_chapters_dropped"] = result.chapters_dropped
        result.validation_errors = errors
        result.validation_warnings = warnings
        result.validation_details = details
        result.validation_status = "failed" if errors else ("warning" if warnings else "ok")
        return

    image_rows = connection.execute(
        """
        SELECT
            chapters.serial AS chapter_serial,
            chapter_images.id AS image_id,
            chapter_images.extra AS extra,
            chapter_images.is_done AS is_done
        FROM chapter_images
        JOIN chapters ON chapters.id = chapter_images.chapter_id
        WHERE chapter_images.novel_id = ?
        ORDER BY chapters.serial, chapter_images.created_at, chapter_images.id
        """,
        (novel["id"],),
    ).fetchall()

    novel_root = data_root / "novels" / novel["id"]
    total_by_chapter: dict[int, int] = {}
    done_by_chapter: dict[int, int] = {}
    missing_files_by_chapter: dict[int, int] = {}
    skipped_by_chapter: dict[int, int] = {}
    total_images = 0
    done_images = 0
    missing_files = 0
    skipped_images = 0

    for row in image_rows:
        serial = int(row["chapter_serial"] or 0)
        if serial_filter is not None and serial not in serial_filter:
            continue
        if image_row_skipped(row):
            skipped_images += 1
            skipped_by_chapter[serial] = skipped_by_chapter.get(serial, 0) + 1
            continue
        total_images += 1
        total_by_chapter[serial] = total_by_chapter.get(serial, 0) + 1
        if row["is_done"]:
            done_images += 1
            done_by_chapter[serial] = done_by_chapter.get(serial, 0) + 1
            image_file = novel_root / "images" / f"{row['image_id']}.jpg"
            if not image_file.is_file():
                missing_files += 1
                missing_files_by_chapter[serial] = missing_files_by_chapter.get(serial, 0) + 1

    exported_by_chapter = {int(key): int(value) for key, value in media.get("body_by_chapter", {}).items()}
    dropped_by_chapter = {int(key): int(value) for key, value in media.get("body_dropped_by_chapter", {}).items()}

    zero_db_images = [serial for serial in validation_serials if total_by_chapter.get(serial, 0) == 0]
    zero_done_images = [serial for serial in validation_serials if done_by_chapter.get(serial, 0) == 0]
    zero_exported_images = [serial for serial in validation_serials if exported_by_chapter.get(serial, 0) == 0]
    not_done_images = total_images - done_images

    details.update(
        {
            "db_image_rows": total_images,
            "done_image_rows": done_images,
            "not_done_image_rows": not_done_images,
            "missing_image_files": missing_files,
            "source_broken_image_rows": skipped_images,
            "exported_body_images": result.body_images_written,
            "dropped_body_images": result.body_images_dropped,
            "chapters_with_zero_db_image_ranges": ranges(zero_db_images),
            "chapters_with_zero_done_image_ranges": ranges(zero_done_images),
            "chapters_with_zero_exported_image_ranges": ranges(zero_exported_images),
            "chapters_with_missing_file_ranges": ranges(missing_files_by_chapter.keys()),
            "chapters_with_source_broken_image_ranges": ranges(skipped_by_chapter.keys()),
            "chapters_with_dropped_image_ranges": ranges(dropped_by_chapter.keys()),
        }
    )

    if total_images == 0:
        errors.append("no manga image rows found in lncrawl database")
    if not_done_images:
        errors.append(f"undownloaded image rows: {not_done_images}")
    if missing_files:
        errors.append(f"downloaded image rows missing files on disk: {missing_files}")
    if zero_db_images:
        errors.append(f"chapters without image rows: {describe_ranges(zero_db_images)}")
    if zero_done_images:
        errors.append(f"chapters without downloaded images: {describe_ranges(zero_done_images)}")
    if zero_exported_images:
        errors.append(f"chapters exported empty after filtering: {describe_ranges(zero_exported_images)}")
    if result.body_images_dropped:
        warnings.append(f"body images dropped by quality filter/dedupe: {result.body_images_dropped}")
    if skipped_images:
        warnings.append(f"source-broken image rows skipped: {skipped_images}")

    result.validation_errors = errors
    result.validation_warnings = warnings
    result.validation_details = details
    result.validation_status = "failed" if errors else ("warning" if warnings else "ok")


def export_lncrawl_db(args: argparse.Namespace, raw_root: Path, photos: PhotoStore, used_slugs: set[str]) -> list[NovelResult]:
    data_root = Path(args.lncrawl_data).expanduser().resolve()
    db_path = data_root / "sqlite.db"
    if not db_path.is_file():
        return []

    results: list[NovelResult] = []
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        novels = connection.execute(
            "SELECT id, title, url, domain, manga, chapter_count, updated_at FROM novels ORDER BY updated_at DESC"
        ).fetchall()
        novels = [novel for novel in novels if include_novel_kind(novel, args.export_kind)]
        for novel in select_lncrawl_novels(connection, data_root, novels, args):
            kind = "manga" if novel["manga"] else "text"
            slug = unique_slug(typed_slug(novel["title"], kind), novel["domain"] or novel["id"], used_slugs)
            result = NovelResult(
                slug=slug,
                title=novel["title"],
                source=f"{data_root}:{novel['id']}",
                urls=[novel["url"]],
                kind=kind,
            )

            clean_result_outputs(args, raw_root, photos.root, result.slug)
            serial_rows = connection.execute(
                "SELECT serial FROM chapters WHERE novel_id = ? ORDER BY serial",
                (novel["id"],),
            ).fetchall()
            serial_filter = limited_chapter_serials(
                [int(row["serial"] or 0) for row in serial_rows],
                int(novel["chapter_count"] or 0),
                args,
                kind,
            )
            if serial_filter is not None:
                print(f"[limit] {result.slug}: exporting chapters {describe_ranges(serial_filter)}")

            if kind == "text":
                chapters = read_lncrawl_chapters(connection, data_root, novel["id"], serial_filter)
                write_chapters(raw_root, result, chapters, args.min_chars, args.min_words, args.preserve_numbering)
            media = copy_lncrawl_media(connection, data_root, novel, slug, photos, args, serial_filter)
            result.covers_written = media["covers"]
            result.extra_images_written = media["extras"]
            result.body_images_written = media["body"]
            result.body_images_dropped = media["body_dropped"]
            result.photos_written = result.covers_written + result.extra_images_written + result.body_images_written
            validate_lncrawl_novel(connection, data_root, novel, slug, media, result, args, serial_filter)
            results.append(result)
            print(
                f"[lncrawl] {result.slug}: kind={result.kind} chapters={result.chapters_written} "
                f"dropped={result.chapters_dropped} cleaned_lines={result.garbage_lines_dropped} "
                f"covers={result.covers_written} extras={result.extra_images_written} "
                f"body_images={result.body_images_written} body_dropped={result.body_images_dropped} "
                f"validation={result.validation_status}"
            )
            for message in result.validation_errors[:5]:
                print(f"[validate:error] {result.slug}: {message}")
            for message in result.validation_warnings[:3]:
                print(f"[validate:warn] {result.slug}: {message}")
    finally:
        connection.close()
    return results


def read_titles(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]


def title_file_has_entries(path: Path) -> bool:
    return path.is_file() and bool(read_titles(path))


def deep_merge_config(default: dict, user: dict) -> dict:
    merged: dict = {}
    for key, value in default.items():
        if isinstance(value, dict):
            merged[key] = deep_merge_config(value, user.get(key, {}) if isinstance(user.get(key), dict) else {})
        else:
            merged[key] = user.get(key, value)
    for key, value in user.items():
        if key not in merged:
            merged[key] = value
    return merged


def parse_chapter_limit(value) -> int:  # noqa: ANN001
    if isinstance(value, str) and value.strip().lower() in {"all", "усі", "всі", "*"}:
        return 0
    return config_int(value, 0, 0)


def normalize_content_type(value: str) -> str:
    value = str(value or "manga").strip().lower()
    if value in {"novel", "text", "book", "txt", "новела"}:
        return "novel"
    if value in {"auto", "all"}:
        return "auto"
    return "manga"


def expand_simple_config(config: dict) -> dict:
    expanded = deep_merge_config(DEFAULT_CONFIG, {})
    input_file = str(config.get("input_file") or SIMPLE_CONFIG_TEMPLATE["input_file"]).strip()
    content_type = normalize_content_type(config.get("content_type", SIMPLE_CONFIG_TEMPLATE["content_type"]))
    chapter_limit = parse_chapter_limit(config.get("chapters", SIMPLE_CONFIG_TEMPLATE["chapters"]))

    if content_type == "novel":
        expanded["input"]["manga_titles"] = []
        expanded["input"]["novel_titles"] = [input_file]
        expanded["processing"]["export_kind"] = "text"
    elif content_type == "auto":
        expanded["input"]["manga_titles"] = [input_file]
        expanded["input"]["novel_titles"] = [input_file]
        expanded["processing"]["export_kind"] = "auto"
    else:
        expanded["input"]["manga_titles"] = [input_file]
        expanded["input"]["novel_titles"] = []
        expanded["processing"]["export_kind"] = "manga"

    expanded["download"]["enabled"] = config_bool(config.get("download", True))
    expanded["download"]["chapter_limit"] = chapter_limit
    expanded["download"]["chapter_limit_from"] = "first"
    expanded["download"]["candidates"] = 999
    expanded["download"]["search_limit"] = 10
    expanded["download"]["manga_sources"] = []
    expanded["download"]["novel_sources"] = []
    expanded["processing"]["export_all_sources"] = False
    expanded["output"]["fresh_start"] = config_bool(config.get("fresh_start", False))
    if config.get("output"):
        expanded["output"]["root"] = str(config["output"])
    return expanded


def expand_dual_config(config: dict) -> dict:
    expanded = deep_merge_config(DEFAULT_CONFIG, {})
    expanded["input"]["manga_titles"] = []
    expanded["input"]["novel_titles"] = []
    expanded["download"]["chapter_limits"] = {}
    expanded["download"]["chapter_limit_from_by_kind"] = {}
    expanded["download"]["enabled"] = config_bool(config.get("download", True))
    expanded["download"]["candidates"] = 999
    expanded["download"]["search_limit"] = 10
    expanded["download"]["manga_sources"] = []
    expanded["download"]["novel_sources"] = []
    expanded["processing"]["export_all_sources"] = False

    fresh_start = config_bool(config.get("fresh_start", False))
    if config.get("output"):
        expanded["output"]["root"] = str(config["output"])

    for block_name, default_type in (("manga", "manga"), ("novel", "novel")):
        block = config.get(block_name)
        if not isinstance(block, dict):
            continue
        fresh_start = fresh_start or config_bool(block.get("fresh_start", False))
        input_file = str(block.get("input_file") or "").strip()
        if not input_file:
            continue

        content_type = normalize_content_type(block.get("content_type", default_type))
        chapter_limit = parse_chapter_limit(block.get("chapters", "all"))
        chapter_from = normalize_chapter_limit_from(block.get("chapter_limit_from", "first"))

        if content_type == "novel":
            expanded["input"]["novel_titles"].append(input_file)
            expanded["download"]["chapter_limits"]["novel"] = chapter_limit
            expanded["download"]["chapter_limit_from_by_kind"]["novel"] = chapter_from
        elif content_type == "auto":
            expanded["input"]["manga_titles"].append(input_file)
            expanded["input"]["novel_titles"].append(input_file)
            expanded["download"]["chapter_limits"]["manga"] = chapter_limit
            expanded["download"]["chapter_limits"]["novel"] = chapter_limit
            expanded["download"]["chapter_limit_from_by_kind"]["manga"] = chapter_from
            expanded["download"]["chapter_limit_from_by_kind"]["novel"] = chapter_from
        else:
            expanded["input"]["manga_titles"].append(input_file)
            expanded["download"]["chapter_limits"]["manga"] = chapter_limit
            expanded["download"]["chapter_limit_from_by_kind"]["manga"] = chapter_from

    expanded["processing"]["export_kind"] = "auto"
    expanded["output"]["fresh_start"] = fresh_start
    return expanded


def expand_crawler_config(config: dict) -> dict:
    if isinstance(config.get("manga"), dict) or isinstance(config.get("novel"), dict):
        return expand_dual_config(config)
    simple_keys = {"input_file", "content_type", "chapters", "fresh_start"}
    if simple_keys & set(config):
        return expand_simple_config(config)
    return deep_merge_config(DEFAULT_CONFIG, config)


def runtime_config_path() -> Path:
    override = os.getenv("CRAWLER_CONFIG", "").strip()
    if override:
        path = Path(override).expanduser()
        return path if path.is_absolute() else APP_DIR / path
    return CONFIG_PATH


def load_crawler_config() -> dict:
    config_path = runtime_config_path()
    if not config_path.is_file():
        config_path.write_text(json.dumps(DUAL_CONFIG_TEMPLATE, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[config] created default config: {config_path}")
        return expand_dual_config(DUAL_CONFIG_TEMPLATE)

    try:
        user_config = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise SystemExit(f"[config] invalid JSON in {config_path}: {error}") from error
    if not isinstance(user_config, dict):
        raise SystemExit(f"[config] root value must be a JSON object: {config_path}")

    print(f"[config] using {config_path}")
    return expand_crawler_config(user_config)


def config_list(value) -> list[str]:  # noqa: ANN001
    if value is None:
        return []
    if isinstance(value, str):
        value = value.strip()
        return [value] if value else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def config_bool(value) -> bool:  # noqa: ANN001
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def config_int(value, default: int, minimum: int | None = None, maximum: int | None = None) -> int:  # noqa: ANN001
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


def project_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else APP_DIR / path


def config_title_paths(values) -> list[str]:  # noqa: ANN001
    return [project_path(value).resolve().as_posix() for value in config_list(values)]


def normalize_export_kind(value: str) -> str:
    value = str(value or "auto").strip().lower()
    if value in {"text", "novel", "novels"}:
        return "text"
    if value in {"manga", "manhua", "manhwa", "comic", "comics"}:
        return "manga"
    if value == "all":
        return "all"
    return "auto"


def normalize_chapter_limit_from(value: str) -> str:
    value = str(value or "first").strip().lower()
    return "last" if value in {"last", "latest", "newest", "end"} else "first"


def args_from_config(config: dict) -> argparse.Namespace:
    input_config = config["input"]
    download_config = config["download"]
    output_config = config["output"]
    cache_config = config["cache"]
    performance_config = config["performance"]
    processing_config = config["processing"]

    novel_titles = config_title_paths(input_config.get("novel_titles"))
    manga_titles = config_title_paths(input_config.get("manga_titles"))
    novel_has_titles = any(title_file_has_entries(Path(path)) for path in novel_titles)
    manga_has_titles = any(title_file_has_entries(Path(path)) for path in manga_titles)

    export_kind = normalize_export_kind(processing_config.get("export_kind"))
    if export_kind == "auto":
        if manga_has_titles and not novel_has_titles:
            export_kind = "manga"
        elif novel_has_titles and not manga_has_titles:
            export_kind = "text"
        else:
            export_kind = "all"

    args = argparse.Namespace(
        titles=None,
        titles_mode="novel",
        novel_titles=novel_titles,
        manga_titles=manga_titles,
        download=config_bool(download_config.get("enabled")),
        source=config_list(download_config.get("novel_sources")),
        manga_source=config_list(download_config.get("manga_sources")),
        lncrawl_data=project_path(cache_config.get("lncrawl_data", DEFAULT_LNCRAWL_DATA)).resolve().as_posix(),
        out=project_path(output_config.get("root", DEFAULT_OUT)).resolve().as_posix(),
        search_limit=config_int(download_config.get("search_limit"), 5, 1),
        search_timeout=config_int(download_config.get("search_timeout_seconds"), 30, 1),
        download_candidates=config_int(download_config.get("candidates"), 1, 1),
        command_timeout=config_int(download_config.get("command_timeout_seconds"), 60 * 60 * 6, 60),
        lncrawl_workers=config_int(performance_config.get("lncrawl_workers"), 16, 1, 32),
        progress_interval=config_int(performance_config.get("progress_interval_seconds"), 10, 0),
        repair_workers=config_int(performance_config.get("repair_workers"), 6, 1, 32),
        repair_attempts=config_int(processing_config.get("repair_attempts"), 5, 1),
        repair_timeout=config_int(processing_config.get("repair_timeout_seconds"), 30, 5),
        min_chars=config_int(processing_config.get("min_text_chars"), 250, 0),
        min_words=config_int(processing_config.get("min_text_words"), 40, 0),
        max_photos_per_novel=config_int(processing_config.get("max_photos_per_title"), 0, 0),
        body_image_min_bytes=config_int(processing_config.get("body_image_min_bytes"), 12000, 0),
        body_image_min_pixels=config_int(processing_config.get("body_image_min_pixels"), 120000, 0),
        export_all_sources=config_bool(processing_config.get("export_all_sources")),
        export_kind=export_kind,
        preserve_numbering=config_bool(processing_config.get("preserve_numbering")),
        clean_export=config_bool(output_config.get("clean_title_before_export")),
        fresh=config_bool(output_config.get("fresh_start")),
        repair_images=config_bool(processing_config.get("repair_images")),
        chapter_limit=config_int(download_config.get("chapter_limit"), 0, 0),
        chapter_limit_from=normalize_chapter_limit_from(download_config.get("chapter_limit_from")),
        chapter_limits={
            key: config_int(value, 0, 0)
            for key, value in (download_config.get("chapter_limits") or {}).items()
        },
        chapter_limit_from_by_kind={
            key: normalize_chapter_limit_from(value)
            for key, value in (download_config.get("chapter_limit_from_by_kind") or {}).items()
        },
    )

    if FRESH_NEXT_RUN.is_file():
        args.fresh = True
        print(f"[auto] fresh marker detected: {FRESH_NEXT_RUN}")

    print(f"[config] output={args.out}")
    print(f"[config] download={'on' if args.download else 'off'} export_kind={args.export_kind}")
    if args.chapter_limits:
        for key in ("manga", "novel"):
            if key not in args.chapter_limits:
                continue
            limit = args.chapter_limits[key]
            if limit:
                print(f"[config] {key}_chapters={limit} from={args.chapter_limit_from_by_kind.get(key, 'first')}")
            else:
                print(f"[config] {key}_chapters=all")
    elif args.chapter_limit:
        print(f"[config] chapter_limit={args.chapter_limit} from={args.chapter_limit_from}")
    else:
        print("[config] chapter_limit=all")
    return args


def lncrawl_python() -> Path:
    return Path(sys.executable)


def lncrawl_command(command_args: list[str]) -> list[str]:
    python = lncrawl_python().as_posix()
    if LNCRAWL_RUNNER.is_file():
        return [python, LNCRAWL_RUNNER.as_posix(), *command_args]
    return [python, "-m", "lncrawl", *command_args]


def ensure_lncrawl_config(data_root: Path, args: argparse.Namespace | None = None) -> Path:
    data_root.mkdir(parents=True, exist_ok=True)
    config_path = data_root / "config.json"
    if config_path.is_file():
        config = json.loads(config_path.read_text(encoding="utf-8"))
    else:
        config = {
            "app": {
                "admin_email": os.getenv("LNCRAWL_ADMIN_EMAIL", "local-admin@example.invalid"),
                "admin_password": os.getenv("LNCRAWL_ADMIN_PASSWORD", secrets.token_urlsafe(24)),
                "openai_api_key": os.getenv("OPENAI_API_KEY", ""),
            },
            "crawler": {
                "can_use_browser": True,
                "disk_size_limit_mb": 0,
                "ignore_images": False,
                "runner_concurrency": 5,
                "runner_cooldown": 1,
                "runner_reset_interval": 14400,
                "cleaner_cooldown": 1800,
                "selenium_grid": "",
            },
            "database": {
                "connect_timeout": 10,
                "pool_recycle": 3600,
                "pool_size": 10,
                "pool_timeout": 30,
                "url": f"sqlite:///{(data_root / 'sqlite.db').resolve().as_posix()}",
            },
            "mail": {"smtp_password": "", "smtp_port": 1025, "smtp_sender": "", "smtp_server": "localhost", "smtp_username": ""},
            "server": {
                "base_url": "http://localhost:8080",
                "token_algorithm": "HS256",
                "token_expiry_minutes": 10080,
                "token_secret": secrets.token_hex(32),
            },
        }

    if args:
        crawler = config.setdefault("crawler", {})
        crawler["runner_concurrency"] = args.lncrawl_workers
        crawler["runner_cooldown"] = 0
        print(f"[speed] lncrawl workers={args.lncrawl_workers}", flush=True)

    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return config_path


def lncrawl_progress_snapshot(data_root: Path, url: str) -> tuple[str, int, int, int, int] | None:
    db_path = data_root / "sqlite.db"
    if not db_path.is_file():
        return None
    try:
        connection = sqlite3.connect(db_path, timeout=1)
        connection.row_factory = sqlite3.Row
        try:
            novel = connection.execute(
                "SELECT id, title FROM novels WHERE url = ? ORDER BY updated_at DESC LIMIT 1",
                (url,),
            ).fetchone()
            if not novel:
                novel = connection.execute("SELECT id, title FROM novels ORDER BY updated_at DESC LIMIT 1").fetchone()
            if not novel:
                return None

            row = connection.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN is_done THEN 1 ELSE 0 END) AS done
                FROM chapters
                WHERE novel_id = ?
                """,
                (novel["id"],),
            ).fetchone()
            images = connection.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN is_done THEN 1 ELSE 0 END) AS done
                FROM chapter_images
                WHERE novel_id = ?
                """,
                (novel["id"],),
            ).fetchone()
            return (
                novel["title"],
                int(row["done"] or 0),
                int(row["total"] or 0),
                int(images["done"] or 0),
                int(images["total"] or 0),
            )
        finally:
            connection.close()
    except sqlite3.Error:
        return None


def monitor_lncrawl_progress(data_root: Path, url: str, interval: int, stop_event: threading.Event) -> None:
    if interval <= 0:
        return

    start = time.monotonic()
    previous_done = 0
    previous_images_done = 0
    previous_time = start
    while not stop_event.wait(interval):
        snapshot = lncrawl_progress_snapshot(data_root, url)
        if not snapshot:
            print("[progress] waiting for lncrawl database...", flush=True)
            continue

        title, done, total, images_done, images_total = snapshot
        now = time.monotonic()
        elapsed = max(now - start, 1)
        window = max(now - previous_time, 1)
        recent_rate = (done - previous_done) / window
        average_rate = done / elapsed
        chapter_rate = recent_rate if recent_rate > 0 else average_rate
        recent_image_rate = (images_done - previous_images_done) / window
        average_image_rate = images_done / elapsed
        image_rate = recent_image_rate if recent_image_rate > 0 else average_image_rate
        tracking_images = images_total > 0 and images_done < images_total
        rate = image_rate if tracking_images else chapter_rate
        remaining = max(images_total - images_done, 0) if tracking_images else max(total - done, 0)
        eta = "unknown"
        if rate > 0 and remaining:
            eta_minutes = remaining / rate / 60
            eta = f"{eta_minutes:.0f} min"
        elif tracking_images and images_total and images_done >= images_total:
            eta = "done"
        elif total and done >= total:
            eta = "done"

        percent = (done / total * 100) if total else 0
        image_percent = (images_done / images_total * 100) if images_total else 0
        speed_label = f"{rate * 60:.1f} img/min" if tracking_images else f"{rate * 60:.1f} ch/min"
        print(
            f"[progress] {title}: chapters {done}/{total} ({percent:.1f}%), "
            f"images {images_done}/{images_total} ({image_percent:.1f}%), "
            f"speed {speed_label}, eta {eta}",
            flush=True,
        )
        previous_done = done
        previous_images_done = images_done
        previous_time = now


def run_lncrawl(
    data_root: Path,
    config_path: Path,
    args: list[str],
    timeout: int,
    stream: bool = False,
    workers: int = 8,
    progress_interval: int = 20,
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["LNCRAWL_DATA_PATH"] = data_root.as_posix()
    env["LNCRAWL_WORKERS"] = str(workers)
    command = lncrawl_command(["-c", config_path.as_posix(), *args])
    if stream:
        stop_event = threading.Event()
        monitor: threading.Thread | None = None
        if args and args[0] == "crawl" and args[-1].startswith("http"):
            monitor = threading.Thread(
                target=monitor_lncrawl_progress,
                args=(data_root, args[-1], progress_interval, stop_event),
                daemon=True,
            )
            monitor.start()
        try:
            result = subprocess.run(
                command,
                cwd=APP_DIR,
                env=env,
                text=True,
                timeout=timeout,
                check=False,
            )
            return result
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(command, 124, "", f"Timed out after {timeout} seconds")
        finally:
            stop_event.set()
            if monitor:
                monitor.join(timeout=2)

    return subprocess.run(
        command,
        cwd=APP_DIR,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )


def unique_urls(urls: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for url in urls:
        clean = url.rstrip(".,);]")
        if clean and clean not in seen:
            seen.add(clean)
            result.append(clean)
    return result


def default_source_names(require_manga: bool = False) -> list[str]:
    index_path = Path(".lncrawl_data/sources/_index.json")
    if not index_path.is_absolute():
        index_path = APP_DIR / index_path
    names: list[str] = []
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
        crawlers = data.get("crawlers", {})
        for crawler in crawlers.values():
            if not crawler.get("can_search"):
                continue
            if require_manga and not crawler.get("has_manga"):
                continue
            source_name = Path(crawler.get("file_path", "")).stem
            if source_name:
                names.append(source_name)
    except Exception:
        names = []

    return unique_urls(names)


def default_manga_sources() -> list[str]:
    return unique_urls([*MANGA_SOURCE_PRIORITY, *default_source_names(require_manga=True)])


def default_novel_sources() -> list[str | None]:
    sources = default_source_names(require_manga=False)
    return sources or [None]


def source_queries_for_mode(args: argparse.Namespace, mode: str) -> list[str | None]:
    if mode == "manga":
        return args.manga_source or default_manga_sources()
    return args.source or default_novel_sources()


def find_urls_for_title(args: argparse.Namespace, data_root: Path, config_path: Path, title: str, mode: str) -> list[str]:
    found: list[str] = []
    sources = source_queries_for_mode(args, mode)
    for index, source in enumerate(sources, start=1):
        label = source or "all sources"
        print(f"[search:{mode}] {title}: {label} ({index}/{len(sources)})", flush=True)
        command = ["search", "--limit", str(args.search_limit), "--timeout", str(args.search_timeout)]
        if source:
            command += ["--source", source]
        command.append(title)

        result = run_lncrawl(
            data_root,
            config_path,
            command,
            args.command_timeout,
            workers=args.lncrawl_workers,
            progress_interval=args.progress_interval,
        )
        urls = URL_RE.findall(result.stdout)
        if urls:
            found.extend(urls)
            unique_found = unique_urls(found)[: args.download_candidates]
            print(f"[search:{mode}] found {len(unique_found)} candidate(s): {', '.join(unique_found)}", flush=True)
            if len(unique_found) >= args.download_candidates:
                return unique_found
            continue
        print(f"[search:{mode}] no result from {label}", flush=True)
    return unique_urls(found)[: args.download_candidates]


def title_jobs(args: argparse.Namespace) -> list[tuple[Path, str]]:
    jobs: list[tuple[Path, str]] = []
    for title_file in args.novel_titles or []:
        path = Path(title_file).expanduser().resolve()
        if title_file_has_entries(path):
            jobs.append((path, "novel"))
        else:
            print(f"[input] no novel titles: {path}")
    for title_file in args.manga_titles or []:
        path = Path(title_file).expanduser().resolve()
        if title_file_has_entries(path):
            jobs.append((path, "manga"))
        else:
            print(f"[input] no manga titles: {path}")
    if args.titles:
        path = Path(args.titles).expanduser().resolve()
        if title_file_has_entries(path):
            jobs.append((path, args.titles_mode))
        else:
            print(f"[input] no titles: {path}")
    return jobs


def lncrawl_chapter_range_args(args: argparse.Namespace, mode: str) -> list[str]:
    kind = "manga" if mode == "manga" else "text"
    limit = chapter_limit_for_kind(args, kind)
    if limit <= 0:
        return ["--all"]
    option = "--last" if chapter_limit_from_for_kind(args, kind) == "last" else "--first"
    return [option, str(limit)]


def download_title_file(args: argparse.Namespace, title_path: Path, mode: str, data_root: Path, config_path: Path, log) -> None:  # noqa: ANN001
    titles = read_titles(title_path)
    if not titles:
        print(f"[batch:{mode}] no titles in {title_path}", flush=True)
        return

    print(f"[batch:{mode}] titles={len(titles)} file={title_path}", flush=True)
    for index, title in enumerate(titles, start=1):
        print(f"[batch:{mode}] {index}/{len(titles)} {title}", flush=True)
        urls = [title] if URL_RE.fullmatch(title) else find_urls_for_title(args, data_root, config_path, title, mode)
        if not urls:
            print(f"[miss:{mode}] {title}")
            log.write(f"[MISS:{mode}] {title}\n")
            continue

        for url_index, url in enumerate(urls, start=1):
            print(f"[crawl:{mode}] {title} candidate {url_index}/{len(urls)} -> {url}")
            log.write(f"[URL:{mode}] {title} candidate {url_index}/{len(urls)} -> {url}\n")
            log.flush()
            command = ["crawl", "--noin", *lncrawl_chapter_range_args(args, mode), "-f", "txt", url]
            print("[crawl] live lncrawl output starts below", flush=True)
            result = run_lncrawl(
                data_root,
                config_path,
                command,
                args.command_timeout,
                stream=True,
                workers=args.lncrawl_workers,
                progress_interval=args.progress_interval,
            )
            log.write(f"[RETURN:{mode}] {title}: {result.returncode}\n\n")
            log.flush()
            if result.returncode != 0:
                print(f"[failed:{mode}] {title}; details: {log.name}")
            else:
                print(f"[crawl:{mode}] done: {title}", flush=True)


def download_titles(args: argparse.Namespace) -> None:
    jobs = title_jobs(args)
    if not jobs:
        print("[download] no title files with entries; skipping download")
        return

    data_root = Path(args.lncrawl_data).expanduser().resolve()
    config_path = ensure_lncrawl_config(data_root, args)
    log_dir = Path(args.out).expanduser().resolve() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "lncrawl_batch.log"

    with log_path.open("a", encoding="utf-8") as log:
        print(f"[batch] files={len(jobs)} log={log_path}", flush=True)
        for title_path, mode in jobs:
            download_title_file(args, title_path, mode, data_root, config_path, log)


def repair_user_agent() -> str:
    return (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    )


def fetch_image_bytes(url: str, referer: str, timeout: int) -> bytes:
    headers = {
        "User-Agent": repair_user_agent(),
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.9",
        "Referer": referer,
    }
    origin = f"{urlparse(referer).scheme}://{urlparse(referer).netloc}" if referer else ""
    if origin:
        headers["Origin"] = origin
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def download_repair_image(job: RepairImageJob, attempts: int, timeout: int) -> tuple[str, bool, str]:
    if image_file_valid(job.output_file):
        return job.image_id, True, "already_valid"

    last_error = ""
    for attempt in range(1, attempts + 1):
        try:
            data = fetch_image_bytes(job.url, job.referer, timeout)
            try:
                output = jpeg_bytes_from_image(data)
            except Exception:
                meta = image_meta(data, Path(urlparse(job.url).path).suffix)
                if meta.width is None or meta.height is None:
                    raise
                output = data

            job.output_file.parent.mkdir(parents=True, exist_ok=True)
            job.output_file.write_bytes(output)
            if image_file_valid(job.output_file):
                return job.image_id, True, f"attempt={attempt}"
            last_error = "downloaded file failed validation"
        except (OSError, urllib.error.URLError, urllib.error.HTTPError, TimeoutError, Exception) as error:
            last_error = f"{type(error).__name__}: {error}"
            time.sleep(min(8, 0.6 * attempt))

    return job.image_id, False, last_error


def load_repair_jobs(connection: sqlite3.Connection, data_root: Path, args: argparse.Namespace) -> tuple[list[RepairImageJob], list[str]]:
    rows = connection.execute(
        """
        SELECT
            chapter_images.id AS image_id,
            chapter_images.url AS image_url,
            chapter_images.extra AS extra,
            chapter_images.is_done AS image_done,
            chapters.serial AS chapter_serial,
            novels.id AS novel_id,
            novels.title AS title,
            novels.url AS novel_url,
            novels.manga AS manga
        FROM chapter_images
        JOIN chapters ON chapters.id = chapter_images.chapter_id
        JOIN novels ON novels.id = chapter_images.novel_id
        ORDER BY novels.updated_at DESC, chapters.serial, chapter_images.created_at, chapter_images.id
        """
    ).fetchall()

    jobs: list[RepairImageJob] = []
    already_valid: list[str] = []
    for row in rows:
        if image_row_skipped(row):
            continue
        novel = {"manga": row["manga"]}
        if not include_novel_kind(novel, args.export_kind):
            continue
        if not row["manga"]:
            continue

        output_file = data_root / "novels" / row["novel_id"] / "images" / f"{row['image_id']}.jpg"
        if image_file_valid(output_file):
            if not row["image_done"]:
                already_valid.append(row["image_id"])
            continue

        jobs.append(
            RepairImageJob(
                image_id=row["image_id"],
                novel_id=row["novel_id"],
                title=row["title"],
                url=row["image_url"],
                referer=row["novel_url"],
                output_file=output_file,
                chapter_serial=int(row["chapter_serial"] or 0),
            )
        )

    return jobs, already_valid


def mark_images_done(connection: sqlite3.Connection, image_ids: list[str]) -> None:
    if not image_ids:
        return
    now = int(time.time() * 1000)
    connection.executemany(
        "UPDATE chapter_images SET is_done = 1, updated_at = ? WHERE id = ?",
        [(now, image_id) for image_id in image_ids],
    )
    connection.commit()


def repair_lncrawl_images(args: argparse.Namespace) -> None:
    data_root = Path(args.lncrawl_data).expanduser().resolve()
    db_path = data_root / "sqlite.db"
    if not db_path.is_file():
        print("[repair] no lncrawl database; skipping image repair")
        return

    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        jobs, already_valid = load_repair_jobs(connection, data_root, args)
        mark_images_done(connection, already_valid)
        if already_valid:
            print(f"[repair] marked existing valid images done: {len(already_valid)}")

        total = len(jobs)
        if not total:
            print("[repair] no missing manga images")
            return

        workers = min(max(1, args.repair_workers), 32)
        attempts = max(1, args.repair_attempts)
        timeout = max(5, args.repair_timeout)
        print(f"[repair] missing manga images={total} workers={workers} attempts={attempts}", flush=True)

        started = time.monotonic()
        last_print = started
        ok_ids: list[str] = []
        failures: list[tuple[str, str]] = []
        completed = 0

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(download_repair_image, job, attempts, timeout) for job in jobs]
            for future in concurrent.futures.as_completed(futures):
                image_id, ok, message = future.result()
                completed += 1
                if ok:
                    ok_ids.append(image_id)
                    if len(ok_ids) >= 100:
                        mark_images_done(connection, ok_ids)
                        ok_ids.clear()
                else:
                    failures.append((image_id, message))

                now = time.monotonic()
                if now - last_print >= args.progress_interval or completed == total:
                    elapsed = max(now - started, 1)
                    rate = completed / elapsed
                    remaining = total - completed
                    eta = f"{remaining / rate / 60:.0f} min" if rate > 0 and remaining else "done"
                    print(
                        f"[repair] images {completed}/{total} ({completed / total * 100:.1f}%), "
                        f"ok={completed - len(failures)} failed={len(failures)} "
                        f"speed={rate * 60:.1f} img/min eta={eta}",
                        flush=True,
                    )
                    last_print = now

        mark_images_done(connection, ok_ids)
        if failures:
            print(f"[repair] failed after retries: {len(failures)}")
            for image_id, message in failures[:20]:
                print(f"[repair:failed] {image_id}: {message}")
        else:
            print("[repair] all missing images repaired")
    finally:
        connection.close()


def is_safe_artifact_path(path: Path) -> bool:
    resolved = path.expanduser().resolve()
    if resolved == APP_DIR:
        return False
    try:
        resolved.relative_to(APP_DIR)
    except ValueError:
        return False
    return True


def remove_artifact_path(path: Path, label: str) -> None:
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        print(f"[fresh] skip missing {label}: {resolved}")
        return
    if not is_safe_artifact_path(resolved):
        raise SystemExit(f"[fresh] refuse to delete unsafe {label}: {resolved}")
    if resolved.is_dir():
        shutil.rmtree(resolved)
    else:
        resolved.unlink()
    print(f"[fresh] removed {label}: {resolved}")


def clean_artifacts(args: argparse.Namespace) -> None:
    requested_paths = [
        (Path(args.out), "export output"),
        (Path(args.lncrawl_data), "lncrawl cache/db"),
    ]
    paths: list[tuple[Path, str]] = []
    seen: set[Path] = set()
    for path, label in requested_paths:
        resolved = path.expanduser().resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        paths.append((resolved, label))

    for path, label in paths:
        if path.exists() and not is_safe_artifact_path(path):
            raise SystemExit(f"[fresh] refuse to delete unsafe {label}: {path}")

    for path, label in paths:
        remove_artifact_path(path, label)

    if FRESH_NEXT_RUN.is_file():
        FRESH_NEXT_RUN.unlink()
        print(f"[fresh] consumed marker: {FRESH_NEXT_RUN}")


def write_manifest(export_root: Path, results: list[NovelResult]) -> Path:
    manifest = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "raw_dir": (export_root / "data" / "10_raw").as_posix(),
        "photos_dir": (export_root / "photos").as_posix(),
        "novels": [asdict(result) for result in results],
    }
    path = export_root / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def write_validation_report(export_root: Path, results: list[NovelResult]) -> tuple[Path, Path]:
    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "summary": {
            "items": len(results),
            "ok": sum(1 for item in results if item.validation_status == "ok"),
            "warning": sum(1 for item in results if item.validation_status == "warning"),
            "failed": sum(1 for item in results if item.validation_status == "failed"),
            "not_checked": sum(1 for item in results if item.validation_status == "not_checked"),
        },
        "items": [
            {
                "slug": item.slug,
                "title": item.title,
                "kind": item.kind,
                "status": item.validation_status,
                "errors": item.validation_errors,
                "warnings": item.validation_warnings,
                "details": item.validation_details,
            }
            for item in results
        ],
    }
    json_path = export_root / "validation_report.json"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    lines = [
        f"generated_at: {report['generated_at']}",
        (
            "summary: "
            f"items={report['summary']['items']} "
            f"ok={report['summary']['ok']} "
            f"warning={report['summary']['warning']} "
            f"failed={report['summary']['failed']} "
            f"not_checked={report['summary']['not_checked']}"
        ),
        "",
    ]
    for item in report["items"]:
        lines.append(f"{item['slug']} [{item['kind']}] {item['status']}")
        for error in item["errors"][:20]:
            lines.append(f"  ERROR: {error}")
        for warning in item["warnings"][:20]:
            lines.append(f"  WARN : {warning}")
        details = item["details"]
        if details:
            lines.append(
                "  DETAILS: "
                f"chapters={details.get('db_chapters', 0)}/{details.get('expected_chapters', 0)} "
                f"images={details.get('done_image_rows', 0)}/{details.get('db_image_rows', 0)} "
                f"exported={details.get('exported_body_images', 0)} "
                f"missing_files={details.get('missing_image_files', 0)}"
            )
        lines.append("")

    text_path = export_root / "validation_report.txt"
    text_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, text_path


def parse_args() -> argparse.Namespace:
    if len(sys.argv) > 1:
        raise SystemExit(
            "[config] CLI options are disabled. Edit crawler_config.json and run "
            "crawl_dataset.py without arguments."
        )
    return args_from_config(load_crawler_config())


def main() -> int:
    args = parse_args()
    export_root = Path(args.out).expanduser().resolve()
    raw_root = export_root / "data" / "10_raw"
    photos_root = export_root / "photos"

    if args.fresh:
        clean_artifacts(args)

    raw_root.mkdir(parents=True, exist_ok=True)
    photos_root.mkdir(parents=True, exist_ok=True)

    if args.download:
        download_titles(args)
    if args.repair_images:
        repair_lncrawl_images(args)

    photos = PhotoStore(photos_root, args.max_photos_per_novel)
    used_slugs: set[str] = set()
    results: list[NovelResult] = []

    results.extend(export_lncrawl_db(args, raw_root, photos, used_slugs))

    manifest = write_manifest(export_root, results)
    validation_json, validation_text = write_validation_report(export_root, results)

    print("")
    print(f"raw txt : {raw_root}")
    print(f"photos  : {photos_root}")
    print(f"manifest: {manifest}")
    print(f"validate: {validation_text}")
    print(f"validate_json: {validation_json}")
    print(
        "summary : "
        f"novels={len(results)} "
        f"chapters={sum(item.chapters_written for item in results)} "
        f"dropped={sum(item.chapters_dropped for item in results)} "
        f"photos={sum(item.photos_written for item in results)} "
        f"covers={sum(item.covers_written for item in results)} "
        f"body_images={sum(item.body_images_written for item in results)} "
        f"body_dropped={sum(item.body_images_dropped for item in results)} "
        f"validation_failed={sum(1 for item in results if item.validation_status == 'failed')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
