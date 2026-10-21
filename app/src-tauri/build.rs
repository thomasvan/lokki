// Declared in the app manifest, so the capabilities gate them. The sidecar names and
// versions come from `lokki_logic`, which bakes them in.
const COMMANDS: &[&str] = &[
    "restart_service",
    "open_external",
    "open_panel",
    "copy_management_secret",
    "try_again",
    "smoke_done",
    "update_status",
    "check_for_update",
    "install_update",
];

fn main() {
    let attributes = tauri_build::Attributes::new()
        .app_manifest(tauri_build::AppManifest::new().commands(COMMANDS));
    if let Err(e) = tauri_build::try_build(attributes) {
        eprintln!("{e:#}");
        std::process::exit(1);
    }
}
