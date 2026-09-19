const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const test = require("node:test");
const vm = require("node:vm");

const source = readFileSync(
    new URL("../frontend/backend-client.js", `file://${__filename}`),
    "utf8",
);

const loadClient = (fetch) => {
    const context = {fetch};
    context.globalThis = context;
    vm.runInNewContext(source, context);
    return context.BackendClient;
};

test("reports an actionable error when the request body is too large", async () => {
    const client = loadClient(async () => new Response("Maximum request body size exceeded", {
        status: 413,
    }));

    await assert.rejects(
        client.request((path) => path, "POST", "/submit", {patch: "large"}),
        /patch is too large.*maximum request size: 16 MiB/i,
    );
});

test("includes backend error details for other failed requests", async () => {
    const client = loadClient(async () => new Response("Invalid form payload", {status: 400}));

    await assert.rejects(
        client.request((path) => path, "POST", "/submit", {}),
        /Request failed: 400 \(Invalid form payload\)/,
    );
});
