use crate::{Result, CLIPROXY_VERSION};
use serde::Deserialize;
use std::io::Write;
use std::path::{Path, PathBuf};

/// What the shell reads from `shell-config`.
#[derive(Clone, Deserialize, PartialEq)]
pub struct Config {
    pub core_port: u16,
    pub cliproxy_port: u16,
    pub operator_token: String,
    pub management_secret_file: PathBuf,
    pub panel: bool,
}

// The token never reaches a log or a failed assertion through `{:?}`.
impl std::fmt::Debug for Config {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Config")
            .field("core_port", &self.core_port)
            .field("cliproxy_port", &self.cliproxy_port)
            .field("operator_token", &"<redacted>")
            .field("management_secret_file", &self.management_secret_file)
            .field("panel", &self.panel)
            .finish()
    }
}

#[derive(Deserialize)]
struct Init {
    created: bool,
}

/// `LOKKI_PORTS=A,B`: two ports, each 0 (any free port) or 1 to 65535.
fn parse_ports(ports: &str) -> Result<(String, String)> {
    let bad = || format!("LOKKI_PORTS must be two ports like 0,0, not {ports:?}");
    let (a, b) = ports.split_once(',').ok_or_else(bad)?;
    let (a, b) = (a.trim(), b.trim());
    if a.parse::<u16>().is_err() || b.parse::<u16>().is_err() {
        return Err(bad());
    }
    Ok((a.to_string(), b.to_string()))
}

/// One line in `app.log`; a log that cannot be written does not stop the app.
fn log_first_run(dir: &Path) {
    let logs = dir.join("logs");
    let _ = std::fs::create_dir_all(&logs);
    if let Ok(mut f) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(logs.join("app.log"))
    {
        let _ = writeln!(f, "first run: data directory created");
    }
}

/// `ensure_with` for the `LOKKI_PORTS` of this process's environment.
pub fn ensure(dir: &Path, run: impl Fn(&[&str]) -> Result<String>) -> Result<Config> {
    ensure_with(dir, std::env::var("LOKKI_PORTS").ok().as_deref(), run)
}

