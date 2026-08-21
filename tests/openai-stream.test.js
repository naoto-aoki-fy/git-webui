const test = require("node:test");
const assert = require("node:assert/strict");
const {readStreamingMessageContent} = require("../frontend/openai-request-settings.js");

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
