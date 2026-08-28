import unittest

from backend.git_webui.application import process_submission


class BranchRequiredTest(unittest.IsolatedAsyncioTestCase):
    async def test_default_mode_rejects_missing_branch(self) -> None:
        logs = []

        result = await process_submission(
            {
                "repository_url": "https://github.com/example/repository.git",
                "branch_mode": "default",
                "patch": "diff --git a/file.txt b/file.txt",
            },
            logs,
        )

        self.assertFalse(result["success"])
        self.assertTrue(any("Branch is required." in entry for entry in logs))

    async def test_add_file_mode_rejects_missing_branch(self) -> None:
        logs = []

        result = await process_submission(
            {
                "repository_url": "https://github.com/example/repository.git",
                "branch_mode": "add_file",
                "files": '[{"path": "file.txt", "content": "content"}]',
            },
            logs,
        )

        self.assertFalse(result["success"])
        self.assertTrue(any("Branch is required." in entry for entry in logs))


if __name__ == "__main__":
    unittest.main()
