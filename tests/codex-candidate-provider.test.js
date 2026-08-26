const test = require("node:test");
const assert = require("node:assert/strict");
const {CodexCandidateProvider} = require("../frontend/codex-candidate-provider.js");

const result = JSON.stringify({
    candidates: [
        {id: 1, style: "short", title: "Short", commit_message: "fix: one", recommended_adoption_level: 5, reason: "Clear."},
        {id: 2, style: "standard", title: "Standard", commit_message: "fix: two", recommended_adoption_level: 4, reason: "Complete."},
    ], recommended_candidate_id: 1,
});

class MockClient {
    constructor() { this.handlers = new Map(); this.requests = []; }
    onServerRequest() { return () => {}; }
    onNotification(name, fn) { this.handlers.set(name, fn); return () => this.handlers.delete(name); }
    async connect() {}
    async request(method, params) {
        this.requests.push({method, params});
        if (method === "thread/start") return {thread: {id: "thread-1"}};
        if (method === "turn/start") {
            queueMicrotask(() => {
                this.handlers.get("item/agentMessage/delta")({threadId: "thread-1", turnId: "turn-1", delta: "ignored"});
                this.handlers.get("item/completed")({threadId: "thread-1", turnId: "turn-1", item: {type: "agentMessage", text: result}});
                this.handlers.get("turn/completed")({threadId: "thread-1", turnId: "turn-1", turn: {status: "completed"}});
            });
            return {turn: {id: "turn-1"}};
        }
        return {};
    }
    close() {}
}

test("maps a Codex turn to normalized candidates and cleans up its thread", async () => {
    const client = new MockClient();
    const provider = new CodexCandidateProvider({endpoint: "ws://localhost", model: "codex"}, client);
    const seen = [];
    const value = await provider.generateCandidates({
        systemPrompt: "system", userPrompt: "patch", outputSchema: {}, signal: new AbortController().signal,
        onContent: (content) => seen.push(content), onReasoning: () => {},
    });
    assert.equal(value.recommended_candidate_id, 1);
    assert.equal(seen.at(-1), result);
    assert.deepEqual(client.requests.map(({method}) => method), ["thread/start", "turn/start", "thread/unsubscribe", "thread/delete"]);
    assert.equal(client.requests[1].params.outputSchema.constructor, Object);
});
