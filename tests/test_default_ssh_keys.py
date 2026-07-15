import unittest
from unittest import mock

from backend import app
from backend.app import _find_default_ssh_key_index_for_repository


class DefaultSshKeyRepositoryMatchingTests(unittest.TestCase):
    def test_scp_like_repository_matches_default_by_exact_owner(self) -> None:
        app_config = {
            "ssh_keys": [
                {"label": "Fallback", "default": True},
                {"label": "Example Org", "default_repositories": ["example"]},
            ],
            "git_users": [],
        }

        with mock.patch.object(app, "APP_CONFIG", app_config):
            self.assertEqual(
                _find_default_ssh_key_index_for_repository("git@github.com:example/project.git"),
                1,
            )

    def test_scp_like_repository_does_not_match_partial_owner(self) -> None:
        app_config = {
            "ssh_keys": [
                {"label": "Fallback", "default": True},
                {"label": "Partial", "default_repositories": ["exam"]},
            ],
            "git_users": [],
        }

        with mock.patch.object(app, "APP_CONFIG", app_config):
            self.assertEqual(
                _find_default_ssh_key_index_for_repository("git@github.com:example/project.git"),
                0,
            )

    def test_default_repository_url_matches_by_owner_not_repo_name(self) -> None:
        app_config = {
            "ssh_keys": [
                {"label": "Fallback", "default": True},
                {"label": "Example Org", "default_repositories": ["git@github.com:example/other.git"]},
            ],
            "git_users": [],
        }

        with mock.patch.object(app, "APP_CONFIG", app_config):
            self.assertEqual(
                _find_default_ssh_key_index_for_repository("git@github.com:example/project.git"),
                1,
            )


if __name__ == "__main__":
    unittest.main()
