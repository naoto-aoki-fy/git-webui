import asyncio
import unittest
from unittest import mock

from backend.git_webui import application as app
from backend.git_webui.application import LogSink, process_submission


class SyncRepositoryTests(unittest.TestCase):
    def run_sync(self, sync_push_option=None, branch=None):
        form = {
            "repository_url": "https://github.com/example/source.git",
            "mirror_repository_url": "https://github.com/example/destination.git",
            "branch_mode": "mirror_repository",
        }
        if sync_push_option is not None:
            form["sync_push_option"] = sync_push_option
        if branch is not None:
            form["branch"] = branch

        completed = mock.Mock(returncode=0)
        with (
            mock.patch.object(app, "run_git_command", new=mock.AsyncMock(return_value=completed)) as run_git,
            mock.patch.object(app, "_clone_isolated_repository", new=mock.AsyncMock()),
        ):
            result = asyncio.run(process_submission(form, LogSink([])))

        self.assertTrue(result["success"])
        return run_git.await_args_list

    def test_sync_defaults_to_push_all(self):
        calls = self.run_sync()

        self.assertEqual(calls[0].args[:3], ("push", "--all", "https://github.com/example/destination.git"))

    def test_sync_can_push_a_single_branch(self):
        calls = self.run_sync("branch", "release/v2")

        self.assertEqual(
            calls[0].args[:3],
            (
                "push",
                "https://github.com/example/destination.git",
                "refs/heads/release/v2:refs/heads/release/v2",
            ),
        )

    def test_single_branch_sync_requires_a_branch(self):
        form = {
            "repository_url": "https://github.com/example/source.git",
            "mirror_repository_url": "https://github.com/example/destination.git",
            "branch_mode": "mirror_repository",
            "sync_push_option": "branch",
        }

        result = asyncio.run(process_submission(form, LogSink([])))

        self.assertFalse(result["success"])

    def test_sync_can_use_previous_mirror_behavior(self):
        calls = self.run_sync("mirror")

        self.assertEqual(calls[0].args[:3], ("push", "--mirror", "https://github.com/example/destination.git"))


if __name__ == "__main__":
    unittest.main()
