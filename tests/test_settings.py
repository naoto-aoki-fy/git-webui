import tempfile
import unittest
from pathlib import Path

from backend.git_webui.settings import Settings


class SettingsTest(unittest.TestCase):
    def test_loads_immutable_typed_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.toml"
            config.write_text(
                'git_user_defaults = [{ repository = "org/repo", github_user = "octocat" }]\n'
                'repository_prefix_destinations = [["source-", "destination"]]\n',
                encoding="utf-8",
            )

            settings = Settings.load(config_path=config, repository_root=Path(directory) / "repos")

            self.assertEqual(settings.git_user_defaults, (("org/repo", "octocat"),))
            self.assertEqual(settings.repository_prefix_destinations, (("source-", "destination"),))

    def test_loading_settings_does_not_create_repository_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository_root = Path(directory) / "not-created"

            Settings.load(repository_root=repository_root)

            self.assertFalse(repository_root.exists())


if __name__ == "__main__":
    unittest.main()
