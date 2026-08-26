(function (root, factory) {
    const api = factory();
    if (typeof module === "object" && module.exports) module.exports = api;
    if (root) root.CodexAppServerClient = api.CodexAppServerClient;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
    "use strict";
    class CodexAppServerClient {
        constructor({WebSocketImpl} = {}) {
            this.WebSocketImpl = WebSocketImpl || globalThis.WebSocket;
            this.nextRequestId = 1; this.pendingRequests = new Map();
            this.notificationHandlers = new Map(); this.serverRequestHandlers = new Map();
            this.socket = null; this.initializedPromise = null; this.connectionGeneration = 0;
        }
        async connect(endpoint) {
            if (this.socket && this.socket.readyState === 1) return this.initializedPromise;
            if (this.socket && this.initializedPromise) return this.initializedPromise;
            if (!this.WebSocketImpl) throw new Error("WebSocket is not available in this browser.");
            const generation = ++this.connectionGeneration;
            this.socket = new this.WebSocketImpl(endpoint);
            this.initializedPromise = new Promise((resolve, reject) => {
                this.socket.addEventListener("open", resolve, {once: true});
                this.socket.addEventListener("error", () => reject(new Error("Unable to connect to Codex App Server.")), {once: true});
            }).then(async () => {
                this.socket.addEventListener("message", (event) => this._receive(event.data, generation));
                this.socket.addEventListener("close", () => this._closed(generation));
                await this.request("initialize", {clientInfo: {name: "git-webui", title: "git-webui", version: "1.0.0"}});
                this.notify("initialized");
            });
            return this.initializedPromise;
        }
        request(method, params = {}) {
            if (!this.socket || this.socket.readyState !== 1) return Promise.reject(new Error("Codex App Server is not connected."));
            const id = this.nextRequestId++;
            return new Promise((resolve, reject) => {
                this.pendingRequests.set(id, {resolve, reject});
                this.socket.send(JSON.stringify({jsonrpc: "2.0", id, method, params}));
            });
        }
        notify(method, params = {}) { this.socket.send(JSON.stringify({jsonrpc: "2.0", method, params})); }
        onNotification(method, handler) { return this._subscribe(this.notificationHandlers, method, handler); }
        onServerRequest(method, handler) { return this._subscribe(this.serverRequestHandlers, method, handler); }
        _subscribe(map, method, handler) {
            if (!map.has(method)) map.set(method, new Set()); map.get(method).add(handler);
            return () => map.get(method)?.delete(handler);
        }
        async listModels() {
            const models = []; let cursor;
            do {
                const result = await this.request("model/list", cursor ? {cursor} : {});
                models.push(...(result.data || result.models || [])); cursor = result.nextCursor;
            } while (cursor);
            return models;
        }
        _receive(data, generation) {
            if (generation !== this.connectionGeneration) return;
            let message; try { message = JSON.parse(data); } catch (_) { return; }
            if (Object.hasOwn(message, "id") && !message.method) {
                const pending = this.pendingRequests.get(message.id); if (!pending) return;
                this.pendingRequests.delete(message.id);
                message.error ? pending.reject(new Error(message.error.message || "Codex App Server request failed.")) : pending.resolve(message.result);
            } else if (message.method && Object.hasOwn(message, "id")) {
                const handlers = this.serverRequestHandlers.get(message.method);
                Promise.resolve(handlers?.values().next().value?.(message.params)).then(
                    (result) => this.socket?.send(JSON.stringify({jsonrpc: "2.0", id: message.id, result})),
                    (error) => this.socket?.send(JSON.stringify({jsonrpc: "2.0", id: message.id, error: {code: -32000, message: error.message}})),
                );
            } else (this.notificationHandlers.get(message.method) || []).forEach((handler) => handler(message.params || {}));
        }
        _closed(generation) {
            if (generation !== this.connectionGeneration) return;
            const error = new Error("Codex App Server connection closed.");
            this.pendingRequests.forEach(({reject}) => reject(error)); this.pendingRequests.clear(); this.socket = null;
        }
        close() {
            ++this.connectionGeneration; this.socket?.close();
            const error = new Error("Codex App Server connection closed.");
            this.pendingRequests.forEach(({reject}) => reject(error)); this.pendingRequests.clear();
            this.socket = null; this.initializedPromise = null;
        }
    }
    return {CodexAppServerClient};
});
