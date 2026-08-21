const test = require("node:test");
const assert = require("node:assert/strict");
const {
    buildOpenAIRequestBody,
    parseAdditionalRequestSettings,
} = require("../frontend/openai-request-settings.js");

test("a blank field produces an empty object", () => {
    assert.deepEqual(parseAdditionalRequestSettings("  \n"), {});
});

test("thinking settings are accepted and merged at the top level", () => {
    const body = buildOpenAIRequestBody({model: "example"}, '{"thinking":{"type":"disabled"}}');
    assert.deepEqual(body, {thinking: {type: "disabled"}, model: "example"});
});

test("invalid JSON has an actionable error", () => {
    assert.throws(() => parseAdditionalRequestSettings("{"), /contain invalid JSON/);
});

test("arrays are rejected", () => {
    assert.throws(() => parseAdditionalRequestSettings("[]"), /must be a JSON object/);
});

test("reserved keys cannot be overridden", () => {
    assert.throws(() => parseAdditionalRequestSettings('{"model":"other"}'), /reserved key "model"/);
});
