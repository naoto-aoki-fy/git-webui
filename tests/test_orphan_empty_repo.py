import asyncio
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend import app
from backend.app import LogSink, _repo_workspace_for_url, process_submission


class OrphanEmptyRepoTests(unittest.TestCase):
    def test_existing_empty_cached_repo_can_create_orphan_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            remote_dir = tmp_path / "remote.git"
            repo_root = tmp_path / "repos"
            subprocess.run(
                ["git", "init", "--bare", str(remote_dir)],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            repository_url = str(remote_dir)
            repo_root.mkdir()
            app_config = {
                "git_users": [{"name": "Test User", "email": "test@example.com"}],
            }
            with (
                mock.patch.object(app, "REPO_ROOT", repo_root),
                mock.patch.object(app, "APP_CONFIG", app_config),
            ):
                cached_repo = _repo_workspace_for_url(repository_url)
                subprocess.run(
                    ["git", "clone", repository_url, str(cached_repo)],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )

                logs = LogSink([])
                result = asyncio.run(
                    process_submission(
                        {
                            "repository_url": repository_url,
                            "branch_mode": "orphan",
                            "new_branch": "first-branch",
                            "commit_message": "Initial orphan commit",
                            "allow_empty_commit": "true",
                            "git_user": "0",
                        },
                        logs,
                    )
                )

            self.assertTrue(result["success"], "\n".join(logs.entries))
            self.assertIn(
                "Origin has no branches; skipping default branch resolution for orphan creation.",
                "\n".join(logs.entries),
            )
            verify = subprocess.run(
                ["git", "ls-remote", "--heads", repository_url, "first-branch"],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            )
            self.assertIn("refs/heads/first-branch", verify.stdout)


if __name__ == "__main__":
    unittest.main()
