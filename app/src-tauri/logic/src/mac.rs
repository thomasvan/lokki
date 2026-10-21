//! What the shell knows of the macOS bundle it runs from (spec 7.4), as plain data, so the
//! update policy that reads it is decided and tested on any OS.
use std::path::{Path, PathBuf};

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct MacBundle {
    /// The `.app` the executable is in.
    pub app: Option<PathBuf>,
    /// A component of `app`'s path is `AppTranslocation`.
    pub translocated: bool,
    /// `app`'s file system is mounted read-only.
    pub read_only: bool,
    /// The user can write both `app` and the folder that holds it.
    pub writable: bool,
}

/// The `.app` when `exe` is `<name>.app/Contents/MacOS/<exe>`.
pub fn app_of(exe: &Path) -> Option<PathBuf> {
    let macos = exe.parent()?;
    let contents = macos.parent()?;
    let app = contents.parent()?;
    let bundle = macos.file_name()? == "MacOS"
        && contents.file_name()? == "Contents"
        && app.extension()? == "app";
    bundle.then(|| app.to_path_buf())
}

/// Apple calls the path an implementation detail: this names the case for the user's reason,
/// while `read_only` and `writable` are the guard.
pub fn translocated(app: &Path) -> bool {
    app.components()
        .any(|c| c.as_os_str() == "AppTranslocation")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_path_inside_a_bundle_names_the_app_and_one_outside_names_none() {
        let rows = [
            (
                "/Applications/lokki.app/Contents/MacOS/lokki",
                Some("/Applications/lokki.app"),
            ),
            ("/usr/local/bin/lokki", None),
            ("/x/lokki.app/Contents/Resources/lokki", None),
        ];
        for (exe, app) in rows {
            assert_eq!(app_of(Path::new(exe)), app.map(PathBuf::from), "{exe}");
        }
    }

    #[test]
    fn an_app_translocation_component_marks_the_copy() {
        let copy = "/private/var/folders/x/T/AppTranslocation/0A1B/d/lokki.app";
        assert!(translocated(Path::new(copy)));
        assert!(!translocated(Path::new("/Applications/lokki.app")));
        assert!(!translocated(Path::new(
            "/Users/me/NotAppTranslocation/lokki.app"
        )));
    }
}
