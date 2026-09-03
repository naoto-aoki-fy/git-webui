(function (root) {
    "use strict";

    const request = async (endpoint, method, path, payload = null) => {
        const options = {method};
        if (payload !== null) {
            options.headers = {"Content-Type": "application/json"};
            options.body = JSON.stringify(payload);
        }
        const response = await fetch(endpoint(path), options);
        if (!response.ok) throw new Error("Request failed: " + response.status);
        return response.json();
    };

    root.BackendClient = Object.freeze({request});
})(globalThis);
