# Publication Checklist

Use this checklist before pushing the project to a public Git host.

## Required Checks

```bash
python3 -m py_compile crawl_dataset.py run_lncrawl_with_workers.py
python3 -m unittest discover -s tests
```

## Files That Must Stay Ignored

```bash
git check-ignore -v output .lncrawl_data .venv .idea __pycache__ \
  crawler_config.json titles_manga.txt titles_novel.txt
```

Expected result: every path above is ignored by `.gitignore`.

## Manual Review

- Confirm `git status --short --ignored` does not show generated data as staged.
- Confirm no real API keys, cookies, passwords, or local absolute paths are
  present in tracked files.
- Confirm `output/` and `.lncrawl_data/` are not committed.
- Decide which license to use and add a `LICENSE` file before inviting external
  reuse.
- Review source-site terms and copyright requirements before publishing any
  downloaded dataset outside the repository.
