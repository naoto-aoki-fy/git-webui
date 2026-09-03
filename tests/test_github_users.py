import asyncio
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from backend.app import (
    CommandResult,
    LogSink,
    _auto_select_git_user,
    _github_accounts,
    _github_listing,
    _github_repositories,
    _github_repository_url,
    _github_user_profiles,
    _read_github_cache,
    _repository_owner,
    _write_github_cache,
    config_handler,
    create_app,
    process_submission,
    server_started,
)
from backend.git_webui.web.keys import GITHUB_REFRESH_TASK_KEY


class GithubUsersTests(unittest.TestCase):
    def test_non_committing_modes_do_not_load_github_users_or_configure_author(self) -> None:
        forms = (
            {"repository_url": "https://github.com/example/repo.git", "branch_mode": "from_commit", "base_commit": "abc123", "new_branch": "topic"},
            {"repository_url": "https://github.com/example/repo.git", "branch_mode": "merge_branches", "branch": "main", "new_branch": "topic"},
        )
        for form in forms:
            with self.subTest(branch_mode=form["branch_mode"]):
                async def run_git(*args, **kwargs):
                    return CommandResult(1 if args[:2] == ("ls-remote", "--exit-code") else 0, "", "")

                command = mock.AsyncMock(side_effect=run_git)
                listing = mock.AsyncMock(side_effect=AssertionError("GitHub users are not needed"))
                logs = LogSink([])
                with (
                    mock.patch("backend.app.APP_CONFIG", {"repository_prefix_destinations": []}),
                    mock.patch("backend.app._github_listing", listing),
                    mock.patch("backend.app._clone_isolated_repository", mock.AsyncMock()),
                    mock.patch("backend.app.run_git_command", command),
                ):
                    result = asyncio.run(process_submission(form, logs))

                self.assertTrue(result["success"], "\n".join(logs.entries))
                listing.assert_not_awaited()
                self.assertFalse(any(
                    call.args[:2] == ("config", "--local")
                    for call in command.await_args_list
                ))

    def test_auto_select_prefers_repository_owner(self) -> None:
        users = [
            {"login": "first", "name": "First", "email": "first@example.com"},
            {"login": "Example", "name": "Owner", "email": "owner@example.com"},
        ]
        with mock.patch("backend.app.APP_CONFIG", {"repository_prefix_destinations": [], "git_user_defaults": []}):
            selected = _auto_select_git_user("example", "project", users)

        self.assertIs(selected, users[1])

    def test_auto_select_prefers_additional_repository_owner(self) -> None:
        users = [
            {"login": "source", "name": "Source", "email": "source@example.com"},
            {"login": "destination", "name": "Destination", "email": "destination@example.com"},
        ]
        config = {
            "repository_prefix_destinations": [["upstream-", "destination"]],
            "git_user_defaults": [],
        }
        with mock.patch("backend.app.APP_CONFIG", config):
            selected = _auto_select_git_user("source", "upstream-project", users)

        self.assertIs(selected, users[1])

    def test_auto_select_falls_back_to_first_configured_user(self) -> None:
        users = [{"login": "first", "name": "First", "email": "first@example.com"}]
        with mock.patch("backend.app.APP_CONFIG", {"repository_prefix_destinations": [], "git_user_defaults": []}):
            selected = _auto_select_git_user("unconfigured", "project", users)

        self.assertIs(selected, users[0])

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

    def test_repositories_are_listed_with_per_user_tokens_without_switching(self) -> None:
        command = mock.AsyncMock(side_effect=[
            CommandResult(0, "octocat-token\n", ""),
            CommandResult(0, json.dumps([{"nameWithOwner": "octocat/hello"}]), ""),
            CommandResult(0, "work-token\n", ""),
            CommandResult(0, json.dumps([{"nameWithOwner": "Work/secret"}]), ""),
        ])
        with mock.patch("backend.app.run_command", command):
            repositories = asyncio.run(_github_repositories(["octocat", "work-user"]))

        self.assertEqual(
            repositories,
            [{"owner": "octocat", "name": "hello"}, {"owner": "Work", "name": "secret"}],
        )
        self.assertEqual(command.await_args_list[0].args, ("gh", "auth", "token", "--user", "octocat"))
        self.assertEqual(command.await_args_list[2].args, ("gh", "auth", "token", "--user", "work-user"))
        self.assertEqual(command.await_args_list[1].kwargs["env"]["GH_TOKEN"], "octocat-token")
        self.assertEqual(command.await_args_list[3].kwargs["env"]["GH_TOKEN"], "work-token")
        self.assertFalse(any(call.args[:3] == ("gh", "auth", "switch") for call in command.await_args_list))

    def test_profiles_supply_github_name_and_pseudo_email(self) -> None:
        command = mock.AsyncMock(side_effect=[
            CommandResult(0, "token\n", ""),
            CommandResult(0, json.dumps({"id": 42, "login": "octocat", "name": "The Octocat"}), ""),
        ])
        with mock.patch("backend.app.run_command", command):
            profiles = asyncio.run(_github_user_profiles(["octocat"]))

        self.assertEqual(profiles, [{
            "id": 42,
            "login": "octocat",
            "name": "The Octocat",
            "email": "42+octocat@users.noreply.github.com",
        }])
        self.assertEqual(command.await_args_list[1].kwargs["env"]["GH_TOKEN"], "token")

    def test_repository_listing_fails_when_user_token_is_unavailable(self) -> None:
        command = mock.AsyncMock(return_value=CommandResult(1, "", "failed"))
        with mock.patch("backend.app.run_command", command):
            with self.assertRaisesRegex(RuntimeError, "Unable to retrieve GitHub token"):
                asyncio.run(_github_repositories(["octocat"]))

    def test_github_listing_uses_fresh_disk_cache_without_running_gh(self) -> None:
        payload = {
            "github_users": [{"id": 1, "login": "octocat", "name": "Octo Cat", "email": "1+octocat@users.noreply.github.com"}],
            "active_github_user": "octocat",
            "github_repositories": [{"owner": "octocat", "name": "hello"}],
        }
        with tempfile.TemporaryDirectory() as tmpdir, mock.patch(
            "backend.app.REPO_ROOT", Path(tmpdir)
        ), mock.patch("backend.app.run_command", mock.AsyncMock()) as command:
            _write_github_cache(payload, datetime.now(UTC))
            listing = asyncio.run(_github_listing())

        self.assertEqual(listing["github_users"][0]["login"], "octocat")
        self.assertEqual(listing["github_repositories"], payload["github_repositories"])
        command.assert_not_awaited()

    def test_cache_does_not_expire_automatically(self) -> None:
        refreshed_at = datetime(2026, 1, 1, tzinfo=UTC)
        payload = {
            "github_users": [{"id": 1, "login": "octocat", "name": "Octo Cat", "email": "1+octocat@users.noreply.github.com"}],
            "active_github_user": "octocat",
            "github_repositories": [],
        }
        with tempfile.TemporaryDirectory() as tmpdir, mock.patch(
            "backend.app.REPO_ROOT", Path(tmpdir)
        ):
            _write_github_cache(payload, refreshed_at)
            self.assertIsNotNone(_read_github_cache())

    def test_forced_refresh_replaces_existing_cache(self) -> None:
        old_payload = {
            "github_users": [{"id": 1, "login": "old-user", "name": "Old", "email": "1+old-user@users.noreply.github.com"}],
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
                "backend.app._github_user_profiles",
                mock.AsyncMock(return_value=[{"id": 2, "login": "new-user", "name": "New", "email": "2+new-user@users.noreply.github.com"}]),
            ), mock.patch(
                "backend.app._github_repositories",
                mock.AsyncMock(return_value=[{"owner": "new-user", "name": "project"}]),
            ):
                listing = asyncio.run(_github_listing(force_refresh=True))

        accounts.assert_awaited_once()
        self.assertEqual(listing["github_users"][0]["login"], "new-user")

    def test_server_started_forces_refresh_in_background(self) -> None:
        async def exercise() -> None:
            app = create_app(serve_frontend=False)
            with mock.patch(
                "backend.app._github_listing", mock.AsyncMock(return_value={})
            ) as listing:
                with mock.patch("builtins.print") as output:
                    server_started(app, "Listening")
                    output.assert_called_once_with("Listening")
                await app[GITHUB_REFRESH_TASK_KEY]["task"]
                listing.assert_awaited_once_with(force_refresh=True)

        asyncio.run(exercise())

    def test_config_waits_for_startup_refresh_before_reading_cache(self) -> None:
        async def exercise() -> None:
            app = create_app(serve_frontend=False)
            refresh_started = asyncio.Event()
            allow_refresh = asyncio.Event()
            refreshed_listing = {
                "github_users": [{"id": 2, "login": "new-user", "name": "New", "email": "2+new-user@users.noreply.github.com"}],
                "active_github_user": "new-user",
                "github_repositories": [],
            }

            async def listing(force_refresh: bool = False) -> dict[str, object]:
                if force_refresh:
                    refresh_started.set()
                    await allow_refresh.wait()
                    return refreshed_listing
                return refreshed_listing

            with mock.patch("backend.app._github_listing", mock.AsyncMock(side_effect=listing)) as github_listing:
                server_started(app, "Listening")
                await refresh_started.wait()
                request = mock.Mock()
                request.app = app
                request.query = {}
                response_task = asyncio.create_task(config_handler(request))
                await asyncio.sleep(0)
                self.assertFalse(response_task.done())

                allow_refresh.set()
                response = await response_task
                body = json.loads(response.body)

            self.assertEqual(body["payload"]["github_users"][0]["login"], "new-user")
            self.assertEqual(
                github_listing.await_args_list,
                [mock.call(force_refresh=True), mock.call(force_refresh=False)],
            )

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
