# Notes

This is a small personal crawler project, not a formal open-source package.

Before changing or pushing code:

```bash
python3 -m py_compile crawl_dataset.py run_lncrawl_with_workers.py
python3 -m unittest discover -s tests
git status --short --ignored
```

Keep generated data, crawler cache, local configs, title files, logs, and
databases out of git.
