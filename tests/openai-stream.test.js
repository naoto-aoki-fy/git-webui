const test = require("node:test");
const assert = require("node:assert/strict");
const {
    parseCandidateResponse,
    parseAndNormalizeCandidateNDJSON,
    readStreamingMessageContent,
} = require("../frontend/openai-request-settings.js");

const candidate = (id, recommended = false, overrides = {}) => JSON.stringify({
    id,
    style: id === 1 ? "short" : "standard",
    title: `Candidate ${id}`,
    commit_message: `feat: candidate ${id}`,
    recommended_adoption_level: 5,
    reason: "Clear and searchable.",
    recommended,
    ...overrides,
});

const responseFor = (events) => new Response(events.map((data) => `data: ${data}\n\n`).join(""));

test("reads normal content and stops cleanly at DONE", async () => {
    const seen = [];
    const result = await readStreamingMessageContent(responseFor([
        JSON.stringify({choices: [{delta: {content: "candidate"}}]}),
        "[DONE]",
    ]), (content) => seen.push(content));
    assert.deepEqual(result, {content: "candidate", thinking: ""});
    assert.deepEqual(seen, ["candidate"]);
});

test("keeps alternating reasoning_content separate from candidate content", async () => {
    const thinkingSeen = [];
    const result = await readStreamingMessageContent(responseFor([
        JSON.stringify({choices: [{delta: {reasoning_content: "plan "}}]}),
        JSON.stringify({choices: [{delta: {content: "{\"c\":"}}]}),
        JSON.stringify({choices: [{delta: {reasoning_content: "check"}}]}),
        JSON.stringify({choices: [{delta: {content: "1}"}}]}),
    ]), undefined, (thinking) => thinkingSeen.push(thinking));
    assert.deepEqual(result, {content: '{"c":1}', thinking: "plan check"});
    assert.deepEqual(thinkingSeen, ["plan ", "plan check"]);
});

test("extracts string and text-object array forms and thinking content parts", async () => {
    const result = await readStreamingMessageContent(responseFor([
        JSON.stringify({choices: [{delta: {
            content: ["A", {text: "B"}, {type: "thinking", text: "C"}],
            reasoning_content: ["D", {text: "E"}, {type: "reasoning", thinking: "F"}],
        }}]}),
    ]));
    assert.deepEqual(result, {content: "AB", thinking: "CDEF"});
});

test("does not call the thinking callback for responses without thinking", async () => {
    let called = false;
    await readStreamingMessageContent(responseFor([
        JSON.stringify({choices: [{delta: {content: [{text: "JSON"}]}}]}),
    ]), undefined, () => { called = true; });
    assert.equal(called, false);
});

test("parses multiline NDJSON and normalizes the recommended candidate", () => {
    const result = parseAndNormalizeCandidateNDJSON(candidate(1) + "\n" + candidate(2, true) + "\n");
    assert.equal(result.candidates.length, 2);
    assert.equal(result.recommended_candidate_id, 2);
});

test("parses CRLF, blank lines, and input without a trailing newline", () => {
    const result = parseAndNormalizeCandidateNDJSON(candidate(1, true) + "\r\n\r\n" + candidate(2));
    assert.equal(result.recommended_candidate_id, 1);
});

test("parses NDJSON assembled from a stream chunk split in the middle of a JSON line", async () => {
    const ndjson = candidate(1, true) + "\n" + candidate(2);
    const event = `data: ${JSON.stringify({choices: [{delta: {content: ndjson}}]})}\n\n`;
    const splitAt = event.indexOf("candidate 1");
    const response = new Response(new ReadableStream({
        start(controller) {
            controller.enqueue(new TextEncoder().encode(event.slice(0, splitAt)));
            controller.enqueue(new TextEncoder().encode(event.slice(splitAt)));
            controller.close();
        },
    }));
    const {content} = await readStreamingMessageContent(response);
    assert.equal(parseAndNormalizeCandidateNDJSON(content).candidates.length, 2);
});

test("reports the physical line number for invalid JSON", () => {
    assert.throws(
        () => parseAndNormalizeCandidateNDJSON(candidate(1, true) + "\n\nnot json\n" + candidate(2)),
        /NDJSON line 3/,
    );
});

test("rejects zero or multiple recommended candidates", () => {
    assert.throws(
        () => parseAndNormalizeCandidateNDJSON(candidate(1) + "\n" + candidate(2)),
        /exactly one candidate/,
    );
    assert.throws(
        () => parseAndNormalizeCandidateNDJSON(candidate(1, true) + "\n" + candidate(2, true)),
        /exactly one candidate/,
    );
});

test("parses and validates FlatJSON and Structured Outputs content", () => {
    const value = {
        candidates: [JSON.parse(candidate(1)), JSON.parse(candidate(2))].map(({recommended, ...item}) => item),
        recommended_candidate_id: 2,
    };
    assert.deepEqual(parseCandidateResponse(JSON.stringify(value), "flat_json"), value);
    assert.deepEqual(parseCandidateResponse(JSON.stringify(value), "structured_json"), value);
});

test("rejects invalid JSON and invalid recommended ids in JSON formats", () => {
    assert.throws(() => parseCandidateResponse("not json", "flat_json"), /invalid JSON/);
    const value = {
        candidates: [JSON.parse(candidate(1)), JSON.parse(candidate(2))].map(({recommended, ...item}) => item),
        recommended_candidate_id: 99,
    };
    assert.throws(() => parseCandidateResponse(JSON.stringify(value), "structured_json"), /recommended candidate id/);
});
