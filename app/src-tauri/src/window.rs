//! The main window: the core's own page, with the injected script, and no way off its origin.
use tauri::webview::PageLoadEvent;
use tauri::{AppHandle, Manager, Url, WebviewUrl, WebviewWindow, WebviewWindowBuilder};

/// A main window's label is `main-<core port>`: a core that moves to another port gets a
/// new window, built before the old one closes, and a grant naming that window alone.
const MAIN_PREFIX: &str = "main-";

pub fn main_label(core_port: u16) -> String {
    format!("{MAIN_PREFIX}{core_port}")
}

pub fn is_main(label: &str) -> bool {
    label.starts_with(MAIN_PREFIX)
}

const INJECT: &str = include_str!("../../inject/inject.js");

/// `inject.js` with its two placeholders filled as its header says: JSON string literals,
/// the origin's first and then the token's, so the token's text is never scanned.
pub fn inject_script(origin: &str, token: &str) -> String {
    let json = |s: &str| serde_json::to_string(s).expect("a string serializes");
    INJECT
        .replacen("__ORIGIN__", &json(origin), 1)
        .replacen("__TOKEN__", &json(token), 1)
}

pub fn origin(core_port: u16) -> String {
    format!("http://127.0.0.1:{core_port}")
}

/// The page the main window opens: the core's `/`, and when the core has moved from another
/// port, `?moved_from=<port>`, so the page can tell the person that clients must follow.
fn main_url(core_port: u16, moved_from: Option<u16>) -> Url {
    let _ = moved_from;
    let query = "";
    format!("{}/{query}", origin(core_port))
        .parse()
        .expect("a loopback URL parses")
}

/// Whether the main window may go to `to`: only within the core's origin.
fn may_navigate(core_origin: &Url, to: &Url) -> bool {
    to.origin() == core_origin.origin()
}

/// Opens the main window on `main_url`. A main window on another origin closes once the
/// new one is built, and stays when the build fails. `on_loaded` runs once the page has
/// loaded.
pub fn open_main(
    app: &AppHandle,
    core_port: u16,
    moved_from: Option<u16>,
    token: &str,
    on_loaded: impl Fn(&WebviewWindow) + Send + Sync + 'static,
) -> tauri::Result<()> {
    let label = main_label(core_port);
    let origin = origin(core_port);
    let url = main_url(core_port, moved_from);
    let core_origin = url.clone();
    WebviewWindowBuilder::new(app, &label, WebviewUrl::External(url))
        .title("lokki")
        .inner_size(1100.0, 760.0)
        // The narrowest and shortest the UI is laid out for: the three mode segments and the
        // longest value on Home fit 480 px, and 600 px leaves the folded chrome about 300 px
        // of content.
        .min_inner_size(480.0, 600.0)
        .initialization_script(inject_script(&origin, token))
        .on_navigation(move |to| may_navigate(&core_origin, to))
        .on_page_load(move |window, load| {
            if load.event() == PageLoadEvent::Finished {
                on_loaded(&window);
            }
        })
        .build()?;
    for (old, window) in app.webview_windows() {
        if is_main(&old) && old != label {
            let _ = window.destroy();
        }
    }
    Ok(())
}

pub fn main_window(app: &AppHandle) -> Option<WebviewWindow> {
    app.webview_windows()
        .into_iter()
        .find_map(|(label, w)| is_main(&label).then_some(w))
}

pub fn show_main(app: &AppHandle) {
    if let Some(w) = main_window(app) {
        let _ = w.unminimize();
        let _ = w.show();
        let _ = w.set_focus();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_script_carries_the_origin_and_token_as_json_literals_once() {
        // A token that holds a quote, a backslash and the other placeholder's name: it must
        // land as one literal, unscanned, and the page code must stay intact.
        let token = r#"t"o\k__ORIGIN__en"#;
        let origin = "http://127.0.0.1:43210";
        let script = inject_script(origin, token);
        let token_json = serde_json::to_string(token).unwrap();
        assert!(
            !script.contains("__TOKEN__"),
            "the token placeholder is left"
        );
        assert_eq!(script.matches(&token_json).count(), 1);
        assert!(script.contains(&format!("const ORIGIN = \"{origin}\";")));
        assert!(script.contains(&format!("const TOKEN = {token_json};")));
        assert_eq!(
            script.len(),
            INJECT.len() - "__ORIGIN____TOKEN__".len() + origin.len() + 2 + token_json.len(),
            "something besides the two placeholders changed"
        );
    }

    #[test]
    fn a_window_that_moved_names_the_port_it_moved_from() {
        // (core port, moved from, the page)
        let rows = [
            (43210, None, "http://127.0.0.1:43210/"),
            (
                43210,
                Some(43211),
                "http://127.0.0.1:43210/?moved_from=43211",
            ),
        ];
        for (port, from, want) in rows {
            assert_eq!(main_url(port, from).as_str(), want);
        }
    }

    #[test]
    fn the_window_navigates_only_within_the_core_origin() {
        let core: Url = format!("{}/", origin(8381)).parse().unwrap();
        // (where to, allowed)
        let rows = [
            ("http://127.0.0.1:8381/", true),
            ("http://127.0.0.1:8381/static/app.js?x=1#top", true),
            // Userinfo on the core's own host and port is still that origin.
            ("http://user@127.0.0.1:8381/", true),
            ("http://127.0.0.1:8317/", false),
            ("http://localhost:8381/", false),
            ("https://127.0.0.1:8381/", false),
            ("http://127.0.0.1.evil.test:8381/", false),
            ("http://127.0.0.1:8381@evil.test/", false),
            ("https://example.com/", false),
            ("about:blank", false),
        ];
        for (to, want) in rows {
            let to: Url = to.parse().unwrap();
            assert_eq!(may_navigate(&core, &to), want, "{to}");
        }
    }
}
