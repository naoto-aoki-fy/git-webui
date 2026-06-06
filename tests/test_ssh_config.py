import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import app
from backend.app import _configured_ssh_identity_options, _serialize_config


class SshConfigTests(unittest.TestCase):
    def test_configured_ssh_identity_options_includes_each_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            key_one = Path(tmpdir) / "id_ed25519_work"
            key_two = Path(tmpdir) / "id ed25519 personal"
            key_one.touch()
            key_two.touch()

            config = {
                "ssh_keys": [
                    {"label": "Work", "path": str(key_one)},
                    {"label": "Personal", "path": str(key_two)},
                ],
                "git_users": [],
            }

            with patch.object(app, "APP_CONFIG", config):
                self.assertEqual(
                    _configured_ssh_identity_options(),
                    [
                        f"-o IdentityFile={key_one}",
                        f"-o IdentityFile='{key_two}'",
                    ],
                )

    def test_serialized_ssh_keys_do_not_include_default(self) -> None:
        config = {
            "ssh_keys": [
                {"label": "Work", "path": "/tmp/id_ed25519_work", "default": True},
            ],
            "git_users": [],
        }

        with patch.object(app, "APP_CONFIG", config):
            payload = _serialize_config()

        self.assertEqual(payload["ssh_keys"], [{"label": "Work"}])
        self.assertNotIn("default_ssh_key_index", payload)


if __name__ == "__main__":
    unittest.main()
