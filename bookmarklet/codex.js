(async function () {
  const baseUrl = "http://endpoint";
  const accessToken = JSON.parse(document.getElementById("client-bootstrap").innerHTML).session.accessToken
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

  try {
    const res = await fetch(
      "https://chatgpt.com/backend-api/wham/tasks/" + taskId,
      {
        method: "GET",
        headers: { authorization: "Bearer " + accessToken },
      }
    );

    // Convert to text first in case we want details even for non-2xx responses
    const text = await res.text();

    if (!res.ok) {
      throw new Error(`HTTP ${res.status} ${res.statusText}: ${text}`);
    }

    // Parse if JSON is expected to be returned
    const data = text ? JSON.parse(text) : null;

    console.log("response:", data);
    // return data;

    const repo = data.task.task_status_display.environment_label;
    const branchName = data.task.task_status_display.branch_name;
    const outputItems = data.current_diff_task_turn.output_items;
    let patch;
    for (let oin = 0; oin < outputItems.length; oin++) {
      patch = data.current_diff_task_turn.output_items[oin].output_diff?.diff;
    }
    console.log({ repo, branchName, patch });

    const url =
      baseUrl +
      "/?" +
      "repository_url=" + encodeURIComponent("git@github.com:" + repo) +
      "&branch=" + encodeURIComponent(branchName) +
      "&branch_mode=default" +
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

  } catch (err) {
    console.error("fetch error:", err);
    throw err; // Re-throw if the caller also needs to handle it
  }

})();
