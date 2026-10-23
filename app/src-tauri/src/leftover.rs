//! Sidecars an earlier run of lokki left running, found holding their ports when the shell
//! starts. The shell stops one only when it is certain the process is lokki's sidecar for
//! this data directory; anything else it leaves alone, and the start's error names it.
//!
//! The certainty rests on what only this data directory has, never on a pid an earlier run
//! wrote down. The shell holds the data directory's lock while it runs, and the system lets
//! the lock go when the shell ends, however it ends: a shell that holds it knows no other
//! lokki runs on this data directory, so no live shell owns what answers on its ports.
//!
//! The listener is found and held first (`process::listener`). It is sent a credential only
//! when it runs as the user the shell runs as, whose programs outside a sandbox can already
//! read every credential, and runs a sidecar's file: `lokki-core-*` for the core,
//! `cliproxy-*` for CLIProxyAPI, from whichever lokki started it, so that a leftover of an
//! AppImage's earlier mount or of a file an upgrade replaced qualifies. A program of another
//! user that took the port first is sent nothing. The credential then proves the data
//! directory: the core is stopped through its own `POST /api/shutdown` with this data
//! directory's operator token, and CLIProxyAPI, which has no such call, by its held pid once
//! it accepts this data directory's management secret.
use crate::process::{self, Listener};
use crate::sidecars::{port_in_use, Context, CoreHttp, Service, GONE_WAIT};
use lokki_logic::firstrun::Config;
use lokki_logic::sidecar_files::{CLIPROXY_PREFIX, CORE_PREFIX};
use lokki_logic::supervise::{drain_wait, Clock, ShutdownHttp, CLIPROXY_WAIT};
use std::fs::File;
use std::path::Path;
use std::time::Duration;

/// The lock file in the data directory.
pub const LOCK_FILE: &str = "lokki.lock";
/// How long a start waits for the lock: an updated lokki starts while the one it replaces
/// is still exiting.
const LOCK_WAIT: Duration = Duration::from_secs(5);
/// The most a start waits for a leftover core's drain, whatever `drain_seconds` it answers.
/// The start waits holding the sidecars' lock, which a quit needs, and before any window
/// opens. A core that drains longer is left to finish: the start ends on its port being in
/// use, and Try again asks it again.
const LEFTOVER_DRAIN_WAIT: Duration = Duration::from_secs(30);

/// Why the data directory's lock was not taken, each with the sentence for the error page.
#[derive(Debug)]
pub enum NotLocked {
    /// Another process holds it: another lokki runs on this data directory.
    Held(String),
    /// The lock file could not be opened, or its file system takes no locks.
    Failed(String),
}

