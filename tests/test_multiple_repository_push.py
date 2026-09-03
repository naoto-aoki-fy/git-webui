import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.git_webui import application as app
from backend.git_webui.application import CommandResult, LogSink, _additional_repository, _push_to_remotes
from backend.git_webui.settings import Settings


class MultipleRepositoryPushTests(unittest.TestCase):
    def test_config_loads_prefix_destination_pairs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text('repository_prefix_destinations = [["source-", "target-user"]]\n')
            self.assertEqual(
                Settings.load(config_path=path).repository_prefix_destinations,
                (("source-", "target-user"),),
            )

    def test_prefix_is_removed_to_derive_destination_name(self):
        with mock.patch.object(
            app, "APP_CONFIG", {"repository_prefix_destinations": [["Source-", "target-user"]]}
        ):
            self.assertEqual(_additional_repository("source-widget"), ("target-user", "widget"))

    def test_additional_push_is_dry_run_before_pushing_to_both_repositories(self):
        command = mock.AsyncMock(return_value=CommandResult(0, "", ""))
        with mock.patch.object(
            app, "APP_CONFIG", {"repository_prefix_destinations": [["source-", "target-user"]]}
        ), mock.patch.object(app, "run_git_command", command):
            asyncio.run(
                _push_to_remotes(Path("/repo"), {}, LogSink([]), "source-widget", "HEAD:main")
            )

        self.assertEqual(
            [call.args for call in command.await_args_list],
            [
                (
                    "push",
                    "--dry-run",
                    "https://github.com/target-user/widget.git",
                    "HEAD:main",
                ),
                ("push", "origin", "HEAD:main"),
                ("push", "https://github.com/target-user/widget.git", "HEAD:main"),
            ],
        )

    def test_dry_run_failure_prevents_all_real_pushes(self):
        command = mock.AsyncMock(return_value=CommandResult(1, "", "rejected"))
        with mock.patch.object(
            app, "APP_CONFIG", {"repository_prefix_destinations": [["source-", "target-user"]]}
        ), mock.patch.object(app, "run_git_command", command):
            with self.assertRaisesRegex(
                RuntimeError, "git push --dry-run to target-user/widget failed"
            ):
                asyncio.run(
                    _push_to_remotes(
                        Path("/repo"), {}, LogSink([]), "source-widget", "HEAD:main"
                    )
                )

        self.assertEqual(
            [call.args for call in command.await_args_list],
            [
                (
                    "push",
                    "--dry-run",
                    "https://github.com/target-user/widget.git",
                    "HEAD:main",
                ),
            ],
        )

    def test_single_destination_pushes_without_a_dry_run(self):
        command = mock.AsyncMock(return_value=CommandResult(0, "", ""))
        with mock.patch.object(
            app, "APP_CONFIG", {"repository_prefix_destinations": []}
        ), mock.patch.object(app, "run_git_command", command):
            asyncio.run(_push_to_remotes(Path("/repo"), {}, LogSink([]), "widget", "HEAD:main"))

        self.assertEqual(command.await_args.args, ("push", "origin", "HEAD:main"))


if __name__ == "__main__":
    unittest.main()
