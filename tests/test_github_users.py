import asyncio
import json
import unittest
from unittest import mock

from backend.app import (
    CommandResult,
    _github_accounts,
    _github_repositories,
    _github_repository_url,
    _repository_owner,
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


if __name__ == "__main__":
    unittest.main()
