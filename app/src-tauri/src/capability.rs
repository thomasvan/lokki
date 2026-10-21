//! The two IPC grants, built at run time: the core's origin is known only once
//! `shell-config` has named its port, so there is no static capability file.
use tauri::ipc::CapabilityBuilder;
use tauri::{AppHandle, Manager};

/// The main window for `core_port`, on the core's exact origin and nowhere else, may call
/// the seven hooks the page uses (the updater among them through the shell's commands,
/// never the plugin's own); `smoke_done` only under `LOKKI_SMOKE`. Tauri cannot remove
/// a grant, so one for a port the core left stays registered and names a window that no
/// longer exists.
pub fn grant_main(app: &AppHandle, core_port: u16, smoke: bool) -> tauri::Result<()> {
    let mut grant = CapabilityBuilder::new(format!("core-origin-{core_port}"))
        .window(crate::window::main_label(core_port))
        .remote(crate::window::origin(core_port))
        .local(false)
        .permission("allow-restart-service")
        .permission("allow-open-external")
        .permission("allow-open-panel")
        .permission("allow-copy-management-secret")
        .permission("allow-update-status")
        .permission("allow-check-for-update")
        .permission("allow-install-update");
    if smoke {
        grant = grant.permission("allow-smoke-done");
    }
    app.add_capability(grant)
}

/// The bundled error page, on the app's own origin (no `remote`), may call `try_again`.
pub fn grant_error(app: &AppHandle) -> tauri::Result<()> {
    app.add_capability(
        CapabilityBuilder::new("error-page")
            .window(crate::errorpage::ERROR)
            .permission("allow-try-again"),
    )
}
