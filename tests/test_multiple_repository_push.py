import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import backend.app as app
from backend.app import CommandResult, LogSink, _additional_repository, _load_config, _push_to_remotes


class MultipleRepositoryPushTests(unittest.TestCase):
    def test_config_loads_prefix_destination_pairs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text('repository_prefix_destinations = [["source-", "target-user"]]\n')
            self.assertEqual(
                _load_config(path)["repository_prefix_destinations"],
                [["source-", "target-user"]],
            )

    def test_prefix_is_removed_to_derive_destination_name(self):
        with mock.patch.object(
            app, "APP_CONFIG", {"repository_prefix_destinations": [["Source-", "target-user"]]}
        ):
            self.assertEqual(_additional_repository("source-widget"), ("target-user", "widget"))

    def test_push_is_repeated_to_derived_repository(self):
        command = mock.AsyncMock(return_value=CommandResult(0, "", ""))
        with mock.patch.object(
            app, "APP_CONFIG", {"repository_prefix_destinations": [["source-", "target-user"]]}
        ), mock.patch.object(app, "run_git_command", command):
            asyncio.run(
                _push_to_remotes(Path("/repo"), {}, LogSink([]), "source-widget", "HEAD:main")
            )

        self.assertEqual(command.await_count, 2)
        self.assertEqual(command.await_args_list[0].args, ("push", "origin", "HEAD:main"))
        self.assertEqual(
            command.await_args_list[1].args,
            ("push", "https://github.com/target-user/widget.git", "HEAD:main"),
        )


if __name__ == "__main__":
    unittest.main()
