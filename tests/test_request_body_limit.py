import asyncio
import io
import unittest
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from backend.git_webui import create_app
from backend.git_webui.application import MAX_REQUEST_BODY_SIZE


class RequestBodyLimitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.client = TestClient(TestServer(create_app(serve_frontend=False)))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()

    async def test_accepts_patch_larger_than_aiohttp_default(self) -> None:
        completed = asyncio.Event()

        async def accept_submission(_form, _logs):
            completed.set()
            return {"success": True}

        with patch(
            "backend.git_webui.application.process_submission",
            side_effect=accept_submission,
        ):
            response = await self.client.post(
                "/submit",
                json={
                    "client_id": "large-patch",
                    "payload": {
                        "branch_mode": "default",
                        "patch": "x" * (1024 * 1024 + 1024),
                    },
                },
            )
            await asyncio.wait_for(completed.wait(), timeout=1)

        self.assertEqual(response.status, 202)
        self.assertEqual(await response.json(), {"accepted": True})

    async def test_oversized_response_exposes_413_through_cors(self) -> None:
        response = await self.client.post(
            "/submit",
            data=io.BytesIO(b"x" * (MAX_REQUEST_BODY_SIZE + 1)),
            headers={
                "Content-Type": "application/json",
                "Origin": "https://frontend.example",
            },
        )

        self.assertEqual(response.status, 413)
        self.assertEqual(
            response.headers["Access-Control-Allow-Origin"],
            "*",
        )

    async def test_handler_http_errors_include_cors_headers(self) -> None:
        response = await self.client.post(
            "/submit",
            json={},
            headers={"Origin": "https://frontend.example"},
        )

        self.assertEqual(response.status, 400)
        self.assertEqual(response.headers["Access-Control-Allow-Origin"], "*")


if __name__ == "__main__":
    unittest.main()
