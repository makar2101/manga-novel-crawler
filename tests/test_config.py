import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("crawl_dataset", PROJECT_ROOT / "crawl_dataset.py")
crawl_dataset = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = crawl_dataset
SPEC.loader.exec_module(crawl_dataset)


class ConfigTests(unittest.TestCase):
    def test_read_titles_ignores_comments_and_blank_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            title_file = Path(tmpdir) / "titles.txt"
            title_file.write_text(
                "\n# comment\n  example title  \nhttps://example.com/item\n",
                encoding="utf-8",
            )

            self.assertEqual(
                crawl_dataset.read_titles(title_file),
                ["example title", "https://example.com/item"],
            )

    def test_dual_config_expands_into_separate_inputs(self) -> None:
        config = {
            "manga": {
                "input_file": "manga_titles.txt",
                "content_type": "manga",
                "chapters": 2,
                "fresh_start": False,
            },
            "novel": {
                "input_file": "novel_titles.txt",
                "content_type": "novel",
                "chapters": "all",
                "fresh_start": False,
            },
        }

        expanded = crawl_dataset.expand_crawler_config(config)

        self.assertEqual(expanded["input"]["manga_titles"], ["manga_titles.txt"])
        self.assertEqual(expanded["input"]["novel_titles"], ["novel_titles.txt"])
        self.assertEqual(expanded["download"]["chapter_limits"]["manga"], 2)
        self.assertEqual(expanded["download"]["chapter_limits"]["novel"], 0)
        self.assertEqual(expanded["processing"]["export_kind"], "auto")

    def test_runtime_templates_are_conservative(self) -> None:
        self.assertEqual(crawl_dataset.SIMPLE_CONFIG_TEMPLATE["chapters"], 3)
        self.assertEqual(crawl_dataset.DUAL_CONFIG_TEMPLATE["manga"]["chapters"], 3)
        self.assertEqual(crawl_dataset.DUAL_CONFIG_TEMPLATE["novel"]["chapters"], 3)


if __name__ == "__main__":
    unittest.main()
