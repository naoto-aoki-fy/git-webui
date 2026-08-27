import asyncio
import unittest
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from backend.app import create_app


class CancelSubmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.client = TestClient(TestServer(create_app(serve_frontend=False)))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()

    async def test_cancel_stops_the_active_submission(self) -> None:
        started = asyncio.Event()

        async def wait_until_cancelled(_form, _logs):
            started.set()
            await asyncio.Event().wait()

        with patch("backend.app.process_submission", side_effect=wait_until_cancelled):
            submit = asyncio.create_task(self.client.post(
                "/submit", json={"client_id": "test-client", "payload": {}},
            ))
            await asyncio.wait_for(started.wait(), timeout=1)

            cancel_response = await self.client.post(
                "/submit/cancel", json={"client_id": "test-client"},
            )
            submit_response = await asyncio.wait_for(submit, timeout=1)

        self.assertEqual(cancel_response.status, 200)
        self.assertEqual(await cancel_response.json(), {"cancelled": True})
        self.assertEqual(submit_response.status, 200)
        self.assertEqual(
            await submit_response.json(), {"accepted": True, "cancelled": True}
        )

    async def test_cancel_reports_when_no_submission_is_running(self) -> None:
        response = await self.client.post(
            "/submit/cancel", json={"client_id": "idle-client"},
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), {"cancelled": False})


if __name__ == "__main__":
    unittest.main()
