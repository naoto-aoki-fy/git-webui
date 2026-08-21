(function (root, factory) {
    const api = factory();
    if (typeof module === "object" && module.exports) module.exports = api;
    if (root) root.OpenAIRequestSettings = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
    "use strict";

    const RESERVED_REQUEST_KEYS = new Set(["model", "messages", "stream", "response_format"]);

    const parseAdditionalRequestSettings = (text) => {
        if (!text.trim()) return {};

        let settings;
        try {
            settings = JSON.parse(text);
        } catch (error) {
            throw new Error("Additional request parameters contain invalid JSON: " + error.message);
        }
        if (settings === null || Array.isArray(settings) || typeof settings !== "object") {
            throw new Error("Additional request parameters must be a JSON object (for example, {\"thinking\":{\"type\":\"disabled\"}}). Arrays, strings, numbers, and null are not allowed.");
        }
        const reservedKey = Object.keys(settings).find((key) => RESERVED_REQUEST_KEYS.has(key));
        if (reservedKey) {
            throw new Error('Additional request parameters cannot override reserved key "' + reservedKey + '".');
        }
        return settings;
    };

    const buildOpenAIRequestBody = (baseBody, additionalSettingsText) => ({
        ...parseAdditionalRequestSettings(additionalSettingsText),
        ...baseBody,
    });

    return {buildOpenAIRequestBody, parseAdditionalRequestSettings};
});
