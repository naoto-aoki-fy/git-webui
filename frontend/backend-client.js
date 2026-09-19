(function (root) {
    "use strict";

    const request = async (endpoint, method, path, payload = null) => {
        const options = {method};
        if (payload !== null) {
            options.headers = {"Content-Type": "application/json"};
            options.body = JSON.stringify(payload);
        }
        const response = await fetch(endpoint(path), options);
        if (!response.ok) {
            if (response.status === 413) {
                throw new Error(
                    "The patch is too large for the backend. " +
                    "Reduce its size and try again (maximum request size: 16 MiB)."
                );
            }
            const detail = (await response.text()).trim();
            throw new Error(
                "Request failed: " + response.status + (detail ? " (" + detail + ")" : "")
            );
        }
        return response.json();
    };

    root.BackendClient = Object.freeze({request});
})(globalThis);
