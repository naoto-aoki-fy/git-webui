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
            throw new Error("Additional request parameters must be a JSON object (for example, {\"reasoning_effort\":\"none\"}). Arrays, strings, numbers, and null are not allowed.");
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

    const THINKING_PART_TYPES = new Set(["thinking", "reasoning", "reasoning_content"]);

    const partText = (part) => {
        if (typeof part === "string") return part;
        if (!part || typeof part !== "object") return "";
        if (typeof part.text === "string") return part.text;
        if (typeof part.thinking === "string") return part.thinking;
        if (typeof part.reasoning_content === "string") return part.reasoning_content;
        return "";
    };

    const splitDeltaContent = (delta) => {
        let content = "";
        let thinking = "";
        const append = (value, defaultToThinking) => {
            const parts = Array.isArray(value) ? value : [value];
            parts.forEach((part) => {
                const text = partText(part);
                if (!text) return;
                const isThinking = defaultToThinking || (
                    part && typeof part === "object" && THINKING_PART_TYPES.has(part.type)
                );
                if (isThinking) thinking += text;
                else content += text;
            });
        };
        append(delta?.content, false);
        append(delta?.reasoning_content, true);
        return {content, thinking};
    };

    const readStreamingMessageContent = async (response, onContent = () => {}, onThinking = () => {}) => {
        if (!response.body) throw new Error("Endpoint response did not contain a response stream.");
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let eventBuffer = "";
        let content = "";
        let thinking = "";

        const processEvent = (eventText) => {
            const data = eventText.split(/\r?\n/)
                .filter((line) => line.startsWith("data:"))
                .map((line) => line.slice(5).trimStart())
                .join("\n");
            if (!data || data === "[DONE]") return;
            let payload;
            try {
                payload = JSON.parse(data);
            } catch (error) {
                throw new Error("Endpoint returned an invalid streaming event.", {cause: error});
            }
            if (payload.error) {
                throw new Error(payload.error.message || "Endpoint returned a streaming error.");
            }
            const additions = splitDeltaContent(payload?.choices?.[0]?.delta);
            if (additions.content) {
                content += additions.content;
                onContent(content);
            }
            if (additions.thinking) {
                thinking += additions.thinking;
                onThinking(thinking);
            }
        };

        while (true) {
            const {value, done} = await reader.read();
            eventBuffer += decoder.decode(value || new Uint8Array(), {stream: !done});
            const events = eventBuffer.split(/\r?\n\r?\n/);
            eventBuffer = events.pop() || "";
            events.forEach(processEvent);
            if (done) break;
        }
        if (eventBuffer.trim()) processEvent(eventBuffer);
        return {content, thinking};
    };

    return {
        buildOpenAIRequestBody,
        parseAdditionalRequestSettings,
        readStreamingMessageContent,
        splitDeltaContent,
    };
});
