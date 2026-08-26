(function (root, factory) {
    const api = factory(
        root && root.CodexAppServerClient,
        root && root.CandidateContract,
        typeof module === "object" && module.exports ? require("./codex-app-server-client.js").CodexAppServerClient : null,
        typeof module === "object" && module.exports ? require("./candidate-contract.js") : null,
    );
    if (typeof module === "object" && module.exports) module.exports = api;
    if (root) root.CodexCandidateProvider = api.CodexCandidateProvider;
})(typeof globalThis !== "undefined" ? globalThis : this, function (BrowserClient, browserContract, NodeClient, nodeContract) {
    "use strict";
    const Client = BrowserClient || NodeClient;
    const contract = browserContract || nodeContract;
    const approvalMethods = ["item/commandExecution/requestApproval", "item/fileChange/requestApproval"];

    class CodexCandidateProvider {
        constructor(settings, client = new Client()) {
            this.settings = settings; this.client = client; this.outputFormat = "structured_json";
            approvalMethods.forEach((method) => client.onServerRequest(method, () => ({decision: "decline"})));
        }
        async connect() { await this.client.connect(this.settings.endpoint); }
        async listModels() { await this.connect(); return this.client.listModels(); }
        async generateCandidates(request) {
            await this.connect();
            const thread = await this.client.request("thread/start", {
                ephemeral: false,
                approvalPolicy: "never",
                sandbox: "read-only",
            });
            const threadId = thread.thread?.id || thread.threadId || thread.id;
            if (!threadId) throw new Error("Codex App Server did not return a thread id.");
            const generation = {threadId, turnId: null, content: "", reasoning: "", cancelled: false, settled: false};
            let completeResolve; let completeReject;
            const completed = new Promise((resolve, reject) => { completeResolve = resolve; completeReject = reject; });
            const scoped = (params) => params.threadId === threadId && (!generation.turnId || !params.turnId || params.turnId === generation.turnId);
            const subscriptions = [
                this.client.onNotification("turn/started", (params) => {
                    if (scoped(params)) generation.turnId = params.turn?.id || params.turnId || generation.turnId;
                }),
                this.client.onNotification("item/agentMessage/delta", (params) => {
                    if (!scoped(params)) return; generation.content += params.delta || ""; request.onContent(generation.content);
                }),
                this.client.onNotification("item/reasoning/summaryTextDelta", (params) => {
                    if (!scoped(params)) return; generation.reasoning += params.delta || ""; request.onReasoning(generation.reasoning);
                }),
                this.client.onNotification("item/completed", (params) => {
                    if (!scoped(params)) return;
                    const item = params.item || {};
                    if (item.type === "agentMessage") {
                        const finalized = typeof item.text === "string" ? item.text :
                            (item.content || []).map((part) => part.text || "").join("");
                        if (finalized) { generation.content = finalized; request.onContent(finalized); }
                    }
                }),
                this.client.onNotification("turn/completed", (params) => {
                    if (!scoped(params) || generation.settled) return; generation.settled = true;
                    const status = params.turn?.status || params.status || "completed";
                    if (status === "completed") completeResolve();
                    else completeReject(new Error(status === "interrupted" ? "Generation stopped." : "Codex turn " + status + "."));
                }),
            ];
            const abort = async () => {
                generation.cancelled = true;
                if (generation.turnId) {
                    try { await this.client.request("turn/interrupt", {threadId, turnId: generation.turnId}); } catch (_) {}
                }
            };
            request.signal?.addEventListener("abort", abort, {once: true});
            try {
                const turn = await this.client.request("turn/start", {
                    threadId,
                    input: [{type: "text", text: request.systemPrompt + "\n\nDo not use tools. Use only the supplied text.\n\n" + request.userPrompt}],
                    ...(this.settings.model ? {model: this.settings.model} : {}),
                    outputSchema: request.outputSchema,
                });
                generation.turnId = generation.turnId || turn.turn?.id || turn.turnId || turn.id;
                if (generation.cancelled) await abort();
                await completed;
                if (!generation.content) throw new Error("Codex turn did not contain an agent message.");
                return contract.parseCandidateResponse(generation.content, "structured_json");
            } finally {
                request.signal?.removeEventListener("abort", abort);
                subscriptions.forEach((unsubscribe) => unsubscribe());
                try { await this.client.request("thread/unsubscribe", {threadId}); } catch (_) {}
                try { await this.client.request("thread/delete", {threadId}); } catch (_) {}
            }
        }
        async disconnect() { this.client.close(); }
    }
    return {CodexCandidateProvider};
});