/// Runs `init`, then `shell-config`, through `run` (the arguments after the program, the
/// stdout back, an `Err` carrying stderr for a non-zero exit); `ports` is `LOKKI_PORTS`.
pub fn ensure_with(
    dir: &Path,
    ports: Option<&str>,
    run: impl Fn(&[&str]) -> Result<String>,
) -> Result<Config> {
    let dir_arg = dir
        .to_str()
        .ok_or("the data directory's path is not valid UTF-8")?;
    let mut init = vec![
        "init",
        "--data-dir",
        dir_arg,
        "--cliproxy-version",
        CLIPROXY_VERSION,
    ];
    let ports = ports.map(parse_ports).transpose()?;
    if let Some((core, cliproxy)) = &ports {
        init.extend(["--core-port", core, "--cliproxy-port", cliproxy]);
    }
    // Stdout may hold a secret from here on: what is wrong with it is said, it is not shown.
    let first: Init = serde_json::from_str(run(&init)?.trim())
        .map_err(|_| "init printed a line the shell cannot read".to_string())?;
    if first.created {
        log_first_run(dir);
    }
    let line = run(&["shell-config", "--data-dir", dir_arg])?;
    serde_json::from_str(line.trim())
        .map_err(|_| "shell-config printed a line the shell cannot read".to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::cell::RefCell;
    use std::fs;
    use std::io::Read;
    use std::process::{Command, Stdio};
    use std::time::{Duration, Instant};

    const TOKEN: &str = "tok-fake-operator";

    fn shell_config_line() -> String {
        json!({"core_port": 8381, "cliproxy_port": 8317, "operator_token": TOKEN,
               "management_secret_file": "/d/secrets/cliproxy-mgmt.secret", "panel": true})
        .to_string()
    }

    fn app_log_lines(dir: &Path) -> Vec<String> {
        fs::read_to_string(dir.join("logs").join("app.log"))
            .unwrap_or_default()
            .lines()
            .map(String::from)
            .collect()
    }

    #[test]
    fn ensure_runs_init_then_shell_config_and_reads_the_second() {
        assert!(
            !CLIPROXY_VERSION.is_empty(),
            "build.rs bakes the CLIProxyAPI version in"
        );
        // (LOKKI_PORTS, what init reports, the extra init flags, app.log lines)
        let rows: &[(Option<&str>, bool, &[&str], usize)] = &[
            (None, true, &[], 1),
            (
                Some("0,0"),
                true,
                &["--core-port", "0", "--cliproxy-port", "0"],
                1,
            ),
            (
                Some("9001,9002"),
                false,
                &["--core-port", "9001", "--cliproxy-port", "9002"],
                0,
            ),
        ];
        for (ports, created, flags, log_lines) in rows {
            let tmp = tempfile::tempdir().unwrap();
            let dir = tmp.path().join("data");
            fs::create_dir_all(dir.join("logs")).unwrap();
            let calls = RefCell::new(Vec::<Vec<String>>::new());
            let run = |args: &[&str]| -> Result<String> {
                calls
                    .borrow_mut()
                    .push(args.iter().map(|a| a.to_string()).collect());
                match args[0] {
                    // A second run reports no ports.
                    "init" => Ok(
                        json!({"created": created, "core_port": null, "cliproxy_port": null})
                            .to_string(),
                    ),
                    "shell-config" => Ok(shell_config_line()),
                    other => Err(format!("unexpected subcommand {other}")),
                }
            };
            let cfg = ensure_with(&dir, *ports, run).unwrap();

            let d = dir.to_str().unwrap();
            let mut init = vec![
                "init",
                "--data-dir",
                d,
                "--cliproxy-version",
                CLIPROXY_VERSION,
            ];
            init.extend(*flags);
            assert_eq!(
                *calls.borrow(),
                vec![init, vec!["shell-config", "--data-dir", d]],
                "LOKKI_PORTS {ports:?}"
            );
            assert_eq!(
                (cfg.core_port, cfg.cliproxy_port, cfg.panel),
                (8381, 8317, true)
            );
            assert_eq!(cfg.operator_token, TOKEN);
            assert_eq!(
                cfg.management_secret_file,
                Path::new("/d/secrets/cliproxy-mgmt.secret")
            );
            assert!(
                !format!("{cfg:?}").contains(TOKEN),
                "Debug must not print the token"
            );
            let log = app_log_lines(&dir);
            assert_eq!(log.len(), *log_lines, "LOKKI_PORTS {ports:?}: {log:?}");
            assert!(
                log.iter()
                    .all(|l| l.contains("first run: data directory created")),
                "{log:?}"
            );
        }
    }

    #[test]
    fn a_failing_init_is_the_error_and_shell_config_is_not_run() {
        let tmp = tempfile::tempdir().unwrap();
        let calls = RefCell::new(Vec::<String>::new());
        let run = |args: &[&str]| -> Result<String> {
            calls.borrow_mut().push(args[0].to_string());
            Err("lokki_core: port 99999 is not a port".to_string())
        };
        let err = ensure_with(tmp.path(), None, run).expect_err("init failed, so ensure fails");
        assert!(err.contains("port 99999 is not a port"), "{err}");
        assert_eq!(*calls.borrow(), ["init"]);
    }

    #[test]
    fn a_malformed_lokki_ports_is_an_error_and_runs_nothing() {
        let tmp = tempfile::tempdir().unwrap();
        for ports in ["9001", "a,b", "70000,1", "1,2,3", ""] {
            let run =
                |_: &[&str]| -> Result<String> { panic!("nothing runs for LOKKI_PORTS={ports:?}") };
            let err = ensure_with(tmp.path(), Some(ports), run)
                .expect_err("a malformed LOKKI_PORTS is an error");
            assert!(err.contains("LOKKI_PORTS"), "{err}");
        }
    }

    #[test]
    fn an_unparsable_shell_config_line_is_an_error_that_does_not_echo_it() {
        // stdout may hold the token, so the error says what was wrong and never what was read:
        // serde's own message would quote the mistyped value, here the token.
        for line in [
            format!(r#"{{"operator_token": "{TOKEN}", "core_port": "{TOKEN}"}}"#),
            format!("Traceback {TOKEN}"),
        ] {
            let tmp = tempfile::tempdir().unwrap();
            let run = |args: &[&str]| -> Result<String> {
                Ok(if args[0] == "init" {
                    json!({"created": false}).to_string()
                } else {
                    line.clone()
                })
            };
            let err =
                ensure_with(tmp.path(), None, run).expect_err("an unparsable line is an error");
            assert!(
                !err.is_empty() && !err.contains(TOKEN),
                "the error echoed stdout"
            );
        }
    }

    #[test]
    fn the_real_init_and_shell_config_produce_what_ensure_reads() {
        // The one test that binds the two programs' contract. A failure message carries
        // stderr and field names only: stdout holds the operator token.
        let core_dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../core");
        let run = |args: &[&str]| -> Result<String> {
            // The environment the shell gives a core subcommand: the caller's RELAY_* does not reach it.
            // A Windows Python installs `python`, not `python3`.
            let python = if cfg!(windows) { "python" } else { "python3" };
            let mut child = Command::new(python)
                .args(["-m", "lokki_core"])
                .args(args)
                .current_dir(&core_dir)
                .env_clear()
                .envs(crate::env::for_sidecars(std::env::vars_os()))
                .env("PYTHONDONTWRITEBYTECODE", "1")
                .stdin(Stdio::null())
                .stdout(Stdio::piped())
                .stderr(Stdio::piped())
                .spawn()
                .map_err(|e| format!("this test runs `python3 -m lokki_core` in core/ and needs python3 (with pyyaml) on PATH: {e}"))?;
            let (mut stdout, mut stderr) =
                (child.stdout.take().unwrap(), child.stderr.take().unwrap());
            let out = std::thread::spawn(move || {
                let mut buf = Vec::new();
                let _ = stdout.read_to_end(&mut buf);
                buf
            });
            let err = std::thread::spawn(move || {
                let mut buf = Vec::new();
                let _ = stderr.read_to_end(&mut buf);
                buf
            });
            let deadline = Instant::now() + Duration::from_secs(60);
            let status = loop {
                if let Some(status) = child.try_wait().map_err(|e| e.to_string())? {
                    break status;
                }
                if Instant::now() > deadline {
                    let _ = child.kill();
                    let _ = child.wait();
                    return Err(format!(
                        "`python3 -m lokki_core {}` did not finish within 60 s",
                        args[0]
                    ));
                }
                std::thread::sleep(Duration::from_millis(20));
            };
            let (out, err) = (out.join().unwrap(), err.join().unwrap());
            if !status.success() {
                return Err(format!(
                    "`python3 -m lokki_core {}` exited {}: {}",
                    args[0],
                    status,
                    String::from_utf8_lossy(&err)
                ));
            }
            String::from_utf8(out)
                .map_err(|_| format!("`{}` printed bytes that are not UTF-8", args[0]))
        };
        let tmp = tempfile::tempdir().unwrap();
        let dir = tmp.path().join("data");
        let first =
            ensure_with(&dir, Some("0,0"), run).unwrap_or_else(|e| panic!("first run: {e}"));

        assert!(
            first.core_port != 0
                && first.cliproxy_port != 0
                && first.core_port != first.cliproxy_port,
            "ports 0 mean free ports, got {}/{}",
            first.core_port,
            first.cliproxy_port
        );
        assert!(!first.operator_token.is_empty());
        assert!(!first.panel, "init writes cliproxy.panel: false");
        assert_eq!(
            first.management_secret_file,
            dir.join("secrets").join("cliproxy-mgmt.secret")
        );
        assert!(first.management_secret_file.is_file());
        let core_yaml = fs::read_to_string(dir.join("config.yaml")).unwrap();
        assert!(
            core_yaml.contains(&first.operator_token),
            "shell-config's token is not in config.yaml"
        );
        assert!(
            core_yaml.contains(&format!("127.0.0.1:{}", first.core_port)),
            "shell-config's core port is not config.yaml's listen"
        );
        assert_eq!(app_log_lines(&dir).len(), 1);

        // A second run: init reports no ports, shell-config answers the same, app.log gains nothing.
        let second =
            ensure_with(&dir, Some("0,0"), run).unwrap_or_else(|e| panic!("second run: {e}"));
        assert!(second == first, "the second run read a different config");
        assert_eq!(app_log_lines(&dir).len(), 1);
    }
}
