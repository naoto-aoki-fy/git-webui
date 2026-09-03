import unittest

from backend.git_webui.application import DEFAULT_BIND, DEFAULT_PORT, _resolve_server_listen_config


class ServerListenConfigTest(unittest.TestCase):
    def test_default_listens_on_tcp(self) -> None:
        config = _resolve_server_listen_config()

        self.assertEqual(config.host, DEFAULT_BIND)
        self.assertEqual(config.port, DEFAULT_PORT)
        self.assertIsNone(config.path)

    def test_unix_socket_disables_tcp(self) -> None:
        config = _resolve_server_listen_config(unix_socket="/tmp/git-webui.sock")

        self.assertIsNone(config.host)
        self.assertIsNone(config.port)
        self.assertEqual(config.path, "/tmp/git-webui.sock")

    def test_unix_socket_rejects_tcp_overrides(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "--unix-socket cannot be combined"):
            _resolve_server_listen_config(bind_override="127.0.0.1", unix_socket="/tmp/git-webui.sock")

        with self.assertRaisesRegex(RuntimeError, "--unix-socket cannot be combined"):
            _resolve_server_listen_config(port_override=9090, unix_socket="/tmp/git-webui.sock")


if __name__ == "__main__":
    unittest.main()
