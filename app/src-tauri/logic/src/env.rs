use crate::Os;
use std::ffi::OsString;

/// The variables a sidecar (or a core subcommand) gets: an allow-list of the parent's.
pub fn for_sidecars(
    parent: impl Iterator<Item = (OsString, OsString)>,
) -> Vec<(OsString, OsString)> {
    for_sidecars_on(Os::current(), parent)
}

/// `for_sidecars` for an injected OS: names match without regard to case on Windows only.
pub fn for_sidecars_on(
    os: Os,
    parent: impl Iterator<Item = (OsString, OsString)>,
) -> Vec<(OsString, OsString)> {
    parent
        .filter(|(name, _)| name.to_str().is_some_and(|n| allowed(os, n)))
        .collect()
}

const COMMON: &[&str] = &[
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TZ", "TMPDIR", "TMP", "TEMP",
];
// A user behind a proxy, or a TLS-inspecting one, reaches the provider through these.
const NETWORK: &[&str] = &[
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
];
const WINDOWS: &[&str] = &[
    "SystemRoot",
    "windir",
    "ComSpec",
    "PATHEXT",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "ProgramData",
    "SystemDrive",
];

fn allowed(os: Os, name: &str) -> bool {
    if os == Os::Windows {
        // The system sets `Path` there, and names match without regard to case.
        let upper = name.to_ascii_uppercase();
        return upper.starts_with("LC_")
            || COMMON
                .iter()
                .chain(NETWORK)
                .chain(WINDOWS)
                .any(|n| n.eq_ignore_ascii_case(name));
    }
    name.starts_with("LC_")
        || COMMON.contains(&name)
        || NETWORK
            .iter()
            .any(|n| *n == name || n.to_ascii_lowercase() == name)
}

#[cfg(test)]
mod tests {
    use super::*;

    // This table deliberately copies the allow-list: it is what keeps RELAY_*,
    // CLI_PROXY_API_KEY and MANAGEMENT_PASSWORD out of a sidecar, so widening it takes
    // an edit here as well as in `COMMON`, `NETWORK` and `WINDOWS`.
    const COMMON_KEPT: &[&str] = &[
        "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR",
        "TMP", "TEMP",
    ];
    const NETWORK_KEPT: &[&str] = &[
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "ssl_cert_file",
        "ssl_cert_dir",
    ];
    const WINDOWS_KEPT: &[&str] = &[
        "SystemRoot",
        "windir",
        "ComSpec",
        "PATHEXT",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "ProgramData",
        "SystemDrive",
    ];
    // What CLIProxyAPI reads (MANAGEMENT_PASSWORD, PGSTORE_*, GITSTORE_*, OBJECTSTORE_*,
    // WRITABLE_PATH), the core's own (RELAY_*, CLI_PROXY_API_KEY), and the rest of a shell.
    const DROPPED: &[&str] = &[
        "RELAY_LISTEN",
        "RELAY_CONFIG",
        "CLI_PROXY_API_KEY",
        "CLIPROXY_URL",
        "MANAGEMENT_PASSWORD",
        "PGSTORE_DSN",
        "GITSTORE_GIT_URL",
        "OBJECTSTORE_ENDPOINT",
        "WRITABLE_PATH",
        "EDITOR",
        "LD_PRELOAD",
        "Management_Password",
    ];

    fn passes(os: Os, name: &str) -> bool {
        let parent = std::iter::once((OsString::from(name), OsString::from("v")));
        for_sidecars_on(os, parent) == vec![(OsString::from(name), OsString::from("v"))]
    }

    #[test]
    fn only_the_allow_list_passes() {
        // (os, name, passes)
        let mut rows: Vec<(Os, &str, bool)> = Vec::new();
        for os in [Os::Linux, Os::MacOs] {
            rows.extend(
                COMMON_KEPT
                    .iter()
                    .chain(NETWORK_KEPT)
                    .map(|n| (os, *n, true)),
            );
            // Case matters off Windows, and the Windows names are not passed on.
            rows.extend(
                ["Path", "path", "lc_all", "Https_Proxy"]
                    .into_iter()
                    .chain(WINDOWS_KEPT.iter().copied())
                    .map(|n| (os, n, false)),
            );
            rows.extend(DROPPED.iter().map(|n| (os, *n, false)));
        }
        // On Windows names match without regard to case, whatever the system spells them.
        rows.extend(
            COMMON_KEPT
                .iter()
                .chain(NETWORK_KEPT)
                .chain(WINDOWS_KEPT)
                .map(|n| (Os::Windows, *n, true)),
        );
        rows.extend(
            ["Path", "path", "SYSTEMROOT", "lc_all", "Https_Proxy"]
                .into_iter()
                .map(|n| (Os::Windows, n, true)),
        );
        rows.extend(DROPPED.iter().map(|n| (Os::Windows, *n, false)));
        for (os, name, want) in rows {
            assert_eq!(passes(os, name), want, "{os:?} {name}");
        }
    }
}
