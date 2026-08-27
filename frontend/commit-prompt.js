(function (root, factory) {
    const api = factory();
    if (typeof module === "object" && module.exports) module.exports = api;
    root.CommitPrompt = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
    const quoteForPrompt = (content, language) => {
        const value = String(content ?? "");
        const runs = value.match(/`+/g) || [];
        const longest = runs.reduce((length, run) => Math.max(length, run.length), 0);
        const fence = "`".repeat(Math.max(3, longest + 1));
        return fence + language + "\n" + value + (value.endsWith("\n") ? "" : "\n") + fence + "\n";
    };

    const buildUserChangesPrompt = ({branchMode, memo, patch, files, filePath, fileContent, overwrite}) => {
        const description = String(memo ?? "");
        if (branchMode !== "add_file") return description + "\n\n" + quoteForPrompt(patch, "diff");
        const entries = Array.isArray(files) ? files : [{path: filePath, content: fileContent, overwrite}];
        const details = entries.filter((file) => String(file?.path ?? "").trim() && String(file?.content ?? ""))
            .map((file, index) => "\n\nFile " + (index + 1) + ":" +
                "\n- File path: " + String(file.path) +
                "\n- Overwrite existing file: " + (file.overwrite ? "enabled" : "disabled") +
                "\n\nFile content:\n" + quoteForPrompt(file.content, "text"))
            .join("");
        return description + "\n\nAdd file details:" + details;
    };

    return {buildUserChangesPrompt, quoteForPrompt};
});
