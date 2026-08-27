import tempfile
import unittest
from pathlib import Path

import json

from backend.app import _parse_add_files, _write_repo_file


class AddFileTests(unittest.TestCase):
    def test_parses_multiple_files_and_ignores_empty_entries(self) -> None:
        files = _parse_add_files({"files": json.dumps([
            {"path": "first.txt", "content": "first", "overwrite": False},
            {"path": "", "content": "ignored", "overwrite": True},
            {"path": "empty.txt", "content": "", "overwrite": True},
            {"path": "second.txt", "content": "second", "overwrite": True},
        ])})

        self.assertEqual(files, [
            {"path": "first.txt", "content": "first", "overwrite": False},
            {"path": "second.txt", "content": "second", "overwrite": True},
        ])

    def test_legacy_empty_name_or_content_is_ignored(self) -> None:
        self.assertEqual(_parse_add_files({"file_path": "file.txt", "file_content": ""}), [])
        self.assertEqual(_parse_add_files({"file_path": "", "file_content": "content"}), [])

    def test_creates_parent_directories_and_writes_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            result = _write_repo_file(repo, "nested/example.txt", "hello\nworld", False)

            self.assertEqual(result.read_text(encoding="utf-8"), "hello\nworld")

    def test_existing_file_requires_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            target = repo / "example.txt"
            target.write_text("original", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "overwrite is disabled"):
                _write_repo_file(repo, "example.txt", "replacement", False)
            self.assertEqual(target.read_text(encoding="utf-8"), "original")

            _write_repo_file(repo, "example.txt", "replacement", True)
            self.assertEqual(target.read_text(encoding="utf-8"), "replacement")

    def test_rejects_paths_outside_repository(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()

            with self.assertRaisesRegex(RuntimeError, "inside the repository"):
                _write_repo_file(repo, "../outside.txt", "content", False)


if __name__ == "__main__":
    unittest.main()
