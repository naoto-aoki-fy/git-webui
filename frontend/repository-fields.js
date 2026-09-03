(function (root) {
    "use strict";

    const normalize = (value) => {
        if (!value || !value.trim()) return "";
        const trimmed = value.trim();
        let normalized = trimmed;
        if (trimmed.includes("://")) {
            try {
                const url = new URL(trimmed);
                normalized = url.hostname + url.pathname;
            } catch (_) {
                normalized = trimmed;
            }
        } else {
            const match = trimmed.match(/^(?:[^@]+@)?([^:]+):(.+)$/);
            if (match) normalized = match[1] + "/" + match[2];
        }
        return normalized.replace(/^\/+|\/+$/g, "").replace(/\.git$/i, "").toLowerCase();
    };

    const parts = (value) => normalize(value).split("/").filter(Boolean);
    const name = (value) => parts(value).at(-1) || "";
    const owner = (value) => {
        const values = parts(value);
        return values.length >= 3 ? values[1] : values[0] || "";
    };
    root.RepositoryFields = Object.freeze({normalize, name, owner});
})(globalThis);
