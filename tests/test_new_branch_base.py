import unittest
from pathlib import Path
from unittest import mock

from backend import app
from backend.app import CommandResult


class NewBranchBaseTest(unittest.IsolatedAsyncioTestCase):
    async def test_remote_branch_name_resolves_to_remote_tracking_ref(self) -> None:
        command = mock.AsyncMock(return_value=CommandResult(0, "", ""))

        with mock.patch.object(app, "run_git_command", command):
            result = await app._resolve_branch_creation_base(
                Path("/repo"), "feature/base", {}, []
            )

        self.assertEqual(result, "origin/feature/base")
        self.assertEqual(
            command.await_args.args,
            ("show-ref", "--verify", "refs/remotes/origin/feature/base"),
        )

    async def test_commit_id_is_left_unchanged(self) -> None:
        command = mock.AsyncMock(return_value=CommandResult(1, "", ""))

        with mock.patch.object(app, "run_git_command", command):
            result = await app._resolve_branch_creation_base(
                Path("/repo"), "a1b2c3d", {}, []
            )

        self.assertEqual(result, "a1b2c3d")


if __name__ == "__main__":
    unittest.main()
