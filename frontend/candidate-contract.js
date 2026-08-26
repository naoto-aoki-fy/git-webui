(function (root, factory) {
    const api = factory(root && root.OpenAIRequestSettings,
        typeof module === "object" && module.exports ? require("./openai-request-settings.js") : null);
    if (typeof module === "object" && module.exports) module.exports = api;
    if (root) root.CandidateContract = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function (browserApi, nodeApi) {
    "use strict";
    const api = browserApi || nodeApi;
    if (!api) throw new Error("openai-request-settings.js must be loaded before candidate-contract.js");
    return {
        candidateSchema: api.candidateSchema,
        parseCandidateResponse: api.parseCandidateResponse,
        validateCandidateResponse: api.validateCandidateResponse,
    };
});
