import asyncio
import json
import unittest
from unittest import mock

from backend.app import CommandResult, _github_accounts, _repository_owner


class GithubUsersTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
