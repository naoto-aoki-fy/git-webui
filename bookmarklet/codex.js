(async function () {
  const baseUrl = "http://endpoint";
  const taskId = location.pathname.split("/").filter(Boolean).pop();

  // --- helpers: Uint8Array <-> Base64URL ---
  function bytesToBase64Url(bytes) {
    let binary = "";
    const chunkSize = 0x8000;
    for (let i = 0; i < bytes.length; i += chunkSize) {
      binary += String.fromCharCode(...bytes.subarray(i, i + chunkSize));
    }
    const base64 = btoa(binary);
    return base64.replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
  }

  // --- gzip-compress a string -> URI-safe string ---
  async function gzipBase64(str) {
    const input = new TextEncoder().encode(str);

    const cs = new CompressionStream("gzip");
    const writer = cs.writable.getWriter();
    writer.write(input);
    writer.close();

    const compressed = new Uint8Array(await new Response(cs.readable).arrayBuffer());

    const b64url = bytesToBase64Url(compressed);
    // Per requirements, use encodeURIComponent (often unnecessary for Base64URL, but makes it safer)
    return encodeURIComponent(b64url);
  }

  function findFirstParentWhere(obj, matcher) {
    const visited = new WeakSet();

    function dfs(node, path) {
      if (node === null) return null;

      const t = typeof node;
      if (t !== "object") return null;

      if (typeof node === "function") return null;

      const anyNode = node;

      if (visited.has(anyNode)) return null;
      visited.add(anyNode);

      if (matcher(anyNode, path)) {
        return { parent: anyNode, path };
      }

      if (Array.isArray(anyNode)) {
        for (let i = 0; i < anyNode.length; i++) {
          const hit = dfs(anyNode[i], path.concat(i));
          if (hit) return hit;
        }
      } else {
        for (const [k, v] of Object.entries(anyNode)) {
          const hit = dfs(v, path.concat(k));
          if (hit) return hit;
        }
      }

      return null;
    }

    return dfs(obj, []);
  }

  const result = findFirstParentWhere(window, (parent, path) => {
    if (!Object.prototype.hasOwnProperty.call(parent, "task")) return false;
    const task = parent.task;
    return task && task.id == taskId; // keep == for string/number compatibility
  });

  if (!result) {
    throw new Error("No matching element found (taskId=" + JSON.stringify(taskId) + ").");
  }

  const data = result.parent;

  console.log({ data });

  const repo = data.task.task_status_display.environment_label;
  const branchName = data.task.task_status_display.branch_name;
  const outputItems = data.current_diff_task_turn.output_items;
  const pr = outputItems.find((value, index, obj) => {
    if ("type" in value) {
      const type = value.type;
      if (type == "pr") {
        return true;
      }
    }
    return false;
  });
  const patch = pr.output_diff.diff;
  console.log({ repo, branchName, patch });

  const url =
    baseUrl +
    "/?" +
    "repository_url=" + encodeURIComponent("git@github.com:" + repo) +
    "&branch=" + encodeURIComponent(branchName) +
    "&branch_mode=default" +
    "&allow_empty_commit=false" +
    "&patch=" + encodeURIComponent(await gzipBase64(patch));

  console.log({ url });

  const a = document.createElement("a");
  a.innerHTML = "dummy";
  a.style.display = "none";

  a.addEventListener(
    "click",
    function () {
      const w = window.open(url, "_blank");
      return false;
    },
    { once: true }
  );

  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);

})();
