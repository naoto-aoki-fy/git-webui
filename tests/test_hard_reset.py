import asyncio
import unittest
from pathlib import Path
from unittest import mock

from backend.git_webui import application as app
from backend.git_webui.application import CommandResult, LogSink


class HardResetValidationTests(unittest.TestCase):
    def test_can_skip_commit_containment_check(self) -> None:
        command = mock.AsyncMock()
        logs = LogSink([])

        with mock.patch.object(app, "run_git_command", command):
            asyncio.run(
                app._verify_reset_commit_on_branch(
                    Path("/repo"), {}, logs, "abc123", "main", skip_check=True
                )
            )

        command.assert_not_awaited()
        self.assertIn(
            "Skipping the reset commit containment check", "\n".join(logs.entries)
        )

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

    def test_formats_backup_branch_from_original_tip_metadata(self) -> None:
        command = mock.AsyncMock(
            side_effect=[
                CommandResult(0, "abc1234\n", ""),
                CommandResult(0, "2026-08-29T20:01:00+00:00\n", ""),
                CommandResult(0, "", ""),
            ]
        )

        with mock.patch.object(app, "run_git_command", command):
            result = asyncio.run(
                app._format_reset_backup_branch(
                    Path("/repo"),
                    {},
                    LogSink([]),
                    "main",
                    "archive/{branch}-{date}-{commit_id}",
                )
            )

        self.assertEqual(result, "archive/main-2026-0829-2001-abc1234")
        self.assertEqual(
            command.await_args_list[-1].args,
            ("check-ref-format", "--branch", result),
        )

    def test_rejects_unknown_backup_format_specifier(self) -> None:
        command = mock.AsyncMock(
            side_effect=[
                CommandResult(0, "abc1234\n", ""),
                CommandResult(0, "2026-08-29T20:01:00+00:00\n", ""),
            ]
        )
        with mock.patch.object(app, "run_git_command", command):
            with self.assertRaisesRegex(RuntimeError, "Invalid backup branch name format"):
                asyncio.run(
                    app._format_reset_backup_branch(
                        Path("/repo"), {}, LogSink([]), "main", "{unknown}"
                    )
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
