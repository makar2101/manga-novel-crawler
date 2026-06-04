#!/usr/bin/env python3
"""Run lncrawl after applying a project-local worker-count patch."""

from __future__ import annotations

import os


def patch_lncrawl_workers() -> None:
    from lncrawl.services.sources.service import Sources

    original = Sources.init_crawler

    def init_crawler(self, constructor, disable_logger=True, workers=None, parser=None):  # noqa: ANN001
        if workers is None:
            workers = int(os.getenv("LNCRAWL_WORKERS", "16"))
        return original(self, constructor, disable_logger=disable_logger, workers=workers, parser=parser)

    Sources.init_crawler = init_crawler


if __name__ == "__main__":
    patch_lncrawl_workers()

    from lncrawl import main

    main()
