//! Sidecars an earlier run of lokki left running, found holding their ports when the shell
//! starts. The shell stops one only when it is certain the process is lokki's sidecar for
//! this data directory; anything else it leaves alone, and the start's error names it.
//!
//! The certainty rests on what only this data directory has, never on a pid an earlier run
//! wrote down. The shell holds the data directory's lock while it runs, and the system lets
//! the lock go when the shell ends, however it ends: a shell that holds it knows no other
//! lokki runs on this data directory, so no live shell owns what answers on its ports. The
//! core is stopped through its own `POST /api/shutdown`, which only a core given this data
//! directory's operator token accepts. CLIProxyAPI has no such call: it is stopped by its
//! pid only when it accepts this data directory's management secret and runs one of this
//! lokki's CLIProxyAPI files, and that pid is held while it is checked and stopped
//! (`process::listener`).
use crate::process::{self, Listener};
use crate::sidecars::{exe_dir, port_in_use, Context, CoreHttp, Service, GONE_WAIT};
use lokki_logic::firstrun::Config;
use lokki_logic::sidecar_files::CLIPROXY_PREFIX;
use lokki_logic::supervise::{drain_wait, Clock, ShutdownHttp, CLIPROXY_WAIT};
use lokki_logic::Result;
use std::fs::File;
use std::path::Path;
use std::time::Duration;

/// The lock file in the data directory.
pub const LOCK_FILE: &str = "lokki.lock";
/// How long a start waits for the lock: an updated lokki starts while the one it replaces
/// is still exiting.
const LOCK_WAIT: Duration = Duration::from_secs(5);

/// The data directory's lock, for as long as the file is open.
pub fn lock(dir: &Path, clock: &impl Clock) -> Result<File> {
    let mut open = std::fs::OpenOptions::new();
    open.create(true).truncate(false).write(true);
    #[cfg(unix)]
    std::os::unix::fs::OpenOptionsExt::mode(&mut open, 0o600);
    let path = dir.join(LOCK_FILE);
    let file = open
        .open(&path)
        .map_err(|e| format!("lokki could not open {}: {e}", path.display()))?;
    let end = clock.now() + LOCK_WAIT;
    loop {
        match file.try_lock() {
            Ok(()) => return Ok(file),
            Err(std::fs::TryLockError::WouldBlock) if clock.now() < end => {
                clock.sleep(Duration::from_millis(100))
            }
            Err(std::fs::TryLockError::WouldBlock) => {
                return Err(format!(
                    "Another lokki is running on the data directory {}. Open it from its \
                     tray icon, or quit it and try again.",
                    dir.display()
                ))
            }
            Err(std::fs::TryLockError::Error(e)) => {
                return Err(format!("lokki could not lock {}: {e}", path.display()))
            }
        }
    }
}

/// Stops what an earlier run left on the core's and CLIProxyAPI's ports, the core first so
/// that its drain still reaches CLIProxyAPI. The caller holds the data directory's lock and
/// runs neither sidecar. Returns a line for `app.log` for each port that was held.
pub fn stop(cx: &Context, conf: &Config) -> Vec<String> {
    [Service::Core, Service::Cliproxy]
        .into_iter()
        .filter(|s| port_in_use(s.port(conf)))
        .map(|s| match s {
            Service::Core => stop_core(cx, conf),
            Service::Cliproxy => stop_cliproxy(cx, conf),
        })
        .collect()
}

fn stop_core(cx: &Context, conf: &Config) -> String {
    let port = conf.core_port;
    let http = CoreHttp {
        client: &cx.http,
        port,
    };
    match http.post_shutdown(&conf.operator_token) {
        Ok((202, drain)) if freed(port, drain_wait(drain), &cx.clock) => format!(
            "port {port}: a core an earlier run left took this data directory's token, \
             drained and stopped"
        ),
        Ok((202, _)) => format!(
            "port {port}: a core an earlier run left took this data directory's token and \
             did not stop"
        ),
        _ => format!(
            "port {port}: {} does not take this data directory's token; left alone",
            holder(port)
        ),
    }
}

