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
    let mut attributes = tauri_build::Attributes::new()
        .app_manifest(tauri_build::AppManifest::new().commands(COMMANDS));
    // Tauri embeds its Windows manifest (Common Controls 6) as a resource of the program
    // alone, and a test binary without it does not load (STATUS_ENTRYPOINT_NOT_FOUND).
    // The same manifest, `windows-app-manifest.xml`, a copy of tauri-build's, goes in
    // through the linker instead, which reaches every binary of the package.
    if std::env::var("CARGO_CFG_TARGET_ENV").as_deref() == Ok("msvc") {
        attributes = attributes
            .windows_attributes(tauri_build::WindowsAttributes::new_without_app_manifest());
        let manifest =
            std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("windows-app-manifest.xml");
        println!("cargo:rerun-if-changed=windows-app-manifest.xml");
        println!("cargo:rustc-link-arg=/MANIFEST:EMBED");
        println!("cargo:rustc-link-arg=/MANIFESTINPUT:{}", manifest.display());
    }
    if let Err(e) = tauri_build::try_build(attributes) {
        eprintln!("{e:#}");
        std::process::exit(1);
    }
}
