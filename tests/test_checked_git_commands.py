import asyncio
import unittest
from pathlib import Path
from unittest import mock

from backend.git_webui import application as app
from backend.git_webui.application import CommandResult, GitCommandError


class CheckedGitCommandTests(unittest.TestCase):
    def test_checked_command_raises_with_command_and_git_error(self) -> None:
        failure = CommandResult(128, "", "fatal: simulated failure\n")
        with mock.patch.object(
            app, "run_git_command", new=mock.AsyncMock(return_value=failure)
        ):
            with self.assertRaisesRegex(
                GitCommandError,
                r"git add -A failed with exit code 128: fatal: simulated failure",
            ):
                asyncio.run(app.run_git_checked("add", "-A", cwd=Path("/repo")))

    def test_orphan_cleanup_stops_when_git_rm_fails(self) -> None:
        command = mock.AsyncMock(return_value=CommandResult(1, "", "rm failed"))
        with mock.patch.object(app, "run_git_command", command):
            with self.assertRaises(GitCommandError):
                asyncio.run(app._clean_orphan_worktree(Path("/repo"), {}))

        self.assertEqual(command.await_count, 1)
        self.assertEqual(
            command.await_args.args,
            ("rm", "-rf", "--ignore-unmatch", "."),
        )

    def test_orphan_cleanup_cleans_and_verifies_worktree(self) -> None:
        command = mock.AsyncMock(
            side_effect=[
                CommandResult(0, "", ""),
                CommandResult(0, "", ""),
                CommandResult(0, "", ""),
            ]
        )
        with mock.patch.object(app, "run_git_command", command):
            asyncio.run(app._clean_orphan_worktree(Path("/repo"), {}))

        self.assertEqual(
            [call.args for call in command.await_args_list],
            [
                ("rm", "-rf", "--ignore-unmatch", "."),
                ("clean", "-fd"),
                ("status", "--porcelain", "--untracked-files=all"),
            ],
        )

    def test_orphan_cleanup_rejects_remaining_files(self) -> None:
        command = mock.AsyncMock(
            side_effect=[
                CommandResult(0, "", ""),
                CommandResult(0, "", ""),
                CommandResult(0, "?? remaining.txt\n", ""),
            ]
        )
        with mock.patch.object(app, "run_git_command", command):
            with self.assertRaisesRegex(RuntimeError, "worktree is not empty"):
                asyncio.run(app._clean_orphan_worktree(Path("/repo"), {}))


if __name__ == "__main__":
    unittest.main()
