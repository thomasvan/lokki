use std::path::{Path, PathBuf};

/// Removes the files of `dir` that start with `lokki-core-` or `cliproxy-` and are not in
/// `keep`; the removed paths come back. Directories are skipped, and so is a removal that fails.
pub fn prune(dir: &Path, keep: &[&str]) -> Vec<PathBuf> {
    let Ok(entries) = std::fs::read_dir(dir) else {
        return Vec::new();
    };
    entries
        .flatten()
        .filter(|e| {
            let name = e.file_name();
            let name = name.to_string_lossy();
            (name.starts_with("lokki-core-") || name.starts_with("cliproxy-"))
                && !keep.contains(&&*name)
                && e.file_type().is_ok_and(|t| !t.is_dir())
        })
        .map(|e| e.path())
        .filter(|path| std::fs::remove_file(path).is_ok())
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    // Tauri's bundler strips the target triple from an `externalBin` file name; `.exe` stays on Windows.
    const CORE: &str = "lokki-core-0_1_0";
    const CLIPROXY: &str = "cliproxy-8_0_23";

    fn dir_with(names: &[&str]) -> tempfile::TempDir {
        let dir = tempfile::tempdir().unwrap();
        for n in names {
            fs::write(dir.path().join(n), b"x").unwrap();
        }
        dir
    }

    fn left(dir: &Path) -> Vec<String> {
        let mut names: Vec<String> = fs::read_dir(dir)
            .unwrap()
            .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
            .collect();
        names.sort();
        names
    }

    #[test]
    fn old_sidecar_files_are_removed() {
        let old = ["lokki-core-0_0_9", "cliproxy-8_0_22", "cliproxy-8_0_22.exe"];
        let dir = dir_with(&[
            CORE,
            CLIPROXY,
            "lokki",
            "libwebkit.so",
            old[0],
            old[1],
            old[2],
        ]);
        fs::create_dir(dir.path().join("cliproxy-a-directory")).unwrap(); // directories are skipped
        let mut removed = prune(dir.path(), &[CORE, CLIPROXY]);
        removed.sort();
        let mut want: Vec<PathBuf> = old.iter().map(|n| dir.path().join(n)).collect();
        want.sort();
        assert_eq!(removed, want);
        assert_eq!(
            left(dir.path()),
            [
                "cliproxy-8_0_23",
                "cliproxy-a-directory",
                "libwebkit.so",
                "lokki",
                "lokki-core-0_1_0"
            ]
        );
    }

    #[cfg(unix)]
    #[test]
    fn a_removal_that_fails_is_skipped() {
        use std::os::unix::fs::PermissionsExt;
        let old = "cliproxy-8_0_22";
        let dir = dir_with(&[CORE, old]);
        fs::set_permissions(dir.path(), fs::Permissions::from_mode(0o555)).unwrap();
        if fs::write(dir.path().join("probe"), b"").is_ok() {
            return; // root writes anywhere: the removal cannot fail here
        }
        let removed = prune(dir.path(), &[CORE]);
        fs::set_permissions(dir.path(), fs::Permissions::from_mode(0o755)).unwrap();
        assert!(removed.is_empty(), "{removed:?}");
        assert!(dir.path().join(old).exists());
    }
}
