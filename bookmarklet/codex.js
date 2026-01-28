(async function () {
  const baseUrl = "https://naoto-aoki-fy.github.io/git-webui/";
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

  function reduceDiffContext(patchText, contextLines = 3) {
    if (!Number.isFinite(contextLines) || contextLines < 0) {
      throw new Error("contextLines must be a non-negative number");
    }
  
    const lines = patchText.replace(/\r\n/g, "\n").split("\n");
  
    const out = [];
    let i = 0;
  
    const isHunkHeader = (line) => /^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@/.test(line);
    const isFileHeaderStart = (line) => line.startsWith("diff --git ");
  
    while (i < lines.length) {
      const line = lines[i];
  
      if (!isHunkHeader(line)) {
        // Outside hunks: just copy through
        out.push(line);
        i++;
        continue;
      }
  
      // We are at a hunk header
      const hunkHeaderLine = line;
      const hunkHeader = parseHunkHeader(hunkHeaderLine);
  
      i++; // move to hunk body
  
      // Collect hunk body lines until next hunk header or next file header or EOF
      const hunkBodyStart = i;
      while (i < lines.length && !isHunkHeader(lines[i]) && !isFileHeaderStart(lines[i])) {
        i++;
      }
      const hunkBodyLines = lines.slice(hunkBodyStart, i);
  
      // Trim this hunk
      const trimmedHunks = trimOneHunk(hunkHeader, hunkBodyLines, contextLines);
  
      // Output trimmed hunks (if any)
      for (const th of trimmedHunks) {
        out.push(th.headerLine);
        out.push(...th.bodyLines);
      }
    }
  
    return out.reduce((acc, line) => acc + line + "\n", "");
  
    // ---- helpers ----
  
    function parseHunkHeader(headerLine) {
      // @@ -oldStart,oldLen +newStart,newLen @@ optionalSection
      const m = headerLine.match(/^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@(.*)$/);
      if (!m) throw new Error("Invalid hunk header: " + headerLine);
  
      const oldStart = parseInt(m[1], 10);
      const oldLen = m[2] != null ? parseInt(m[2], 10) : 1;
      const newStart = parseInt(m[3], 10);
      const newLen = m[4] != null ? parseInt(m[4], 10) : 1;
      const section = m[5] || ""; // includes leading space if present
  
      return { oldStart, oldLen, newStart, newLen, section, raw: headerLine };
    }
  
    function formatHunkHeader(oldStart, oldLen, newStart, newLen, section) {
      const fmtRange = (start, len) => {
        if (len === 1) return "" + start;
        return "" + start + "," + len;
      };
      // For 0-length hunks, keep ",0" explicitly (common & safe)
      const oldPart = lenIsZero(oldLen) ? "" + oldStart + ",0" : fmtRange(oldStart, oldLen);
      const newPart = lenIsZero(newLen) ? "" + newStart + ",0" : fmtRange(newStart, newLen);

      return "@@ -" + oldPart + " +" + newPart + " @@" + section;
    }
  
    function lenIsZero(n) {
      return n === 0;
    }
  
    function classifyLine(l) {
      // Unified diff body lines:
      // ' ' context
      // '+' addition
      // '-' deletion
      // '\ No newline at end of file' meta
      // Other lines can exist but in hunks they should follow the above.
      if (l.startsWith("\\ No newline at end of file")) return { type: "meta", text: l };
      const ch = l[0];
      if (ch === " ") return { type: "ctx", text: l };
      if (ch === "+") return { type: "add", text: l };
      if (ch === "-") return { type: "del", text: l };
      // Fallback: treat as context-ish (won't affect counters)
      return { type: "other", text: l };
    }
  
    function trimOneHunk(hh, bodyLines, ctx) {
      const items = bodyLines.map(classifyLine);
  
      // Precompute old/new line number before each item index
      // "before" means the line number at the position where this item applies.
      const oldBefore = new Array(items.length + 1);
      const newBefore = new Array(items.length + 1);
      let o = hh.oldStart;
      let n = hh.newStart;
      oldBefore[0] = o;
      newBefore[0] = n;
  
      for (let idx = 0; idx < items.length; idx++) {
        const it = items[idx];
        // record before processing idx+1
        if (it.type === "ctx") {
          o += 1; n += 1;
        } else if (it.type === "del") {
          o += 1;
        } else if (it.type === "add") {
          n += 1;
        } else {
          // meta/other: no counters
        }
        oldBefore[idx + 1] = o;
        newBefore[idx + 1] = n;
      }
  
      // Find change indices (add/del)
      const changeIdxs = [];
      for (let idx = 0; idx < items.length; idx++) {
        const t = items[idx].type;
        if (t === "add" || t === "del") changeIdxs.push(idx);
      }
  
      // If no changes, drop the hunk (rare but safe)
      if (changeIdxs.length === 0) return [];
  
      // Build keep ranges around change blocks with context `ctx`
      // Start from each contiguous "change block" (where there is at least one add/del;
      // context lines between changes are included in the same block if they are adjacent in index)
      const ranges = [];
      let blockStart = changeIdxs[0];
      let blockEnd = changeIdxs[0];
  
      const isChange = (idx) => {
        const t = items[idx]?.type;
        return t === "add" || t === "del";
      };
  
      for (let k = 1; k < changeIdxs.length; k++) {
        const idx = changeIdxs[k];
        // If there is any gap, we might still want to merge if the gap is small
        // but the later splitting logic will do the right thing. Here we define a "block"
        // as changes not separated by another change gap; we'll expand with context later.
        if (idx === blockEnd + 1) {
          blockEnd = idx;
        } else {
          ranges.push(expandBlock(blockStart, blockEnd, ctx));
          blockStart = idx;
          blockEnd = idx;
        }
      }
      ranges.push(expandBlock(blockStart, blockEnd, ctx));
  
      // Merge overlapping/adjacent ranges
      ranges.sort((a, b) => a[0] - b[0]);
      const merged = [];
      for (const r of ranges) {
        if (merged.length === 0) {
          merged.push(r);
        } else {
          const last = merged[merged.length - 1];
          if (r[0] <= last[1] + 1) {
            last[1] = Math.max(last[1], r[1]);
          } else {
            merged.push(r);
          }
        }
      }
  
      // Convert merged ranges into hunks, recomputing header ranges.
      const result = [];
      for (const [s, e] of merged) {
        const segItems = items.slice(s, e + 1);
  
        // If segment starts with meta lines (shouldn't), shift start forward
        // but preserve meta if it belongs to previous kept line. We'll keep meta only if included by range.
        // Compute old/new starts at index s:
        const segOldStart = oldBefore[s];
        const segNewStart = newBefore[s];
  
        // Compute lengths for segment
        let oldLen = 0, newLen = 0;
        for (const it of segItems) {
          if (it.type === "ctx") { oldLen++; newLen++; }
          else if (it.type === "del") { oldLen++; }
          else if (it.type === "add") { newLen++; }
          else {
            // meta/other: no lengths
          }
        }
  
        const headerLine = formatHunkHeader(segOldStart, oldLen, segNewStart, newLen, hh.section);
        const bodyOut = segItems.map(x => x.text);
  
        // Ensure segment has at least one change (should, but double-check)
        if (segItems.some(x => x.type === "add" || x.type === "del")) {
          result.push({ headerLine, bodyLines: bodyOut });
        }
      }
  
      return result;
  
      function expandBlock(startIdx, endIdx, ctxN) {
        // Expand left: include up to ctxN context lines immediately before the block.
        // We'll expand by indices, but only count ctx lines; include intervening meta/other if within that index span.
        let s = startIdx;
        let ctxCount = 0;
  
        for (let j = startIdx - 1; j >= 0; j--) {
          const t = items[j].type;
          if (t === "ctx") {
            ctxCount++;
            s = j;
            if (ctxCount >= ctxN) break;
          } else if (t === "meta" || t === "other") {
            // keep moving left without counting as context
            s = j;
          } else if (t === "add" || t === "del") {
            // hit another change: stop (it will be in another block)
            break;
          } else {
            // unknown: treat like non-counting
            s = j;
          }
        }
  
        // Expand right: include up to ctxN context lines immediately after the block.
        let e = endIdx;
        ctxCount = 0;
  
        for (let j = endIdx + 1; j < items.length; j++) {
          const t = items[j].type;
          if (t === "ctx") {
            ctxCount++;
            e = j;
            if (ctxCount >= ctxN) break;
          } else if (t === "meta" || t === "other") {
            e = j;
          } else if (t === "add" || t === "del") {
            break;
          } else {
            e = j;
          }
        }
  
        return [s, e];
      }
    }
  }

  const resultFindTask = findFirstParentWhere(window, (parent, path) => {
    if (!Object.prototype.hasOwnProperty.call(parent, "task")) return false;
    const task = parent.task;
    return task && task.id == taskId; // keep == for string/number compatibility
  });

  if (!resultFindTask) {
    throw new Error("No matching element found (taskId=" + JSON.stringify(taskId) + ").");
  }

  const data = resultFindTask.parent;

  console.log({ data });

  const repo = data.task.task_status_display.environment_label;
  const branchName = data.task.task_status_display.branch_name;
  const outputItems = data.current_assistant_turn.output_items;
  const pr = outputItems.find((value, index, obj) => {
    if ("type" in value) {
      const type = value.type;
      if (type == "pr") {
        return true;
      }
    }
    return false;
  });
  const prMessage = "# " + pr.pr_title + "\n\n" + pr.pr_message;
  const patchOriginal = pr.output_diff.diff;
  const patch = reduceDiffContext(patchOriginal, 5);
  console.log({repo, branchName, prMessage, patch});

  const url =
    baseUrl +
    "#?" +
    "repository_url=" + encodeURIComponent("git@github.com:" + repo) +
    "&branch=" + encodeURIComponent(branchName) +
    "&branch_mode=default" +
    "&allow_empty_commit=false" +
    "&new_branch=" +
    "&base_commit=" +
    "&pr_message=" + encodeURIComponent(prMessage) +
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
