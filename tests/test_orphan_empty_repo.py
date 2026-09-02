import asyncio
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend import app
from backend.app import LogSink, _repo_workspace_for_url, process_submission


class OrphanEmptyRepoTests(unittest.TestCase):
    def test_orphan_mode_deletes_all_existing_local_branches(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            remote_dir = tmp_path / "remote.git"
            source_dir = tmp_path / "source"
            repo_root = tmp_path / "repos"
            subprocess.run(
                ["git", "init", "--bare", str(remote_dir)],
                check=True,
                stdout=subprocess.PIPE,
            )
            subprocess.run(
                ["git", "init", str(source_dir)], check=True, stdout=subprocess.PIPE
            )
            subprocess.run(
                ["git", "config", "user.name", "Test User"], cwd=source_dir, check=True
            )
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"],
                cwd=source_dir,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "--allow-empty", "-m", "initial"],
                cwd=source_dir,
                check=True,
                stdout=subprocess.PIPE,
            )
            subprocess.run(["git", "branch", "feature"], cwd=source_dir, check=True)
            subprocess.run(
                ["git", "remote", "add", "origin", str(remote_dir)],
                cwd=source_dir,
                check=True,
            )
            subprocess.run(
                ["git", "push", "origin", "HEAD:main", "feature"],
                cwd=source_dir,
                check=True,
                stdout=subprocess.PIPE,
            )
            subprocess.run(
                ["git", "symbolic-ref", "HEAD", "refs/heads/main"],
                cwd=remote_dir,
                check=True,
            )

            repository_url = str(remote_dir)
            repo_root.mkdir()
            app_config = {"git_user_defaults": []}
            github_listing = mock.AsyncMock(return_value={"github_users": [{"id": 1, "login": "test-user", "name": "Test User", "email": "1+test-user@users.noreply.github.com"}]})
            with (
                mock.patch.object(app, "REPO_ROOT", repo_root),
                mock.patch.object(app, "APP_CONFIG", app_config),
                mock.patch.object(app, "_github_listing", github_listing),
            ):
                cached_repo = _repo_workspace_for_url(repository_url)
                subprocess.run(
                    ["git", "clone", "--mirror", repository_url, str(cached_repo)],
                    check=True,
                    stdout=subprocess.PIPE,
                )
                subprocess.run(
                    ["git", "update-ref", "refs/heads/stale-local", "refs/heads/main"],
                    cwd=cached_repo,
                    check=True,
                )
                logs = LogSink([])
                result = asyncio.run(
                    process_submission(
                        {
                            "repository_url": repository_url,
                            "branch_mode": "orphan",
                            "new_branch": "fresh-orphan",
                            "commit_message": "Orphan commit",
                            "allow_empty_commit": "true",
                            "git_user": "test-user",
                        },
                        logs,
                    )
                )

            self.assertTrue(result["success"], "\n".join(logs.entries))
            cached_branches = subprocess.run(
                ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/"],
                cwd=cached_repo,
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout.splitlines()
            self.assertEqual(["feature", "main"], cached_branches)
            self.assertEqual(
                "true",
                subprocess.run(
                    ["git", "rev-parse", "--is-bare-repository"],
                    cwd=cached_repo,
                    check=True,
                    stdout=subprocess.PIPE,
                    text=True,
                ).stdout.strip(),
            )
            self.assertIn(
                "Deleting all local branches before orphan branch creation.",
                "\n".join(logs.entries),
            )

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
            app_config = {"git_user_defaults": []}
            github_listing = mock.AsyncMock(return_value={"github_users": [{"id": 1, "login": "test-user", "name": "Test User", "email": "1+test-user@users.noreply.github.com"}]})
            with (
                mock.patch.object(app, "REPO_ROOT", repo_root),
                mock.patch.object(app, "APP_CONFIG", app_config),
                mock.patch.object(app, "_github_listing", github_listing),
            ):
                cached_repo = _repo_workspace_for_url(repository_url)
                subprocess.run(
                    ["git", "clone", "--mirror", repository_url, str(cached_repo)],
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
                            "git_user": "test-user",
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