fn stop_cliproxy(cx: &Context, conf: &Config) -> String {
    let port = conf.cliproxy_port;
    let answers = std::fs::read_to_string(&conf.management_secret_file)
        .is_ok_and(|secret| takes_secret(&cx.http, port, secret.trim()));
    let found = process::listener(port);
    let dir = exe_dir().ok();
    match found {
        Some(l) if ours(answers, l.exe.as_deref(), dir.as_deref()) => {
            let pid = l.pid;
            if l.stop(port, CLIPROXY_WAIT) && freed(port, GONE_WAIT, &cx.clock) {
                format!("port {port}: a CLIProxyAPI an earlier run left, pid {pid}, stopped")
            } else {
                format!("port {port}: a CLIProxyAPI an earlier run left, pid {pid}, did not stop")
            }
        }
        found => format!(
            "port {port}: {} is not this data directory's CLIProxyAPI; left alone",
            name(found.as_ref())
        ),
    }
}

/// Whether CLIProxyAPI on `port` accepts `secret` on its management API.
fn takes_secret(http: &reqwest::blocking::Client, port: u16, secret: &str) -> bool {
    http.get(format!("http://127.0.0.1:{port}/v0/management/debug"))
        .timeout(Duration::from_secs(5))
        .bearer_auth(secret)
        .send()
        .is_ok_and(|r| r.status().as_u16() == 200)
}

/// A listener is this data directory's CLIProxyAPI, started by this lokki: it accepts the
/// data directory's management secret, and runs a CLIProxyAPI file in `sidecar_dir`, the
/// directory this shell's own sidecars are in.
fn ours(answers: bool, exe: Option<&Path>, sidecar_dir: Option<&Path>) -> bool {
    let (Some(exe), Some(dir)) = (exe, sidecar_dir) else {
        return false;
    };
    let named = exe
        .file_name()
        .is_some_and(|n| n.to_string_lossy().starts_with(CLIPROXY_PREFIX));
    let beside = match (std::fs::canonicalize(exe), std::fs::canonicalize(dir)) {
        (Ok(exe), Ok(dir)) => exe.parent() == Some(dir.as_path()),
        _ => false,
    };
    answers && named && beside
}

/// What holds `port`, for a sentence: its executable and pid where the system says.
pub fn holder(port: u16) -> String {
    name(process::listener(port).as_ref())
}

fn name(found: Option<&Listener>) -> String {
    match found {
        Some(Listener {
            pid,
            exe: Some(exe),
            ..
        }) => format!("{} (pid {pid})", exe.display()),
        Some(l) => format!("a program with pid {}", l.pid),
        None => "another program".to_string(),
    }
}

