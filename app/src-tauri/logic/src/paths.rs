use crate::Os;
use std::ffi::OsString;
use std::path::PathBuf;

/// The data directory of this process: `LOKKI_DATA_DIR`, else the OS's home for it.
pub fn data_dir() -> PathBuf {
    data_dir_for(Os::current(), |name| std::env::var_os(name))
}

/// `data_dir` over an injected OS and environment.
pub fn data_dir_for(os: Os, var: impl Fn(&str) -> Option<OsString>) -> PathBuf {
    if let Some(dir) = var("LOKKI_DATA_DIR") {
        return PathBuf::from(dir);
    }
    let required = |name: &str| {
        PathBuf::from(var(name).unwrap_or_else(|| {
            panic!("{name} is not set, so there is no home for the data directory")
        }))
    };
    match os {
        Os::Linux => match var("XDG_DATA_HOME") {
            Some(xdg) => PathBuf::from(xdg).join("lokki"),
            None => required("HOME").join(".local/share/lokki"),
        },
        Os::MacOs => required("HOME").join("Library/Application Support/lokki"),
        Os::Windows => required("APPDATA").join("lokki"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;
    use std::path::Path;

    #[test]
    fn the_override_wins_and_each_os_has_its_home() {
        // (os, environment, expected)
        type Row = (Os, &'static [(&'static str, &'static str)], &'static str);
        let rows: &[Row] = &[
            (
                Os::Linux,
                &[("HOME", "/home/t")],
                "/home/t/.local/share/lokki",
            ),
            (
                Os::Linux,
                &[("HOME", "/home/t"), ("XDG_DATA_HOME", "/x/data")],
                "/x/data/lokki",
            ),
            (
                Os::MacOs,
                &[("HOME", "/Users/t")],
                "/Users/t/Library/Application Support/lokki",
            ),
            (
                Os::Windows,
                &[("APPDATA", "C:/Users/t/AppData/Roaming")],
                "C:/Users/t/AppData/Roaming/lokki",
            ),
            (
                Os::Linux,
                &[
                    ("HOME", "/home/t"),
                    ("XDG_DATA_HOME", "/x/data"),
                    ("LOKKI_DATA_DIR", "/o/lokki-dir"),
                ],
                "/o/lokki-dir",
            ),
            (
                Os::MacOs,
                &[("HOME", "/Users/t"), ("LOKKI_DATA_DIR", "/o/lokki-dir")],
                "/o/lokki-dir",
            ),
            (
                Os::Windows,
                &[
                    ("APPDATA", "C:/Users/t/AppData/Roaming"),
                    ("LOKKI_DATA_DIR", "/o/lokki-dir"),
                ],
                "/o/lokki-dir",
            ),
        ];
        for (os, env, want) in rows {
            let env: HashMap<&str, OsString> =
                env.iter().map(|(k, v)| (*k, OsString::from(v))).collect();
            let got = data_dir_for(*os, |name| env.get(name).cloned());
            assert_eq!(
                got,
                Path::new(want),
                "{os:?} with {:?}",
                env.keys().collect::<Vec<_>>()
            );
        }
    }
}
