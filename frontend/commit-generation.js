(function (root) {
    "use strict";

    const outputFormat = (provider, codexFormat, openAIFormat) =>
        provider === "codex" ? codexFormat : openAIFormat;
    root.CommitGeneration = Object.freeze({outputFormat});
})(globalThis);