/// The data directory's lock, for as long as the file is open.
pub fn lock(dir: &Path, clock: &impl Clock) -> std::result::Result<File, NotLocked> {
    let mut open = std::fs::OpenOptions::new();
    open.create(true).truncate(false).write(true);
    #[cfg(unix)]
    std::os::unix::fs::OpenOptionsExt::mode(&mut open, 0o600);
    let path = dir.join(LOCK_FILE);
    let file = open
        .open(&path)
        .map_err(|e| NotLocked::Failed(format!("lokki could not open {}: {e}", path.display())))?;
    let end = clock.now() + LOCK_WAIT;
    loop {
        match file.try_lock() {
            Ok(()) => return Ok(file),
            Err(std::fs::TryLockError::WouldBlock) if clock.now() < end => {
                clock.sleep(Duration::from_millis(100))
            }
            Err(std::fs::TryLockError::WouldBlock) => {
                return Err(NotLocked::Held(format!(
                    "Another lokki is running on the data directory {}. Open it from its \
                     tray icon, or quit it and try again.",
                    dir.display()
                )))
            }
            Err(std::fs::TryLockError::Error(e)) => {
                return Err(NotLocked::Failed(format!(
                    "lokki could not lock {}: {e}. lokki runs only on a data directory whose \
                     file system supports file locks.",
                    path.display()
                )))
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
            Service::Core => stop_core(cx, conf, &cx.clock),
            Service::Cliproxy => stop_cliproxy(cx, conf),
        })
        .collect()
}

fn stop_core(cx: &Context, conf: &Config, clock: &impl Clock) -> String {
    let port = conf.core_port;
    let found = process::listener(port);
    let Some(core) = found.as_ref().filter(|l| may_ask(l, CORE_PREFIX)) else {
        return left_alone(port, found.as_ref(), "a lokki core of this user");
    };
    let http = CoreHttp {
        client: &cx.http,
        port,
    };
    let pid = core.pid;
    match http.post_shutdown(&conf.operator_token) {
        Ok((202, drain)) => {
            let wait = leftover_drain_wait(drain);
            if freed(port, wait, clock) {
                format!(
                    "port {port}: a core an earlier run left, pid {pid}, took this data \
                     directory's token, drained and stopped"
                )
            } else {
                format!(
                    "port {port}: a core an earlier run left, pid {pid}, took this data \
                     directory's token and did not stop within {} s",
                    wait.as_secs()
                )
            }
        }
        _ => left_alone(port, found.as_ref(), "this data directory's core"),
    }
}

fn stop_cliproxy(cx: &Context, conf: &Config) -> String {
    let port = conf.cliproxy_port;
    let found = process::listener(port);
    let Some(proxy) = found.as_ref().filter(|l| may_ask(l, CLIPROXY_PREFIX)) else {
        return left_alone(port, found.as_ref(), "a lokki CLIProxyAPI of this user");
    };
    let answers = std::fs::read_to_string(&conf.management_secret_file)
        .is_ok_and(|secret| takes_secret(&cx.http, port, secret.trim()));
    if !answers {
        return left_alone(port, found.as_ref(), "this data directory's CLIProxyAPI");
    }
    let pid = proxy.pid;
    if proxy.stop(port, CLIPROXY_WAIT) && freed(port, GONE_WAIT, &cx.clock) {
        format!("port {port}: a CLIProxyAPI an earlier run left, pid {pid}, stopped")
    } else {
        format!("port {port}: a CLIProxyAPI an earlier run left, pid {pid}, did not stop")
    }
}

/// How long a start waits for a leftover core that answered `drain`: as long as a stop
/// would, and never more than `LEFTOVER_DRAIN_WAIT`.
fn leftover_drain_wait(drain: Option<f64>) -> Duration {
    drain_wait(drain).min(LEFTOVER_DRAIN_WAIT)
}

fn left_alone(port: u16, found: Option<&Listener>, what: &str) -> String {
    format!("port {port}: {} is not {what}; left alone", name(found))
}

/// Whether CLIProxyAPI on `port` accepts `secret` on its management API.
fn takes_secret(http: &reqwest::blocking::Client, port: u16, secret: &str) -> bool {
    http.get(format!("http://127.0.0.1:{port}/v0/management/debug"))
        .timeout(Duration::from_secs(5))
        .bearer_auth(secret)
        .send()
        .is_ok_and(|r| r.status().as_u16() == 200)
}

/// Whether a listener may be sent a credential: see the module's comment.
fn may_ask(found: &Listener, prefix: &str) -> bool {
    sidecar_of_this_user(found.same_user, found.exe.as_deref(), prefix)
}

/// A process of this user that runs a file named for the sidecar `prefix` names. Linux
/// reports a file an upgrade removed as `<path> (deleted)`, whose name still starts so.
fn sidecar_of_this_user(same_user: bool, exe: Option<&Path>, prefix: &str) -> bool {
    same_user
        && exe
            .and_then(Path::file_name)
            .is_some_and(|n| n.to_string_lossy().starts_with(prefix))
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
    use crate::process::tests::{answer, line_after, me_as};
    use crate::sidecars::{AppLog, RealClock};
    use crate::testing::FakeClock;
    use std::io::{BufRead, BufReader};
    use std::net::TcpListener;
    use std::path::PathBuf;
    use std::process::Stdio;
    use std::time::Instant;

    const TOKEN: &str = "tok-this-data-directory";

    /// A loopback HTTP server that answers each request with `status(request head)`.
    fn server(status: fn(&str) -> u16) -> u16 {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        std::thread::spawn(move || {
            for stream in listener.incoming() {
                answer(stream.unwrap(), 0, status);
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

    const SECRET: &str = "mgmt-this-data-directory";

    /// What holds the port in a row: a program of this user that is not a sidecar and
    /// answers yes to anything, or a copy of a sidecar's file that answers yes only to
    /// `credential`.
    enum Holder {
        Foreign,
        Sidecar { credential: &'static str },
    }

    #[test]
    fn a_secret_goes_only_to_a_lokki_sidecar_of_this_user() {
        let token = format!("x-relay-token: {TOKEN}\r\n");
        let bearer = format!("authorization: Bearer {SECRET}\r\n");
        use Holder::{Foreign, Sidecar};
        // (label, service, holder, stopped)
        let rows = [
            (
                "a foreign program on the core's port",
                Service::Core,
                Foreign,
                false,
            ),
            (
                "a foreign program on CLIProxyAPI's port",
                Service::Cliproxy,
                Foreign,
                false,
            ),
            (
                "this data directory's core",
                Service::Core,
                Sidecar {
                    credential: "token",
                },
                true,
            ),
            (
                "a core of another data directory",
                Service::Core,
                Sidecar {
                    credential: "other",
                },
                false,
            ),
            (
                "this data directory's CLIProxyAPI",
                Service::Cliproxy,
                Sidecar {
                    credential: "bearer",
                },
                true,
            ),
            (
                "a CLIProxyAPI of another data directory",
                Service::Cliproxy,
                Sidecar {
                    credential: "other",
                },
                false,
            ),
        ];
        for (label, service, holder, stopped) in rows {
            let tmp = tempfile::tempdir().unwrap();
            let secret_file = tmp.path().join("cliproxy-mgmt.secret");
            std::fs::write(&secret_file, format!("{SECRET}\n")).unwrap();
            let requests = std::sync::Arc::new(std::sync::Mutex::new(Vec::<String>::new()));
            let (port, mut child) = match holder {
                Foreign => {
                    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
                    let port = listener.local_addr().unwrap().port();
                    let seen = std::sync::Arc::clone(&requests);
                    std::thread::spawn(move || {
                        for stream in listener.incoming() {
                            // Recorded before the answer; a probe of the port sends nothing.
                            answer(stream.unwrap(), 0, |head| {
                                if !head.is_empty() {
                                    seen.lock().unwrap().push(head.to_string());
                                }
                                if head.starts_with("POST") {
                                    202
                                } else {
                                    200
                                }
                            });
                        }
                    });
                    (port, None)
                }
                Sidecar { credential } => {
                    let file = match service {
                        Service::Core => "lokki-core-test",
                        Service::Cliproxy => "cliproxy-test",
                    };
                    let (credential, ok) = match credential {
                        "token" => (token.as_str(), "202"),
                        "bearer" => (bearer.as_str(), "200"),
                        _ => ("a credential no one sends", "200"),
                    };
                    let mut child = me_as(tmp.path(), file, "http")
                        .env("LOKKI_TEST_CREDENTIAL", credential)
                        .env("LOKKI_TEST_STATUS", ok)
                        .stdout(Stdio::piped())
                        .spawn()
                        .unwrap();
                    let port: u16 = line_after(&mut child, "port ").parse().unwrap();
                    let out = BufReader::new(child.stdout.take().unwrap());
                    let seen = std::sync::Arc::clone(&requests);
                    let reader = std::thread::spawn(move || {
                        for line in out.lines().map_while(std::result::Result::ok) {
                            if let Some(request) = line.strip_prefix("request ") {
                                seen.lock().unwrap().push(request.to_string());
                            }
                        }
                    });
                    (port, Some((child, reader)))
                }
            };
            let (cx, mut conf) = rig(port);
            conf.cliproxy_port = port;
            conf.management_secret_file = secret_file;
            let line = match service {
                Service::Core => stop_core(&cx, &conf, &cx.clock),
                Service::Cliproxy => stop_cliproxy(&cx, &conf),
            };
            assert_eq!(!port_in_use(port), stopped, "{label}: {line}");
            // The holder's whole output, read to its end, says what it was asked.
            if let Some((mut child, reader)) = child.take() {
                let _ = child.kill();
                let _ = child.wait();
                reader.join().unwrap();
            }
            let asked = !requests.lock().unwrap().is_empty();
            match holder {
                Foreign => {
                    assert!(!asked, "{label}: a request reached it: {line}");
                    let pid = format!("(pid {})", std::process::id());
                    assert!(
                        line.contains(&pid),
                        "{label}: what holds it is named: {line}"
                    );
                }
                Sidecar { .. } => assert!(asked, "{label}: it was asked: {line}"),
            }
        }
    }

    #[test]
    fn only_a_sidecar_file_of_this_user_may_be_asked() {
        // (label, runs as this user, its executable, the sidecar's prefix, may be asked)
        let rows: &[(&str, bool, Option<&str>, &str, bool)] = &[
            (
                "CLIProxyAPI",
                true,
                Some("/opt/lokki/cliproxy-8_0_23"),
                CLIPROXY_PREFIX,
                true,
            ),
            (
                "an AppImage's earlier mount",
                true,
                Some("/tmp/.mount_lokkiAb/usr/bin/cliproxy-8_0_23"),
                CLIPROXY_PREFIX,
                true,
            ),
            (
                "a file an upgrade removed",
                true,
                Some("/usr/bin/cliproxy-8_0_22 (deleted)"),
                CLIPROXY_PREFIX,
                true,
            ),
            (
                "the core's Python child",
                true,
                Some("/opt/lokki/lokki-core-0_1_0"),
                CORE_PREFIX,
                true,
            ),
            (
                "another user's",
                false,
                Some("/opt/lokki/cliproxy-8_0_23"),
                CLIPROXY_PREFIX,
                false,
            ),
            (
                "the other sidecar's file",
                true,
                Some("/opt/lokki/lokki-core-0_1_0"),
                CLIPROXY_PREFIX,
                false,
            ),
            (
                "a program that is not a sidecar",
                true,
                Some("/usr/bin/python3"),
                CORE_PREFIX,
                false,
            ),
            (
                "a prefix only in the directory",
                true,
                Some("/cliproxy-x/proxy"),
                CLIPROXY_PREFIX,
                false,
            ),
            (
                "an executable the system does not name",
                true,
                None,
                CORE_PREFIX,
                false,
            ),
        ];
        for (label, same_user, exe, prefix, want) in rows {
            let got = sidecar_of_this_user(*same_user, exe.map(Path::new), prefix);
            assert_eq!(got, *want, "{label}");
        }
    }

    #[test]
    fn no_answer_holds_a_start_longer_than_the_leftover_wait() {
        let s = Duration::from_secs;
        // (drain_seconds answered, the wait)
        let rows = [
            (Some(0.0), s(5)),
            (Some(10.0), s(15)),
            (None, s(20)),
            (Some(3600.0), LEFTOVER_DRAIN_WAIT),
            (Some(f64::INFINITY), s(20)),
        ];
        for (drain, want) in rows {
            assert_eq!(leftover_drain_wait(drain), want, "{drain:?}");
        }
    }

    /// A clock whose every sleep passes a second, so a wait of tens of seconds probes the
    /// port tens of times.
    #[derive(Default)]
    struct SecondsClock(std::cell::Cell<Duration>);

    impl Clock for SecondsClock {
        fn now(&self) -> Duration {
            self.0.get()
        }
        fn sleep(&self, _: Duration) {
            self.0.set(self.0.get() + Duration::from_secs(1));
        }
    }

    #[test]
    fn a_core_still_draining_after_the_wait_is_left_to_drain_and_asked_again() {
        // (drain_seconds it answers, how long a start waits for it)
        for (drain, wait) in [(60, LEFTOVER_DRAIN_WAIT), (10, Duration::from_secs(15))] {
            let tmp = tempfile::tempdir().unwrap();
            let mut core = me_as(tmp.path(), "lokki-core-test", "http")
                .env(
                    "LOKKI_TEST_CREDENTIAL",
                    format!("x-relay-token: {TOKEN}\r\n"),
                )
                .env("LOKKI_TEST_STATUS", "202")
                .env("LOKKI_TEST_DRAIN", drain.to_string())
                .stdout(Stdio::piped())
                .spawn()
                .unwrap();
            let port: u16 = line_after(&mut core, "port ").parse().unwrap();
            let (cx, conf) = rig(port);
            for start in ["the start", "Try again"] {
                let clock = SecondsClock::default();
                let line = stop_core(&cx, &conf, &clock);
                let label = format!("drain {drain}, {start}: {line}");
                let within = format!("did not stop within {} s", wait.as_secs());
                assert!(line.contains(&within), "{label}");
                assert_eq!(clock.now(), wait, "{label}");
                let asked = line_after(&mut core, "request ");
                assert!(asked.starts_with("POST /api/shutdown "), "{label}: {asked}");
                assert!(port_in_use(port), "{label}: it is left to drain");
            }
            // The start's error page names what holds the port.
            let pid = format!("(pid {})", core.id());
            assert!(holder(port).contains(&pid), "{}", holder(port));
            core.kill().unwrap();
            core.wait().unwrap();
            assert!(
                stop(&cx, &conf).is_empty(),
                "drain {drain}: once it has drained, nothing holds a start"
            );
        }
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
        assert!(matches!(e, NotLocked::Held(_)), "{e:?}");
        assert!(clock.now() >= LOCK_WAIT, "it waited for the lock");
        drop(held);
        // A child another test forks holds a copy of the lock until it execs: real time.
        let real = RealClock(Instant::now());
        assert!(lock(dir.path(), &real).is_ok(), "a lock let go is taken");
        let e = lock(&dir.path().join("missing"), &clock).unwrap_err();
        assert!(
            matches!(e, NotLocked::Failed(_)),
            "a lock file that cannot be opened: {e:?}"
        );
    }
}
