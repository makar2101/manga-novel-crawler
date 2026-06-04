# Manga and Novel Dataset Crawler

Python utility for building a local manga/light-novel dataset with
[`lightnovel-crawler`](https://github.com/dipu-bd/lightnovel-crawler). It can:

- download manga/manhua/manhwa pages into a normalized image folder structure;
- export text novel chapters into plain `.txt` files;
- repair missing manga images from the crawler database;
- write a manifest and validation report for every run.

This repository contains code and safe examples only. Downloaded content, local
crawler cache, local title lists, IDE files, and virtual environments are
ignored by git.

## Repository Layout

```text
crawl_dataset.py                Main entrypoint.
run_lncrawl_with_workers.py     Wrapper that applies the LNCRAWL_WORKERS patch.
crawler_config.example.json     Safe config template for local runs.
titles_manga.example.txt        Example local manga title file.
titles_novel.example.txt        Example local novel title file.
requirements.txt                Runtime dependency list.
tests/                          Lightweight standard-library tests.
docs/publication-checklist.md   Checklist before publishing or releasing.
```

Runtime artifacts are written under `output/` and `.lncrawl_data/` by default.
These directories can be large and may contain copyrighted material, so they
must stay out of git.

## Setup

Use Python 3.10 or newer.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
cp crawler_config.example.json crawler_config.json
cp titles_manga.example.txt titles_manga.txt
cp titles_novel.example.txt titles_novel.txt
```

Edit `titles_manga.txt` and/or `titles_novel.txt`. Add one title or direct URL
per line. Blank lines and lines starting with `#` are ignored.

Then run:

```bash
.venv/bin/python crawl_dataset.py
```

The script intentionally does not accept CLI options. Change
`crawler_config.json` instead, or point to another config file:

```bash
CRAWLER_CONFIG=path/to/config.json .venv/bin/python crawl_dataset.py
```

## Configuration

The default config has separate blocks for manga and novels:

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

`content_type` values:

- `manga`: export image pages.
- `novel`: export text chapters.
- `auto`: search/export both kinds from the same input file.

`chapters` values:

- `3`: download/export only the first 3 chapters.
- `25`: download/export only the first 25 chapters.
- `"all"`: download/export every available chapter.

Set `"fresh_start": true` to remove the configured `output/` and
`.lncrawl_data/` paths before the next run. The script refuses to delete paths
outside the project directory.

## Output

Manga pages:

```text
output/manga_dataset/photos/<title-slug>/chapter-00001/
```

Text novel chapters:

```text
output/manga_dataset/data/10_raw/<title-slug>/00001.txt
```

Run metadata:

```text
output/manga_dataset/manifest.json
output/manga_dataset/validation_report.json
output/manga_dataset/validation_report.txt
```

## Verification

Run local checks before committing:

```bash
python3 -m py_compile crawl_dataset.py run_lncrawl_with_workers.py
python3 -m unittest discover -s tests
```

## Public Repository Safety

- Do not commit `output/`, `.lncrawl_data/`, `.venv/`, `.idea/`,
  `__pycache__/`, local configs, or local title files.
- Do not publish downloaded manga pages, novel text, crawler databases, or logs
  unless you have the required rights.
- Review source-site terms of service and robots policies before crawling.
- Keep crawl limits conservative in shared examples. Use `"chapters": "all"`
  only when you explicitly want a full download.

## License

No license has been selected yet. Add a `LICENSE` file before inviting external
reuse or contributions.
