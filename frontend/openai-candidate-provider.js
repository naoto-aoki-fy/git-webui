(function (root, factory) {
    const api = factory(root && root.OpenAIRequestSettings,
        typeof module === "object" && module.exports ? require("./openai-request-settings.js") : null);
    if (typeof module === "object" && module.exports) module.exports = api;
    if (root) root.OpenAICandidateProvider = api.OpenAICandidateProvider;
})(typeof globalThis !== "undefined" ? globalThis : this, function (browserApi, nodeApi) {
    "use strict";
    const settingsApi = browserApi || nodeApi;

    const determineModelListEndpoint = (completionEndpoint) => {
        const url = new URL(completionEndpoint);
        const marker = "/chat/completions";
        const markerIndex = url.pathname.replace(/\/$/, "").lastIndexOf(marker);
        url.pathname = markerIndex >= 0
            ? url.pathname.slice(0, markerIndex) + "/models"
            : url.pathname.replace(/\/$/, "") + "/models";
        url.search = "";
        url.hash = "";
        return url.toString();
    };

    class OpenAICandidateProvider {
        constructor(settings) { this.settings = settings; this.outputFormat = settings.outputFormat; }
        async listModels() {
            const response = await fetch(determineModelListEndpoint(this.settings.endpoint), {
                headers: {"Authorization": "Bearer " + this.settings.token},
            });
            if (!response.ok) throw new Error("Model endpoint returned HTTP " + response.status + ".");
            const body = await response.json();
            if (!Array.isArray(body.data)) throw new Error("Model endpoint response did not contain a model list.");
            return body.data;
        }
        async generateCandidates(request) {
            const response = await fetch(this.settings.endpoint, {
                method: "POST", signal: request.signal,
                headers: {"Content-Type": "application/json", "Authorization": "Bearer " + this.settings.token},
                body: JSON.stringify(settingsApi.buildOpenAIRequestBody({
                    ...settingsApi.buildCandidateResponseFormat(this.outputFormat),
                    model: this.settings.model,
                    messages: [{role: "system", content: request.systemPrompt}, {role: "user", content: request.userPrompt}],
                    stream: true,
                }, this.settings.additionalRequestParameters)),
            });
            if (!response.ok) throw new Error("Endpoint returned HTTP " + response.status + ".");
            const {content} = await settingsApi.readStreamingMessageContent(response, request.onContent, request.onReasoning);
            if (!content) throw new Error("Endpoint response did not contain message content.");
            return settingsApi.parseCandidateResponse(content, this.outputFormat);
        }
        async disconnect() {}
    }
    return {OpenAICandidateProvider, determineModelListEndpoint};
});
