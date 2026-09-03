(function (root) {
    "use strict";

    const read = (key) => {
        try {
            const raw = root.localStorage.getItem(key);
            if (!raw) return {};
            const value = JSON.parse(raw);
            return value && typeof value === "object" ? value : {};
        } catch (error) {
            console.warn("Failed to read draft input", error);
            return {};
        }
    };

    const write = (key, draft) => root.localStorage.setItem(key, JSON.stringify(draft));
    root.DraftStorage = Object.freeze({read, write});
})(globalThis);
