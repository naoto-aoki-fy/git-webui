import asyncio
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.git_webui.application import (
    CommandResult,
    LogSink,
    _apply_patch,
    _parse_patch_application_options,
    process_submission,
)


class PatchApplicationMethodTests(unittest.TestCase):
    def test_omitted_method_defaults_to_git_apply(self) -> None:
        self.assertEqual(_parse_patch_application_options({}), ("git_apply", 2))

    def test_explicit_git_apply_uses_existing_helper(self) -> None:
        with mock.patch(
            "backend.git_webui.application._apply_patch_with_line_ending_retry",
            new=mock.AsyncMock(),
        ) as apply_git:
            asyncio.run(
                _apply_patch(
                    "git_apply", 9, Path("repo"), Path("patch.diff"), "patch", {}, LogSink([])
                )
            )
        apply_git.assert_awaited_once()

    def test_fuzzy_method_invokes_patch_with_configured_fuzz(self) -> None:
        with mock.patch(
            "backend.git_webui.application.run_command",
            new=mock.AsyncMock(return_value=CommandResult(0, "", "")),
        ) as run:
            asyncio.run(
                _apply_patch(
                    "patch_fuzzy", 7, Path("repo"), Path("patch.diff"),
                    "--- a/a.txt\n+++ b/a.txt\n", {}, LogSink([]),
                )
            )
        self.assertEqual(run.await_args.args, ("patch", "-p1", "-F", "7", "-i", "patch.diff"))
        self.assertEqual(run.await_args.kwargs["cwd"], Path("repo"))

    def test_outer_context_mismatch_succeeds_only_in_fuzzy_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            target = repo / "file.txt"
            target.write_text("ONE\ntwo\nthree\nfour\nFIVE\n", encoding="utf-8")
            subprocess.run(["git", "add", "file.txt"], cwd=repo, check=True)
            patch_content = """diff --git a/file.txt b/file.txt
--- a/file.txt
+++ b/file.txt
@@ -1,5 +1,5 @@
 one
 two
-three
+THREE
 four
 five
"""
            patch_path = Path(directory) / "patch.diff"
            patch_path.write_text(patch_content, encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "git apply failed"):
                asyncio.run(
                    _apply_patch("git_apply", 2, repo, patch_path, patch_content, {}, LogSink([]))
                )
            target.write_text("ONE\ntwo\nthree\nfour\nFIVE\n", encoding="utf-8")
            asyncio.run(
                _apply_patch("patch_fuzzy", 1, repo, patch_path, patch_content, {}, LogSink([]))
            )
            self.assertEqual(target.read_text(encoding="utf-8"), "ONE\ntwo\nTHREE\nfour\nFIVE\n")

    def test_invalid_fuzz_values_are_rejected(self) -> None:
        for value, message in (("abc", "integer"), ("-1", "non-negative"), ("101", "not exceed")):
            with self.subTest(value=value), self.assertRaisesRegex(RuntimeError, message):
                _parse_patch_application_options({"patch_fuzz": value})
        with self.assertRaisesRegex(RuntimeError, "Invalid patch application method"):
            _parse_patch_application_options({"patch_application_method": "automatic"})

    def test_patch_failure_includes_command_output_and_log(self) -> None:
        logs = LogSink([])
        result = CommandResult(2, "can't find file to patch", "")
        with mock.patch(
            "backend.git_webui.application.run_command", new=mock.AsyncMock(return_value=result)
        ):
            with self.assertRaisesRegex(RuntimeError, "can't find file to patch"):
                asyncio.run(
                    _apply_patch(
                        "patch_fuzzy", 2, Path("repo"), Path("patch.diff"),
                        "--- a/a\n+++ b/a\n", {}, logs,
                    )
                )
        self.assertIn("fuzz factor 2", "\n".join(logs.entries))

    def test_fuzzy_mode_rejects_git_only_metadata(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "file mode changes"):
            asyncio.run(
                _apply_patch(
                    "patch_fuzzy", 2, Path("repo"), Path("patch.diff"),
                    "diff --git a/a b/a\nold mode 100644\nnew mode 100755\n", {}, LogSink([]),
                )
            )

    def test_submission_applies_fuzzy_patch_before_common_staging(self) -> None:
        events = []

        async def clone(_url, repo_dir, _env, _logs, **_kwargs):
            repo_dir.mkdir(parents=True)

        async def apply(*args, **_kwargs):
            events.append(("apply", args[0], args[1]))

        async def checked(*args, **_kwargs):
            if args[:2] == ("add", "-A"):
                events.append(("stage", *args))
            return CommandResult(0, "", "")

        listing = {
            "github_users": [{
                "login": "example", "name": "Example", "email": "example@example.com"
            }],
            "github_repositories": [],
        }
        form = {
            "repository_url": "https://github.com/example/repo.git",
            "branch": "main",
            "patch": "--- a/a.txt\n+++ b/a.txt\n",
            "patch_application_method": "patch_fuzzy",
            "patch_fuzz": "4",
        }
        with (
            mock.patch("backend.git_webui.application._github_listing", mock.AsyncMock(return_value=listing)),
            mock.patch("backend.git_webui.application._clone_isolated_repository", clone),
            mock.patch("backend.git_webui.application._apply_patch", apply),
            mock.patch("backend.git_webui.application.run_git_checked", checked),
            mock.patch(
                "backend.git_webui.application.run_git_command",
                mock.AsyncMock(return_value=CommandResult(0, "", "")),
            ),
            mock.patch("backend.git_webui.application._push_to_remotes", mock.AsyncMock()),
        ):
            result = asyncio.run(process_submission(form, LogSink([])))

        self.assertTrue(result["success"])
        self.assertEqual(events, [("apply", "patch_fuzzy", 4), ("stage", "add", "-A")])


if __name__ == "__main__":
    unittest.main()
