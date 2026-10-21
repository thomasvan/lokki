//! What the shell decides without Tauri: where the data lives, first run through the
//! core's own subcommands, which files and variables a sidecar gets, when to
//! restart or stop one, what the tray shows and writes, and what the macOS bundle is.

pub mod env;
pub mod firstrun;
pub mod logs;
pub mod mac;
pub mod paths;
pub mod sidecar_files;
pub mod supervise;
pub mod tray;

/// An error is the sentence the error page shows.
pub type Result<T> = std::result::Result<T, String>;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Os {
    Linux,
    MacOs,
    Windows,
}

impl Os {
    pub fn current() -> Os {
        if cfg!(windows) {
            Os::Windows
        } else if cfg!(target_os = "macos") {
            Os::MacOs
        } else {
            Os::Linux
        }
    }
}

// Names and versions `build.rs` bakes in from `scripts/sidecars.py`.
pub const CORE_SIDECAR: &str = env!("LOKKI_CORE_SIDECAR");
pub const CLIPROXY_SIDECAR: &str = env!("LOKKI_CLIPROXY_SIDECAR");
pub const VERSION: &str = env!("LOKKI_VERSION");
pub const CLIPROXY_VERSION: &str = env!("LOKKI_CLIPROXY_VERSION");
