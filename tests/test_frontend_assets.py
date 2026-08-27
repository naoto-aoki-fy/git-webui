import unittest

from aiohttp.test_utils import TestClient, TestServer

from backend.app import create_app


class FrontendAssetsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.client = TestClient(TestServer(create_app()))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()

    async def test_commit_prompt_script_is_served(self) -> None:
        response = await self.client.get("/commit-prompt.js")

        self.assertEqual(response.status, 200)
        self.assertIn("javascript", response.content_type)
        self.assertIn("root.CommitPrompt = api", await response.text())

    async def test_unknown_script_is_not_served(self) -> None:
        response = await self.client.get("/unknown.js")

        self.assertEqual(response.status, 404)


if __name__ == "__main__":
    unittest.main()
