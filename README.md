# Manga Novel Crawler

[![CI](https://github.com/makar2101/manga-novel-crawler/actions/workflows/ci.yml/badge.svg)](https://github.com/makar2101/manga-novel-crawler/actions/workflows/ci.yml)

Personal Python script for collecting manga/manhua/manhwa pages and light-novel
chapters into a local dataset. It is built around
[`lightnovel-crawler`](https://github.com/lncrawl/lightnovel-crawler).

This repo is for the code only. Downloaded content, crawler cache, local config,
title lists, logs, and virtual environments are intentionally ignored.

## What It Produces

```text
output/manga_dataset/photos/<title>/chapter-00001/   manga page images
output/manga_dataset/data/10_raw/<title>/00001.txt   novel chapter text
output/manga_dataset/manifest.json                   run manifest
output/manga_dataset/validation_report.txt           validation summary
```

## Setup

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
cp crawler_config.example.json crawler_config.json
cp titles_manga.example.txt titles_manga.txt
cp titles_novel.example.txt titles_novel.txt
```

Add one title or direct URL per line to `titles_manga.txt` or
`titles_novel.txt`, then run:

```bash
.venv/bin/python crawl_dataset.py
```

The script does not use CLI flags. Edit `crawler_config.json` instead.

## Config

Default example:

```json
{
  "manga": {
    "input_file": "titles_manga.txt",
    "content_type": "manga",
    "chapters": 3,
    "fresh_start": false
  },
  "novel": {
    "input_file": "titles_novel.txt",
    "content_type": "novel",
    "chapters": 3,
    "fresh_start": false
  }
}
```

Useful values:

- `content_type`: `manga`, `novel`, or `auto`.
- `chapters`: a number like `3` / `25`, or `"all"`.
- `fresh_start`: `true` removes the configured `output/` and `.lncrawl_data/`
  before the next run.

## Files

```text
crawl_dataset.py                main script
run_lncrawl_with_workers.py     lightnovel-crawler worker patch
crawler_config.example.json     safe config example
titles_manga.example.txt        title-list example
titles_novel.example.txt        title-list example
requirements.txt                dependencies
tests/                          small smoke tests
```

## Do Not Commit

These are local/generated files and should stay out of git:

```text
output/
.lncrawl_data/
.venv/
.idea/
__pycache__/
crawler_config.json
titles_manga.txt
titles_novel.txt
*.log
*.db
```

Do not publish downloaded manga pages, novel text, crawler databases, or logs
unless you have the rights to share them.

## Checks

```bash
python3 -m py_compile crawl_dataset.py run_lncrawl_with_workers.py
python3 -m unittest discover -s tests
```

## License

No license is selected yet.
