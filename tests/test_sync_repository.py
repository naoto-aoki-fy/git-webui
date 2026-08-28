import asyncio
import unittest
from unittest import mock

from backend import app
from backend.app import LogSink, process_submission


class SyncRepositoryTests(unittest.TestCase):
    def run_sync(self, sync_push_option=None):
        form = {
            "repository_url": "https://github.com/example/source.git",
            "mirror_repository_url": "https://github.com/example/destination.git",
            "branch_mode": "mirror_repository",
        }
        if sync_push_option is not None:
            form["sync_push_option"] = sync_push_option

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

    def test_sync_can_use_previous_mirror_behavior(self):
        calls = self.run_sync("mirror")

        self.assertEqual(calls[0].args[:3], ("push", "--mirror", "https://github.com/example/destination.git"))


if __name__ == "__main__":
    unittest.main()
