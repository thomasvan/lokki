// The webview initialization script (spec 6.4). `window.rs` fills the two placeholder
// identifiers on the ORIGIN and TOKEN lines below, each appearing once in this file
// (comments name neither), with JSON string literals (`serde_json::to_string` of the
// &str, quotes included): the core's origin (`http://127.0.0.1:<port>`) and the operator
// token. It replaces the origin's first and the token's second, so the token's text is
// never scanned for a placeholder.
(() => {
  const ORIGIN = __ORIGIN__;
  const TOKEN = __TOKEN__;
  if (location.origin !== ORIGIN) return;

  const original = window.fetch;
  window.fetch = function (input, init) {
    try {
      const url = new URL(input instanceof Request ? input.url : String(input), location.href);
      if (url.origin === ORIGIN) {
        // Options replace a Request's own headers, so they start from the Request's.
        const headers = new Headers(init?.headers ?? (input instanceof Request ? input.headers : undefined));
        headers.set("X-Relay-Token", TOKEN);
        return original.call(this, input, { ...init, headers });
      }
    } catch {
      // An unparsable URL or headers value: `fetch` rejects it itself, without the token.
    }
    return original.call(this, input, init);
  };

  const call = (command, args) => window.__TAURI__.core.invoke(command, args);
  window.lokki = {
    restart: (service) => call("restart_service", { service }),
    openExternal: (url) => call("open_external", { url }),
    openPanel: () => call("open_panel"),
    copyManagementSecret: () => call("copy_management_secret"),
    updateStatus: () => call("update_status"),
    checkForUpdate: () => call("check_for_update"),
    installUpdate: () => call("install_update"),
  };
})();
