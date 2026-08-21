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

    const CANDIDATE_STYLES = new Set([
        "short", "standard", "detailed", "documentation-oriented", "alternative",
    ]);

    const CANDIDATE_OUTPUT_FORMATS = new Set(["ndjson", "flat_json", "structured_json"]);

    const candidateSchema = {
        type: "object",
        additionalProperties: false,
        properties: {
            candidates: {
                type: "array",
                minItems: 2,
                maxItems: 5,
                items: {
                    type: "object",
                    additionalProperties: false,
                    properties: {
                        id: {type: "integer"},
                        style: {type: "string", enum: Array.from(CANDIDATE_STYLES)},
                        title: {type: "string"},
                        commit_message: {type: "string"},
                        recommended_adoption_level: {type: "integer", minimum: 1, maximum: 5},
                        reason: {type: "string"},
                    },
                    required: ["id", "style", "title", "commit_message", "recommended_adoption_level", "reason"],
                },
            },
            recommended_candidate_id: {type: "integer"},
        },
        required: ["candidates", "recommended_candidate_id"],
    };

    const buildCandidateResponseFormat = (format) => {
        if (!CANDIDATE_OUTPUT_FORMATS.has(format)) throw new Error("Unknown candidate output format.");
        if (format === "ndjson") return {};
        if (format === "flat_json") return {response_format: {type: "json_object"}};
        return {
            response_format: {
                type: "json_schema",
                json_schema: {name: "commit_message_candidates", strict: true, schema: candidateSchema},
            },
        };
    };

    const validateCandidateResponse = (value) => {
        if (!value || Array.isArray(value) || typeof value !== "object" ||
            !Array.isArray(value.candidates) || value.candidates.length < 2 || value.candidates.length > 5) {
            throw new Error("The endpoint did not return 2–5 candidates.");
        }
        value.candidates.forEach((candidate, index) => {
            if (!candidate || Array.isArray(candidate) || typeof candidate !== "object" ||
                candidate.id !== index + 1 || !CANDIDATE_STYLES.has(candidate.style) ||
                typeof candidate.title !== "string" || typeof candidate.commit_message !== "string" ||
                !Number.isInteger(candidate.recommended_adoption_level) ||
                candidate.recommended_adoption_level < 1 || candidate.recommended_adoption_level > 5 ||
                typeof candidate.reason !== "string") {
                throw new Error("The endpoint returned an invalid candidate at position " + (index + 1) + ".");
            }
        });
        if (!Number.isInteger(value.recommended_candidate_id) ||
            !value.candidates.some((candidate) => candidate.id === value.recommended_candidate_id)) {
            throw new Error("The recommended candidate id is invalid.");
        }
        return value;
    };

    const parseCandidateResponse = (content, format) => {
        if (!CANDIDATE_OUTPUT_FORMATS.has(format)) throw new Error("Unknown candidate output format.");
        if (format === "ndjson") return parseAndNormalizeCandidateNDJSON(content);
        let value;
        try {
            value = JSON.parse(content);
        } catch (error) {
            throw new Error("The endpoint returned invalid JSON: " + error.message);
        }
        return validateCandidateResponse(value);
    };

    const parseAndNormalizeCandidateNDJSON = (content) => {
        const candidates = [];
        String(content).split(/\r?\n/).forEach((line, index) => {
            if (!line.trim()) return;
            let candidate;
            try {
                candidate = JSON.parse(line);
            } catch (error) {
                throw new Error("Invalid JSON on NDJSON line " + (index + 1) + ": " + error.message);
            }
            candidates.push(candidate);
        });

        if (candidates.length < 2 || candidates.length > 5) {
            throw new Error("The endpoint did not return 2–5 candidates.");
        }
        candidates.forEach((candidate, index) => {
            if (!candidate || Array.isArray(candidate) || typeof candidate !== "object" ||
                candidate.id !== index + 1 || !CANDIDATE_STYLES.has(candidate.style) ||
                typeof candidate.title !== "string" || typeof candidate.commit_message !== "string" ||
                !Number.isInteger(candidate.recommended_adoption_level) ||
                candidate.recommended_adoption_level < 1 || candidate.recommended_adoption_level > 5 ||
                typeof candidate.reason !== "string" || typeof candidate.recommended !== "boolean") {
                throw new Error("The endpoint returned an invalid candidate at position " + (index + 1) + ".");
            }
        });
        const recommendedCandidates = candidates.filter((candidate) => candidate.recommended);
        if (recommendedCandidates.length !== 1) {
            throw new Error("The endpoint must recommend exactly one candidate.");
        }
        return {
            candidates,
            recommended_candidate_id: recommendedCandidates[0].id,
        };
    };

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
        buildCandidateResponseFormat,
        parseCandidateResponse,
        parseAndNormalizeCandidateNDJSON,
        parseAdditionalRequestSettings,
        readStreamingMessageContent,
        splitDeltaContent,
    };
});
