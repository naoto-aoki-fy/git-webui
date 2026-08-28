import asyncio
import unittest
from pathlib import Path
from unittest import mock

from backend import app
from backend.app import CommandResult, LogSink


class HardResetValidationTests(unittest.TestCase):
    def test_accepts_commit_contained_in_remote_branch(self) -> None:
        command = mock.AsyncMock(return_value=CommandResult(0, "", ""))
        logs = LogSink([])

        with mock.patch.object(app, "run_git_command", command):
            asyncio.run(
                app._verify_reset_commit_on_branch(
                    Path("/repo"), {}, logs, "abc123", "main"
                )
            )

        self.assertEqual(
            command.await_args.args,
            ("merge-base", "--is-ancestor", "abc123", "origin/main"),
        )

    def test_rejects_commit_not_contained_in_remote_branch(self) -> None:
        command = mock.AsyncMock(return_value=CommandResult(1, "", ""))

        with mock.patch.object(app, "run_git_command", command):
            with self.assertRaisesRegex(
                RuntimeError,
                "Commit 'abc123' is not contained in branch 'main'; hard reset aborted",
            ):
                asyncio.run(
                    app._verify_reset_commit_on_branch(
                        Path("/repo"), {}, LogSink([]), "abc123", "main"
                    )
                )


if __name__ == "__main__":
    unittest.main()
