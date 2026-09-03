import unittest

from aiohttp.test_utils import TestClient, TestServer

from backend.git_webui import create_app


class FrontendAssetsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.client = TestClient(TestServer(create_app()))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()

    async def frontend_source(self) -> str:
        sources = []
        for path in ("/", "/app.js", "/styles.css", "/form-mode.js"):
            response = await self.client.get(path)
            self.assertEqual(response.status, 200)
            sources.append(await response.text())
        return "\n".join(sources)

    async def test_index_loads_split_frontend_assets(self) -> None:
        response = await self.client.get("/")
        html = await response.text()

        self.assertEqual(response.status, 200)
        self.assertIn('<link rel="stylesheet" href="styles.css">', html)
        for asset in (
            "backend-client.js", "form-mode.js", "draft-storage.js",
            "repository-fields.js", "commit-generation.js", "app.js",
        ):
            self.assertIn(f'<script src="{asset}"></script>', html)
        self.assertNotIn("<style>", html)
        self.assertNotIn("<script>", html)

    async def test_commit_prompt_script_is_served(self) -> None:
        response = await self.client.get("/commit-prompt.js")

        self.assertEqual(response.status, 200)
        self.assertIn("javascript", response.content_type)
        self.assertIn("root.CommitPrompt = api", await response.text())

    async def test_unknown_script_is_not_served(self) -> None:
        response = await self.client.get("/unknown.js")

        self.assertEqual(response.status, 404)

    async def test_commit_message_action_defaults_to_generate(self) -> None:
        html = await self.frontend_source()

        self.assertNotIn('id="paste_commit_message"', html)
        self.assertIn(
            'name="commit_message_action" value="generate" checked',
            html,
        )
        self.assertIn('commitMessageAction:', html)

    async def test_config_fields_use_a_vertical_layout(self) -> None:
        html = await self.frontend_source()

        self.assertIn('id="config_group" class="config-group hidden"', html)
        self.assertIn(".config-group {", html)
        self.assertIn("flex-direction: column;", html)

    async def test_extra_input_line_height_is_only_applied_on_windows(self) -> None:
        html = await self.frontend_source()

        self.assertIn("navigator.userAgentData?.platform || navigator.platform", html)
        self.assertIn('document.documentElement.classList.add("windows");', html)
        self.assertIn(".windows input[type=text],", html)
        self.assertEqual(html.count("line-height: calc(1em + 2px);"), 1)

    async def test_branch_is_required(self) -> None:
        html = await self.frontend_source()

        self.assertIn('<label for="branch" id="branch_label">Branch</label>', html)
        self.assertIn('id="branch" name="branch" list="branch_history" required', html)
        self.assertNotIn("optional: use the current branch", html)
        self.assertIn('branchField.setAttribute("required", "");', html)

    async def test_new_branch_base_accepts_a_commit_or_branch_name(self) -> None:
        html = await self.frontend_source()

        self.assertIn("Base Commit ID or Branch Name", html)
        self.assertIn(
            "Create a new branch from a base commit ID or branch name.", html
        )

    async def test_sync_mode_offers_single_branch(self) -> None:
        html = await self.frontend_source()

        self.assertIn('name="sync_push_option" value="branch"', html)
        self.assertIn("const syncsSingleBranch = isMirrorMode", html)
        self.assertIn(
            'syncPushOptionInputs.forEach((input) => input.addEventListener("change", toggleCommitField));',
            html,
        )

    async def test_double_clicking_candidate_uses_its_commit_message(self) -> None:
        html = await self.frontend_source()

        self.assertIn(
            'label.addEventListener("dblclick", () => useCommitCandidate(candidate.id));',
            html,
        )
        self.assertIn(
            'useCandidateButton.addEventListener("click", () => useCommitCandidate());',
            html,
        )

    async def test_codex_candidate_output_format_is_selectable_and_persisted(self) -> None:
        html = await self.frontend_source()

        self.assertIn('id="codex_candidate_output_format"', html)
        self.assertIn('candidateOutputFormat: codexCandidateOutputFormatField.value', html)
        self.assertIn(
            'const outputFormat = CommitGeneration.outputFormat(',
            html,
        )

    async def test_config_is_reloaded_when_the_event_stream_connects(self) -> None:
        html = await self.frontend_source()

        self.assertIn('if (payload.type === "connected") {', html)
        self.assertIn('void fetchConfig(false, true);', html)
        self.assertIn(
            'Array.from(gitUserField.options).some((option) => option.value === selectedGitUser)',
            html,
        )
        self.assertNotIn("connectEventStream();\n        fetchConfig();", html)

    async def test_github_users_can_be_updated_beside_git_author(self) -> None:
        html = await self.frontend_source()

        git_author_group = html[
            html.index('<div id="git_user_group">'):
            html.index('<div id="commit_message_group">')
        ]
        self.assertIn(
            'id="update_github_users">Update GitHub users</button>',
            git_author_group,
        )
        self.assertIn(
            'updateGithubUsersButton.addEventListener("click", () => fetchConfig(true));',
            html,
        )
        self.assertIn('"/config" + (forceRefresh ? "?refresh=1" : "")', html)

    async def test_github_repositories_can_be_updated_beside_repository(self) -> None:
        html = await self.frontend_source()

        repository_group = html[
            html.index('<div id="repository_group">'):
            html.index('<div id="mirror_repository_group"')
        ]
        self.assertIn(
            'id="update_github_repositories">Update GitHub repositories</button>',
            repository_group,
        )
        self.assertIn(
            'updateGithubRepositoriesButton.addEventListener("click", () => fetchConfig(true));',
            html,
        )
        self.assertIn(
            "const updateButtons = [updateGithubRepositoriesButton, updateGithubUsersButton].filter(Boolean);",
            html,
        )

    async def test_git_author_defaults_to_backend_auto_selection(self) -> None:
        html = await self.frontend_source()

        self.assertIn('<option value="">Auto-select</option>', html)
        self.assertIn('autoOption.textContent = "Auto-select";', html)
        self.assertNotIn("applyDefaultGitUserForRepository", html)
        self.assertNotIn("confirmNonDefaultGitUser", html)


if __name__ == "__main__":
    unittest.main()
