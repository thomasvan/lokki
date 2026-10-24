//! The nine commands `build.rs` declares; the capabilities decide which page may call which.
use crate::sidecars::Service;
use crate::{update, Shell};
use std::sync::Arc;
use std::time::Duration;
use tauri::{AppHandle, State, Url};
use tauri_plugin_clipboard_manager::ClipboardExt;
use tauri_plugin_opener::OpenerExt;

type Answer = Result<(), String>;

/// How long the shell stays up after the smoke's report, so the smoke can sample the
/// listeners the page's restart opened.
const SMOKE_HOLD: Duration = Duration::from_secs(2);

/// Runs blocking work on the runtime's blocking pool: the loopback HTTP client blocks.
async fn off_runtime(work: impl FnOnce() -> Answer + Send + 'static) -> Answer {
    tauri::async_runtime::spawn_blocking(work)
        .await
        .unwrap_or_else(|_| Err("Lokki's shell failed while doing that; see logs/app.log".into()))
}

fn open_in_browser(app: &AppHandle, url: &str) -> Answer {
    app.opener()
        .open_url(url, None::<&str>)
        .map_err(|e| format!("could not open the browser: {e}"))
}

/// The URL `open_external` opens: `https:` only.
fn external_url(url: &str) -> Result<Url, String> {
    let parsed: Url = url.parse().map_err(|_| "that is not a URL".to_string())?;
    if parsed.scheme() != "https" {
        return Err("Lokki opens https: links only".into());
    }
    Ok(parsed)
}

/// Resolves once the named sidecar has restarted and is ready (`"cliproxy"` restarts the
/// core after it); rejects with the sentence that says why not.
#[tauri::command]
pub async fn restart_service(shell: State<'_, Arc<Shell>>, service: String) -> Answer {
    let service =
        Service::parse(&service).ok_or_else(|| format!("there is no service {service:?}"))?;
    let shell = Arc::clone(&shell);
    off_runtime(move || shell.restart(service)).await
}

/// The system browser, `https:` only.
#[tauri::command]
pub async fn open_external(app: AppHandle, url: String) -> Answer {
    open_in_browser(&app, external_url(&url)?.as_str())
}

/// CLIProxyAPI's panel in the system browser, when `cliproxy.panel` is on.
#[tauri::command]
pub async fn open_panel(app: AppHandle, shell: State<'_, Arc<Shell>>) -> Answer {
    let conf = shell.conf().ok_or("Lokki has not started its services")?;
    if !conf.panel {
        return Err("the panel is off".into());
    }
    open_in_browser(
        &app,
        &format!("http://127.0.0.1:{}/management.html", conf.cliproxy_port),
    )
}

/// The management secret, from memory to the clipboard, on the user's click.
#[tauri::command]
pub async fn copy_management_secret(app: AppHandle, shell: State<'_, Arc<Shell>>) -> Answer {
    let secret = shell
        .secret()
        .ok_or("Lokki could not read the management secret file")?;
    app.clipboard()
        .write_text(secret)
        .map_err(|e| format!("could not reach the clipboard: {e}"))
}

/// The error page's button: one more try of what failed.
#[tauri::command]
pub async fn try_again(shell: State<'_, Arc<Shell>>, name: String) -> Answer {
    let shell = Arc::clone(&shell);
    off_runtime(move || shell.try_again(&name)).await
}

/// The update state the shell holds.
#[tauri::command]
pub async fn update_status(shell: State<'_, Arc<Shell>>) -> Result<update::Status, String> {
    Ok(shell.update_status())
}

/// A check now (or the one already running), then the state.
#[tauri::command]
pub async fn check_for_update(shell: State<'_, Arc<Shell>>) -> Result<update::Status, String> {
    let shell = Arc::clone(&shell);
    tauri::async_runtime::spawn_blocking(move || shell.check_for_update())
        .await
        .map_err(|e| e.to_string())
}

/// Download and verify, stop both sidecars and prove them gone, install, restart; rejects
/// with the sentence that says why not.
#[tauri::command]
pub async fn install_update(shell: State<'_, Arc<Shell>>) -> Answer {
    let shell = Arc::clone(&shell);
    off_runtime(move || shell.install_update()).await
}

/// The headless smoke's report from the page: the restart, the authenticated read, what
/// the page's grant did with four commands it does not name, the update state and the
/// install's refusal. Nothing without `LOKKI_SMOKE`.
#[tauri::command]
pub async fn smoke_done(
    shell: State<'_, Arc<Shell>>,
    status: i64,
    restart: String,
    grant: String,
    update: String,
    install: String,
) -> Answer {
    if !shell.smoke {
        return Ok(());
    }
    println!("smoke: page status={status} restart={restart}");
    println!("smoke: grant {grant}");
    println!("smoke: update state={update}");
    println!("smoke: install refused={install}");
    let shell = Arc::clone(&shell);
    std::thread::spawn(move || {
        std::thread::sleep(SMOKE_HOLD);
        shell.quit()
    });
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn open_external_opens_https_only() {
        // (url, opened)
        let rows = [
            ("https://auth.openai.com/oauth/authorize?state=x", true),
            ("https://example.com", true),
            ("http://example.com/", false),
            ("javascript:alert(1)", false),
            ("file:///etc/passwd", false),
            ("not a url", false),
            ("", false),
        ];
        for (url, want) in rows {
            assert_eq!(external_url(url).is_ok(), want, "{url}");
        }
    }
}
