(async function () {
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

  const baseUrl = "https://naoto-aoki-fy.github.io/git-webui/";
  const taskId = location.pathname.split("/").filter(Boolean).pop();

  const PLACEHOLDER_PR_TITLE = "Codex-generated pull request";
  const PLACEHOLDER_PR_MESSAGE =
    "Codex generated this pull request, but encountered an unexpected error after generation. This is a placeholder PR message.";

  function buildTurnMdListMarkdown(turnsInfo) {
    if (!turnsInfo || typeof turnsInfo !== "object") {
      return [
        "# Conversation (fallback)",
        "",
        "(turnsInfo not found; the placeholder PR message was used)",
      ].join("\n");
    }

    const turnMapping = turnsInfo.turn_mapping;
    if (!turnMapping || typeof turnMapping !== "object") {
      return [
        "# Conversation (fallback)",
        "",
        "(turn_mapping not found; the placeholder PR message was used)",
      ].join("\n");
    }

    const turnsSorted = Object.entries(turnMapping).sort(([, a], [, b]) => {
      const ta = a?.turn?.created_at ?? 0;
      const tb = b?.turn?.created_at ?? 0;
      return ta - tb;
    });

    const conversation = [];

    for (const [, entry] of turnsSorted) {
      const turn = entry?.turn;
      if (!turn) continue;

      let ioitemsKey;
      let roleLabel;

      if (turn.role === "user") {
        ioitemsKey = "input_items";
        roleLabel = "User";
      } else if (turn.role === "assistant") {
        ioitemsKey = "output_items";
        roleLabel = "Assistant";
      } else {
        continue; // Ignore system/tool/etc.
      }

      const ioitems = turn[ioitemsKey];
      if (!Array.isArray(ioitems)) continue;

      for (const item of ioitems) {
        if (item?.type !== "message") continue;

        const contentArr = item.content;
        if (!Array.isArray(contentArr)) continue;

        const text = contentArr.reduce((acc, c) => {
          if (c?.content_type === "text" && typeof c.text === "string") {
            return acc + c.text;
          }
          return acc;
        }, "");

        if (text.trim().length === 0) continue;

        conversation.push({
          role: roleLabel,
          text,
        });
      }
    }

    const md = [
      "# Conversation log",
      "",
      ...conversation.flatMap((c, i) => [
        `## ${i + 1}. ${c.role}`,
        "",
        c.text,
        "",
        "---",
        "",
      ]),
    ].join("\n");

    return md;
  }

  function reduceDiffContext(patchText, contextLines = 3) {
    if (!Number.isFinite(contextLines) || contextLines < 0) {
      throw new Error("contextLines must be a non-negative number");
    }

    const lines = patchText.replace(/\r\n/g, "\n").split("\n");

    const out = [];
    let i = 0;

    const isHunkHeader = (line) =>
      /^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@/.test(line);
    const isFileHeaderStart = (line) => line.startsWith("diff --git ");

    while (i < lines.length) {
      const line = lines[i];

      if (!isHunkHeader(line)) {
        // Outside hunks: copy through
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
      const m = headerLine.match(
        /^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@(.*)$/
      );
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
      const oldPart = oldLen === 0 ? "" + oldStart + ",0" : fmtRange(oldStart, oldLen);
      const newPart = newLen === 0 ? "" + newStart + ",0" : fmtRange(newStart, newLen);

      return "@@ -" + oldPart + " +" + newPart + " @@" + section;
    }

    function classifyLine(l) {
      // Unified diff body lines:
      // ' ' context
      // '+' addition
      // '-' deletion
      // '\ No newline at end of file' meta
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
      const oldBefore = new Array(items.length + 1);
      const newBefore = new Array(items.length + 1);
      let o = hh.oldStart;
      let n = hh.newStart;
      oldBefore[0] = o;
      newBefore[0] = n;

      for (let idx = 0; idx < items.length; idx++) {
        const it = items[idx];
        if (it.type === "ctx") {
          o += 1;
          n += 1;
        } else if (it.type === "del") {
          o += 1;
        } else if (it.type === "add") {
          n += 1;
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

      // If no changes, drop the hunk
      if (changeIdxs.length === 0) return [];

      // Build keep ranges around change blocks with context `ctx`
      const ranges = [];
      let blockStart = changeIdxs[0];
      let blockEnd = changeIdxs[0];

      for (let k = 1; k < changeIdxs.length; k++) {
        const idx = changeIdxs[k];
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

        const segOldStart = oldBefore[s];
        const segNewStart = newBefore[s];

        let oldLen = 0,
          newLen = 0;
        for (const it of segItems) {
          if (it.type === "ctx") {
            oldLen++;
            newLen++;
          } else if (it.type === "del") {
            oldLen++;
          } else if (it.type === "add") {
            newLen++;
          }
        }

        const headerLine = formatHunkHeader(segOldStart, oldLen, segNewStart, newLen, hh.section);
        const bodyOut = segItems.map((x) => x.text);

        if (segItems.some((x) => x.type === "add" || x.type === "del")) {
          result.push({ headerLine, bodyLines: bodyOut });
        }
      }

      return result;

      function expandBlock(startIdx, endIdx, ctxN) {
        // Expand left: include up to ctxN context lines immediately before the block.
        let s = startIdx;
        let ctxCount = 0;

        for (let j = startIdx - 1; j >= 0; j--) {
          const t = items[j].type;
          if (t === "ctx") {
            ctxCount++;
            s = j;
            if (ctxCount >= ctxN) break;
          } else if (t === "meta" || t === "other") {
            s = j;
          } else if (t === "add" || t === "del") {
            break;
          } else {
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

  // --- find taskInfo ---
  const resultFindTask = findFirstParentWhere(window, (parent) => {
    if (!Object.prototype.hasOwnProperty.call(parent, "task")) return false;
    const task = parent.task;
    return task && task.id == taskId; // keep == for string/number compatibility
  });

  if (!resultFindTask) {
    throw new Error("No matching element found (taskId=" + JSON.stringify(taskId) + ").");
  }

  const taskInfo = resultFindTask.parent;

  // --- find turnsInfo (outside buildTurnMdListMarkdown) ---
  const taskIdStr = String(taskId);
  const resultTurnMapping = findFirstParentWhere(window, (parent) => {
    if (!Object.prototype.hasOwnProperty.call(parent, "turn_mapping")) return false;
    const turnMapping = parent.turn_mapping;
    if (!turnMapping || typeof turnMapping !== "object") return false;

    const keys = Object.keys(turnMapping);
    if (keys.length === 0) return false;

    return keys.every((k) => String(k).startsWith(taskIdStr));
  });

  const turnsInfo = resultTurnMapping ? resultTurnMapping.parent : null;

  const branchName = taskInfo.current_assistant_turn.branch;
  const repoMapEntries = Object.entries(taskInfo.current_assistant_turn.environment.repo_map);
  if (repoMapEntries.length != 1) {
    throw Error("repo_map has " + repoMapEntries.length + " entrie(s). (expected: 1)");
  }
  const repoInfo = repoMapEntries[0][1];
  const repo = repoInfo.repository_full_name;

  const outputItems = taskInfo.current_assistant_turn.output_items;
  const pr = outputItems.find((value) => value && value.type === "pr");
  if (!pr) throw new Error("No PR output item found.");

  const isPlaceholderPr =
    pr.pr_title === PLACEHOLDER_PR_TITLE && pr.pr_message === PLACEHOLDER_PR_MESSAGE;

  const prMessage = isPlaceholderPr
    ? buildTurnMdListMarkdown(turnsInfo)
    : "# " + pr.pr_title + "\n\n" + pr.pr_message;

  const patchOriginal = pr?.output_diff?.diff ?? "";
  const patch = reduceDiffContext(patchOriginal, 5);

  const url =
    baseUrl +
    "#?" +
    "repository_url=" +
    encodeURIComponent("git@github.com:" + repo) +
    "&branch=" +
    encodeURIComponent(branchName) +
    "&branch_mode=default" +
    "&allow_empty_commit=false" +
    "&new_branch=" +
    "&base_commit=" +
    "&pr_message=" +
    encodeURIComponent(prMessage) +
    "&patch=" +
    encodeURIComponent(patch);

  const a = document.createElement("a");
  a.innerHTML = "dummy";
  a.style.display = "none";

  a.addEventListener(
    "click",
    function () {
      window.open(url, "_blank");
      return false;
    },
    { once: true }
  );

  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
})();
