# Security Notes

This project should not contain secrets or downloaded datasets.

Do not commit:

```text
output/
.lncrawl_data/
crawler_config.json
titles_manga.txt
titles_novel.txt
*.log
*.db
```

If a token, cookie, private URL, database, or downloaded content is committed by
mistake, remove it, rotate any exposed secret, and rewrite public git history if
needed.
