//! The bundled error page, opened when the first run or a start fails and when a sidecar
//! gives up: what failed, the last lines of the log that says why, and "Try again".
use serde::Serialize;
use tauri::{AppHandle, Manager, WebviewUrl, WebviewWindowBuilder};

pub const ERROR: &str = "error";

/// How many log lines the page shows.
pub const TAIL_LINES: usize = 50;

/// What the page shows; `name` is what `try_again` receives back.
#[derive(Serialize)]
pub struct Failure {
    pub title: String,
    pub what: String,
    pub name: String,
    pub tail: String,
}

/// Opens the error window on top. One already open shows the new failure in place: closing
/// the last window would end the app.
pub fn show(app: &AppHandle, failure: &Failure) -> tauri::Result<()> {
    let data = serde_json::to_string(failure).expect("a failure serializes");
    if let Some(open) = app.get_webview_window(ERROR) {
        open.eval(format!("render({data});"))?;
        let _ = open.show();
        let _ = open.set_focus();
        return Ok(());
    }
    WebviewWindowBuilder::new(app, ERROR, WebviewUrl::App("error.html".into()))
        .title("Lokki")
        .inner_size(760.0, 560.0)
        .initialization_script(format!("window.__LOKKI_ERROR__ = {data};"))
        .build()?;
    Ok(())
}

pub fn close(app: &AppHandle) {
    if let Some(w) = app.get_webview_window(ERROR) {
        let _ = w.destroy();
    }
}
