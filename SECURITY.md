# Security

## Public Data Rules

This project is a crawler/exporter. Generated data can contain copyrighted
content and source-site metadata. Do not commit:

- `output/`
- `.lncrawl_data/`
- local `crawler_config.json`
- local `titles_manga.txt` or `titles_novel.txt`
- logs, SQLite databases, archives, screenshots, or downloaded media

The repository `.gitignore` blocks these by default. If you force-add ignored
files, review them manually first.

## Secrets

The project does not require API keys for normal use. If a local config or
environment file ever contains a token, password, cookie, or private URL, keep it
out of git and rotate it immediately if it was exposed.

## Crawling Responsibility

Before crawling a source, review its terms, rate limits, and robots policies.
Use conservative chapter limits for testing and avoid publishing downloaded
content unless you have the required rights.
