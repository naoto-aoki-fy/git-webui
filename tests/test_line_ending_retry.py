import asyncio
import subprocess
import tempfile
import unittest
from pathlib import Path

from backend.app import LogSink, _apply_patch_with_line_ending_retry


class LineEndingRetryTests(unittest.TestCase):
    def test_git_apply_retry_normalizes_and_restores_crlf_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir) / "repo"
            subprocess.run(
                ["git", "init", str(repo_dir)],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"],
                cwd=repo_dir,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Test User"],
                cwd=repo_dir,
                check=True,
            )

            target = repo_dir / "file.txt"
            target.write_bytes(b"one\r\ntwo\r\nthree\r\n")
            subprocess.run(["git", "add", "file.txt"], cwd=repo_dir, check=True)
            subprocess.run(
                ["git", "commit", "-m", "initial"],
                cwd=repo_dir,
                check=True,
                stdout=subprocess.PIPE,
            )

            patch_content = """diff --git a/file.txt b/file.txt
index 4cb29ea..8d17622 100644
--- a/file.txt
+++ b/file.txt
@@ -1,3 +1,3 @@
 one
-two
+TWO
 three
"""
            patch_path = Path(tmpdir) / "patch.diff"
            patch_path.write_text(patch_content, encoding="utf-8", newline="\n")

            logs = LogSink([])
            asyncio.run(
                _apply_patch_with_line_ending_retry(repo_dir, patch_path, patch_content, {}, logs)
            )

            self.assertEqual(target.read_bytes(), b"one\r\nTWO\r\nthree\r\n")
            self.assertIn("retrying with LF-normalized inputs", "\n".join(logs.entries))


if __name__ == "__main__":
    unittest.main()
