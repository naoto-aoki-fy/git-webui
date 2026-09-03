import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.git_webui import application as app
from backend.git_webui.application import CommandResult, _ensure_mirror_cache


class MirrorCacheTests(unittest.TestCase):
    def test_existing_cache_is_refreshed_from_remote(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = Path(tmpdir) / "repository.git"
            cache.mkdir()
            commands = mock.AsyncMock(
                side_effect=[
                    CommandResult(0, "true\n", ""),
                    CommandResult(0, "", ""),
                ]
            )

            with (
                mock.patch.object(app, "_repo_workspace_for_url", return_value=cache),
                mock.patch.object(app, "run_git_command", commands),
            ):
                result = asyncio.run(
                    _ensure_mirror_cache("https://example.com/repository.git", {}, [])
                )

            self.assertEqual(cache, result)
            self.assertEqual(
                commands.await_args_list[1].args,
                ("remote", "update", "--prune"),
            )
            self.assertEqual(commands.await_args_list[1].kwargs["cwd"], cache)
            self.assertEqual(commands.await_args_list[1].kwargs["env"], {})

    def test_cache_refresh_failure_aborts_the_operation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = Path(tmpdir) / "repository.git"
            cache.mkdir()
            commands = mock.AsyncMock(
                side_effect=[
                    CommandResult(0, "true\n", ""),
                    CommandResult(1, "", "fetch failed"),
                ]
            )

            with (
                mock.patch.object(app, "_repo_workspace_for_url", return_value=cache),
                mock.patch.object(app, "run_git_command", commands),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "Failed to refresh bare mirror cache"
                ):
                    asyncio.run(
                        _ensure_mirror_cache(
                            "https://example.com/repository.git", {}, []
                        )
                    )


if __name__ == "__main__":
    unittest.main()
