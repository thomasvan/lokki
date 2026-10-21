//! Bakes the sidecar names and the two versions in, from the one home of the rule,
//! `scripts/sidecars.py` (run from `app/`).
use std::path::Path;
use std::process::Command;

fn sidecars(app: &Path, flag: &str) -> serde_json::Value {
    let mut spawn_error = None;
    for python in ["python3", "python"] {
        let out = match Command::new(python)
            .args(["scripts/sidecars.py", flag])
            .current_dir(app)
            .output()
        {
            Ok(out) => out,
            Err(e) => {
                spawn_error = Some(e);
                continue;
            }
        };
        if !out.status.success() {
            panic!(
                "scripts/sidecars.py {flag} failed: {}",
                String::from_utf8_lossy(&out.stderr).trim()
            );
        }
        return serde_json::from_slice(&out.stdout)
            .unwrap_or_else(|e| panic!("scripts/sidecars.py {flag} printed no JSON: {e}"));
    }
    panic!(
        "lokki needs python3 (or python) on PATH to read the sidecar names from scripts/sidecars.py: {}",
        spawn_error.map(|e| e.to_string()).unwrap_or_default()
    );
}

fn main() {
    let app = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    for home in [
        "../../../core/VERSION",
        "../../cliproxy.lock",
        "../../scripts/sidecars.py",
    ] {
        println!("cargo:rerun-if-changed={home}");
    }
    let names = sidecars(&app, "--names");
    let versions = sidecars(&app, "--versions");
    for (var, value) in [
        ("LOKKI_CORE_SIDECAR", &names["core"]),
        ("LOKKI_CLIPROXY_SIDECAR", &names["cliproxy"]),
        ("LOKKI_VERSION", &versions["core"]),
        ("LOKKI_CLIPROXY_VERSION", &versions["cliproxy"]),
    ] {
        let value = value
            .as_str()
            .unwrap_or_else(|| panic!("scripts/sidecars.py gave no {var}"));
        println!("cargo:rustc-env={var}={value}");
    }
}
