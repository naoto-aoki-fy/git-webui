const test = require("node:test");
const assert = require("node:assert/strict");
const {OpenAICandidateProvider, determineModelListEndpoint} = require("../frontend/openai-candidate-provider.js");

test("determines the OpenAI-compatible model endpoint separately", () => {
    assert.equal(
        determineModelListEndpoint("https://example.test/v1/chat/completions?version=1"),
        "https://example.test/v1/models",
    );
});

test("lists models through the derived endpoint", async (t) => {
    const originalFetch = global.fetch;
    t.after(() => { global.fetch = originalFetch; });
    global.fetch = async (url, options) => {
        assert.equal(url, "https://example.test/v1/models");
        assert.equal(options.headers.Authorization, "Bearer secret");
        return {ok: true, json: async () => ({data: [{id: "model-a"}]})};
    };
    const provider = new OpenAICandidateProvider({
        endpoint: "https://example.test/v1/chat/completions", token: "secret",
    });
    assert.deepEqual(await provider.listModels(), [{id: "model-a"}]);
});
