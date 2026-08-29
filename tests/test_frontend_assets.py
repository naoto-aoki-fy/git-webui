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

    async def test_commit_message_action_defaults_to_generate(self) -> None:
        response = await self.client.get("/")
        html = await response.text()

        self.assertNotIn('id="paste_commit_message"', html)
        self.assertIn(
            'name="commit_message_action" value="generate" checked',
            html,
        )
        self.assertIn('commitMessageAction:', html)

    async def test_config_fields_use_a_vertical_layout(self) -> None:
        response = await self.client.get("/")
        html = await response.text()

        self.assertIn('id="config_group" class="config-group hidden"', html)
        self.assertIn(".config-group {", html)
        self.assertIn("flex-direction: column;", html)

    async def test_branch_is_required(self) -> None:
        response = await self.client.get("/")
        html = await response.text()

        self.assertIn('<label for="branch" id="branch_label">Branch</label>', html)
        self.assertIn('id="branch" name="branch" list="branch_history" required', html)
        self.assertNotIn("optional: use the current branch", html)
        self.assertIn('branchField.setAttribute("required", "");', html)

    async def test_double_clicking_candidate_uses_its_commit_message(self) -> None:
        response = await self.client.get("/")
        html = await response.text()

        self.assertIn(
            'label.addEventListener("dblclick", () => useCommitCandidate(candidate.id));',
            html,
        )
        self.assertIn(
            'useCandidateButton.addEventListener("click", () => useCommitCandidate());',
            html,
        )

    async def test_codex_candidate_output_format_is_selectable_and_persisted(self) -> None:
        response = await self.client.get("/")
        html = await response.text()

        self.assertIn('id="codex_candidate_output_format"', html)
        self.assertIn('candidateOutputFormat: codexCandidateOutputFormatField.value', html)
        self.assertIn(
            'const outputFormat = isCodex ? codexCandidateOutputFormatField.value',
            html,
        )

    async def test_config_is_reloaded_when_the_event_stream_connects(self) -> None:
        response = await self.client.get("/")
        html = await response.text()

        self.assertIn('if (payload.type === "connected") {', html)
        self.assertIn('void fetchConfig(false, true);', html)
        self.assertIn(
            'Array.from(gitUserField.options).some((option) => option.value === selectedGitUser)',
            html,
        )
        self.assertNotIn("connectEventStream();\n        fetchConfig();", html)


if __name__ == "__main__":
    unittest.main()
