import asyncio
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from backend.app import (
    CommandResult,
    _github_accounts,
    _github_listing,
    _github_repositories,
    _github_repository_url,
    _read_github_cache,
    _repository_owner,
    _write_github_cache,
)


class GithubUsersTests(unittest.TestCase):
    def test_repository_url_is_built_from_owner_and_name(self) -> None:
        self.assertEqual(
            _github_repository_url("Example", "project"),
            "https://github.com/Example/project.git",
        )

    def test_repository_identifiers_cannot_contain_path_segments(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid characters"):
            _github_repository_url("Example/other", "project")

    def test_https_repository_owner(self) -> None:
        self.assertEqual(_repository_owner("https://github.com/Example/project.git"), "example")

    def test_accounts_are_read_from_gh_auth_status(self) -> None:
        response = CommandResult(
            0,
            json.dumps({"hosts": {"github.com": [
                {"login": "octocat", "active": False},
                {"login": "work-user", "active": True},
            ]}}),
            "",
        )
        with mock.patch("backend.app.run_command", mock.AsyncMock(return_value=response)):
            users, active = asyncio.run(_github_accounts())
        self.assertEqual(users, ["octocat", "work-user"])
        self.assertEqual(active, "work-user")

    def test_repositories_are_listed_after_switching_each_user_and_active_user_is_restored(self) -> None:
        responses = [
            CommandResult(0, "", ""),
            CommandResult(0, json.dumps([{"nameWithOwner": "octocat/hello"}]), ""),
            CommandResult(0, "", ""),
            CommandResult(0, json.dumps([{"nameWithOwner": "Work/secret"}]), ""),
            CommandResult(0, "", ""),
        ]
        command = mock.AsyncMock(side_effect=responses)
        with mock.patch("backend.app.run_command", command):
            repositories = asyncio.run(_github_repositories(["octocat", "work-user"], "work-user"))

        self.assertEqual(
            repositories,
            [{"owner": "octocat", "name": "hello"}, {"owner": "Work", "name": "secret"}],
        )
        self.assertEqual(
            [call.args for call in command.await_args_list],
            [
                ("gh", "auth", "switch", "--hostname", "github.com", "--user", "octocat"),
                ("gh", "repo", "list", "--limit", "1000", "--json", "nameWithOwner"),
                ("gh", "auth", "switch", "--hostname", "github.com", "--user", "work-user"),
                ("gh", "repo", "list", "--limit", "1000", "--json", "nameWithOwner"),
                ("gh", "auth", "switch", "--hostname", "github.com", "--user", "work-user"),
            ],
        )

    def test_active_user_is_restored_when_repository_listing_fails(self) -> None:
        command = mock.AsyncMock(
            side_effect=[
                CommandResult(0, "", ""),
                CommandResult(1, "", "failed"),
                CommandResult(0, "", ""),
            ]
        )
        with mock.patch("backend.app.run_command", command):
            with self.assertRaisesRegex(RuntimeError, "Unable to list"):
                asyncio.run(_github_repositories(["octocat"], "work-user"))
        self.assertEqual(command.await_args_list[-1].args[-1], "work-user")

    def test_github_listing_uses_fresh_disk_cache_without_running_gh(self) -> None:
        payload = {
            "github_users": ["octocat"],
            "active_github_user": "octocat",
            "github_repositories": [{"owner": "octocat", "name": "hello"}],
        }
        with tempfile.TemporaryDirectory() as tmpdir, mock.patch(
            "backend.app.REPO_ROOT", Path(tmpdir)
        ), mock.patch("backend.app.run_command", mock.AsyncMock()) as command:
            _write_github_cache(payload, datetime.now(UTC))
            listing = asyncio.run(_github_listing())

        self.assertEqual(listing["github_users"], ["octocat"])
        self.assertEqual(listing["github_repositories"], payload["github_repositories"])
        command.assert_not_awaited()

    def test_cache_is_stale_after_one_day(self) -> None:
        refreshed_at = datetime(2026, 1, 1, tzinfo=UTC)
        payload = {
            "github_users": ["octocat"],
            "active_github_user": "octocat",
            "github_repositories": [],
        }
        with tempfile.TemporaryDirectory() as tmpdir, mock.patch(
            "backend.app.REPO_ROOT", Path(tmpdir)
        ):
            _write_github_cache(payload, refreshed_at)
            self.assertIsNotNone(_read_github_cache(refreshed_at + timedelta(hours=23)))
            self.assertIsNone(_read_github_cache(refreshed_at + timedelta(days=1)))

    def test_forced_refresh_replaces_existing_cache(self) -> None:
        old_payload = {
            "github_users": ["old-user"],
            "active_github_user": "old-user",
            "github_repositories": [],
        }
        with tempfile.TemporaryDirectory() as tmpdir, mock.patch(
            "backend.app.REPO_ROOT", Path(tmpdir)
        ):
            _write_github_cache(old_payload, datetime.now(UTC))
            with mock.patch(
                "backend.app._github_accounts", mock.AsyncMock(return_value=(["new-user"], "new-user"))
            ) as accounts, mock.patch(
                "backend.app._github_repositories",
                mock.AsyncMock(return_value=[{"owner": "new-user", "name": "project"}]),
            ):
                listing = asyncio.run(_github_listing(force_refresh=True))

        accounts.assert_awaited_once()
        self.assertEqual(listing["github_users"], ["new-user"])


if __name__ == "__main__":
    unittest.main()
