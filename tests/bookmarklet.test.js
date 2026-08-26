const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const test = require("node:test");
const vm = require("node:vm");

const bookmarkletSource = readFileSync(
  new URL("../bookmarklet/codex.js", `file://${__filename}`),
  "utf8"
).replace("__BASE_URL__", JSON.stringify("https://example.test/"));

async function runBookmarklet(windowData) {
  const alerts = [];
  const window = {
    ...windowData,
    alert(message) {
      alerts.push(message);
    },
  };

  vm.runInNewContext(bookmarkletSource, {
    document: {},
    location: { pathname: "/tasks/task-123" },
    window,
  });
  await new Promise((resolve) => setImmediate(resolve));

  return alerts;
}

test("alerts the user to reload when task data is unavailable", async () => {
  const alerts = await runBookmarklet({});

  assert.deepEqual(alerts, [
    "Unable to find the task data. Please reload the Codex page and try the bookmarklet again.",
  ]);
});

test("alerts the user to reload when conversation data is unavailable", async () => {
  const alerts = await runBookmarklet({
    taskState: {
      task: { id: "task-123" },
      current_assistant_turn: {},
    },
  });

  assert.deepEqual(alerts, [
    "Unable to find the conversation data. Please reload the Codex page and try the bookmarklet again.",
  ]);
});
