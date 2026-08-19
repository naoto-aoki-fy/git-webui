import asyncio
import json
import unittest
from unittest import mock

from backend import app
from backend.app import CommandResult, _is_supported_repository_url, _parse_gh_hosts, run_git_command


class GithubAuthTests(unittest.TestCase):
    def test_parses_users_and_active_user(self) -> None:
        payload = json.dumps({"hosts": {"github.com": [
            {"login": "owner", "active": False},
            {"login": "current", "active": True},
        ]}})
        self.assertEqual(_parse_gh_hosts(payload), (["owner", "current"], "current"))

    def test_network_command_switches_and_restores_account(self) -> None:
        calls = []

        async def fake_run(*command, **kwargs):
            calls.append(command)
            if command[:4] == ("gh", "auth", "status", "--json"):
                payload = json.dumps({"hosts": {"github.com": [
                    {"login": "owner", "active": False},
                    {"login": "current", "active": True},
                ]}})
                return CommandResult(0, payload, "")
            return CommandResult(0, "", "")

        env = {"GIT_WEBUI_GITHUB_USERNAME": "owner"}
        with mock.patch.object(app, "run_command", side_effect=fake_run):
            asyncio.run(run_git_command("push", "origin", "HEAD", env=env))

        self.assertEqual(calls[1][:4], ("gh", "auth", "switch", "--hostname"))
        self.assertEqual(calls[2][0], "git")
        self.assertEqual(calls[3][-1], "current")

    def test_only_https_remote_urls_are_supported(self) -> None:
        self.assertTrue(_is_supported_repository_url("https://github.com/owner/repo.git"))
        self.assertTrue(_is_supported_repository_url("/tmp/local-repo.git"))
        self.assertFalse(_is_supported_repository_url("git@github.com:owner/repo.git"))
        self.assertFalse(_is_supported_repository_url("ssh://git@github.com/owner/repo.git"))


if __name__ == "__main__":
    unittest.main()
