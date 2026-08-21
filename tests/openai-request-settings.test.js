const test = require("node:test");
const assert = require("node:assert/strict");
const {
    buildOpenAIRequestBody,
    parseFlatCandidateResponse,
    parseAdditionalRequestSettings,
} = require("../frontend/openai-request-settings.js");

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

test("converts a flat candidate response into renderable candidates", () => {
    const result = parseFlatCandidateResponse({
        candidate_count: 2,
        candidate_1_style: "short",
        candidate_1_title: "Short",
        candidate_1_commit_message: "fix: one",
        candidate_1_recommended_adoption_level: 5,
        candidate_1_reason: "Clear.",
        candidate_2_style: "detailed",
        candidate_2_title: "Detailed",
        candidate_2_commit_message: "fix: two",
        candidate_2_recommended_adoption_level: 4,
        candidate_2_reason: "Complete.",
        recommended_candidate_id: 1,
    });
    assert.equal(result.recommended_candidate_id, 1);
    assert.deepEqual(result.candidates.map(({id, style}) => ({id, style})), [
        {id: 1, style: "short"},
        {id: 2, style: "detailed"},
    ]);
});

test("rejects nested and incomplete candidate responses", () => {
    assert.throws(() => parseFlatCandidateResponse({candidates: []}), /flat JSON object/);
    assert.throws(() => parseFlatCandidateResponse({candidate_count: 2}), /invalid candidate/);
});
