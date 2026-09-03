(function (root) {
    "use strict";

    const defaults = {requiresBase: false, createsBranch: false, requiresSecondBranch: false,
        requiresExistingBranch: false, hidesCommitDetails: false, acceptsPatch: true};
    const modes = {
        default: {},
        add_file: {addsFiles: true},
        from_commit: {requiresBase: true, createsBranch: true, hidesCommitDetails: true, acceptsPatch: false},
        orphan: {createsBranch: true},
        revert_to_commit: {requiresBase: true, requiresExistingBranch: true, resetsBranch: true,
            hidesCommitDetails: true, acceptsPatch: false},
        merge_branches: {requiresSecondBranch: true, requiresExistingBranch: true, acceptsPatch: false},
        mirror_repository: {mirrorsRepository: true, acceptsPatch: false},
        config: {configuresBackend: true, acceptsPatch: false},
    };
    const get = (mode) => Object.freeze({...defaults, ...(modes[mode] || modes.default)});
    root.FormMode = Object.freeze({modes: Object.freeze(modes), get});
})(globalThis);
