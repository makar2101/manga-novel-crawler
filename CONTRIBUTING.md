# Contributing

## Local Setup

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
cp crawler_config.example.json crawler_config.json
cp titles_manga.example.txt titles_manga.txt
cp titles_novel.example.txt titles_novel.txt
```

## Checks

Run these before opening a pull request:

```bash
python3 -m py_compile crawl_dataset.py run_lncrawl_with_workers.py
python3 -m unittest discover -s tests
```

## Data Policy

Keep repository changes limited to source code, docs, tests, and safe examples.
Do not commit downloaded pages, novel text, crawler caches, local databases,
logs, secrets, or personal input files.

## Code Style

- Prefer small, focused changes.
- Keep runtime defaults conservative.
- Keep local paths configurable and relative to the project root when possible.
- Add tests for config parsing, path safety, and output-shaping logic when those
  areas change.