/// Whether `port` is free within `limit`.
fn freed(port: u16, limit: Duration, clock: &impl Clock) -> bool {
    let end = clock.now() + limit;
    loop {
        if !port_in_use(port) {
            return true;
        }
        if clock.now() >= end {
            return false;
        }
        clock.sleep(Duration::from_millis(100));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sidecars::{AppLog, RealClock};
    use crate::testing::FakeClock;
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::path::PathBuf;
    use std::time::Instant;

    const TOKEN: &str = "tok-this-data-directory";

    /// A loopback HTTP server that answers each request with `answer(request head)`, and
    /// closes its port once it has answered 202.
    fn server(answer: fn(&str) -> u16) -> u16 {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        std::thread::spawn(move || {
            for stream in listener.incoming() {
                let mut stream = stream.unwrap();
                let mut head = Vec::new();
                let mut byte = [0u8];
                while !head.ends_with(b"\r\n\r\n") && stream.read(&mut byte).unwrap_or(0) == 1 {
                    head.push(byte[0]);
                }
                let status = answer(&String::from_utf8_lossy(&head));
                let body = r#"{"drain_seconds": 0}"#;
                let _ = write!(
                    stream,
                    "HTTP/1.1 {status} X\r\nContent-Type: application/json\r\n\
                     Content-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                );
                if status == 202 {
                    return;
                }
            }
        });
        port
    }

    fn rig(core_port: u16) -> (Context, Config) {
        let dir = std::env::temp_dir();
        let cx = Context {
            dir: dir.clone(),
            log: AppLog::new(&dir),
            http: reqwest::blocking::Client::new(),
            clock: RealClock(Instant::now()),
        };
        let conf = Config {
            core_port,
            cliproxy_port: 0,
            operator_token: TOKEN.into(),
            management_secret_file: PathBuf::new(),
            panel: false,
        };
        (cx, conf)
    }

    #[test]
    fn a_core_left_on_the_port_is_stopped_only_when_it_takes_the_token() {
        // (label, what the server on the port answers, stopped)
        let rows: &[(&str, fn(&str) -> u16, bool)] = &[
            (
                "the core of this data directory",
                |head| {
                    let ours = head.starts_with("POST /api/shutdown ")
                        && head.contains(&format!("x-relay-token: {TOKEN}\r\n"));
                    if ours {
                        202
                    } else {
                        401
                    }
                },
                true,
            ),
            ("a core that refuses the token", |_| 401, false),
            ("a server that is not a core", |_| 404, false),
        ];
        for (label, answer, stopped) in rows {
            let port = server(*answer);
            let (cx, conf) = rig(port);
            let line = stop_core(&cx, &conf);
            assert_eq!(!port_in_use(port), *stopped, "{label}: {line}");
            if !stopped {
                // What holds it is named: this test's own process.
                let pid = format!("(pid {})", std::process::id());
                assert!(line.contains(&pid), "{label}: {line}");
            }
        }
    }

    #[test]
    fn cliproxy_on_the_port_is_ours_only_by_the_secret_and_its_file() {
        let tmp = tempfile::tempdir().unwrap();
        let dir = tmp.path().join("lokki");
        let elsewhere = tmp.path().join("elsewhere");
        for d in [&dir, &elsewhere] {
            std::fs::create_dir(d).unwrap();
        }
        let file = |d: &Path, n: &str| {
            let p = d.join(n);
            std::fs::write(&p, b"").unwrap();
            Some(p)
        };
        let ours_file = file(&dir, "cliproxy-8_0_23.exe");
        let old_file = file(&dir, "cliproxy-8_0_22");
        // (label, accepts the secret, its executable, ours)
        let rows = [
            ("this lokki's CLIProxyAPI", true, ours_file.clone(), true),
            (
                "an older CLIProxyAPI file of this lokki",
                true,
                old_file,
                true,
            ),
            ("without the secret", false, ours_file.clone(), false),
            (
                "the core's file",
                true,
                file(&dir, "lokki-core-0_1_0"),
                false,
            ),
            (
                "a CLIProxyAPI file elsewhere",
                true,
                file(&elsewhere, "cliproxy-8_0_23.exe"),
                false,
            ),
            ("an executable the system does not name", true, None, false),
            (
                "a file that is gone",
                true,
                Some(dir.join("cliproxy-9")),
                false,
            ),
        ];
        for (label, answers, exe, want) in rows {
            assert_eq!(ours(answers, exe.as_deref(), Some(&dir)), want, "{label}");
        }
        assert!(
            !ours(true, ours_file.as_deref(), None),
            "no directory of its own"
        );
    }

    #[test]
    fn the_management_secret_is_what_cliproxy_accepts() {
        let port = server(|head| {
            if head.contains("authorization: Bearer right\r\n") {
                200
            } else {
                401
            }
        });
        let http = reqwest::blocking::Client::new();
        assert!(takes_secret(&http, port, "right"));
        assert!(!takes_secret(&http, port, "wrong"));
    }

    #[test]
    fn a_second_shell_on_the_data_directory_does_not_get_the_lock() {
        let dir = tempfile::tempdir().unwrap();
        let clock = FakeClock::default();
        let held = lock(dir.path(), &clock).unwrap();
        let e = lock(dir.path(), &clock).unwrap_err();
        assert!(e.contains("Another lokki is running"), "{e}");
        assert!(clock.now() >= LOCK_WAIT, "it waited for the lock");
        drop(held);
        assert!(lock(dir.path(), &clock).is_ok(), "a lock let go is taken");
    }
}
