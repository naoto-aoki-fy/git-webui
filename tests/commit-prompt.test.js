const test = require("node:test");
const assert = require("node:assert/strict");
const {buildUserChangesPrompt} = require("../frontend/commit-prompt.js");

test("uses patch content for the normal mode", () => {
    const prompt = buildUserChangesPrompt({branchMode: "default", memo: "Fix greeting", patch: "+hello"});
    assert.match(prompt, /^Fix greeting\n\n```diff\n\+hello\n```\n$/);
});

test("reflects all user-entered add-file details", () => {
    const prompt = buildUserChangesPrompt({branchMode: "add_file", memo: "Add configuration",
        patch: "+must not be included", filePath: "config/example.toml",
        fileContent: "name = `example`\n", overwrite: true});
    assert.match(prompt, /^Add configuration/);
    assert.match(prompt, /File path: config\/example\.toml/);
    assert.match(prompt, /Overwrite existing file: enabled/);
    assert.match(prompt, /```text\nname = `example`\n```/);
    assert.doesNotMatch(prompt, /must not be included/);
});

test("uses a safe fence when add-file content contains backticks", () => {
    const prompt = buildUserChangesPrompt({branchMode: "add_file", fileContent: "before\n```\nafter", overwrite: false});
    assert.match(prompt, /Overwrite existing file: disabled/);
    assert.match(prompt, /````text\nbefore\n```\nafter\n````/);
});
