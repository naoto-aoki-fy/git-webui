import tempfile
import unittest
from pathlib import Path

from backend.app import _write_repo_file


class AddFileTests(unittest.TestCase):
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
