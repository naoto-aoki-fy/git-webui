const test = require("node:test");
const assert = require("node:assert/strict");
const {
    buildCandidateResponseFormat,
    buildOpenAIRequestBody,
    parseAdditionalRequestSettings,
} = require("../frontend/openai-request-settings.js");

test("NDJSON does not add a response_format", () => {
    assert.deepEqual(buildCandidateResponseFormat("ndjson"), {});
});

test("FlatJSON enables JSON object mode", () => {
    assert.deepEqual(buildCandidateResponseFormat("flat_json"), {response_format: {type: "json_object"}});
});

test("Structured Outputs supplies a strict candidate schema", () => {
    const result = buildCandidateResponseFormat("structured_json");
    assert.equal(result.response_format.type, "json_schema");
    assert.equal(result.response_format.json_schema.strict, true);
    assert.deepEqual(result.response_format.json_schema.schema.required, ["candidates", "recommended_candidate_id"]);
});

test("unknown output formats are rejected", () => {
    assert.throws(() => buildCandidateResponseFormat("yaml"), /Unknown candidate output format/);
});

test("a blank field produces an empty object", () => {
    assert.deepEqual(parseAdditionalRequestSettings("  \n"), {});
});

test("reasoning effort settings are accepted and merged at the top level", () => {
    const body = buildOpenAIRequestBody({model: "example"}, '{"reasoning_effort":"none"}');
    assert.deepEqual(body, {reasoning_effort: "none", model: "example"});
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
