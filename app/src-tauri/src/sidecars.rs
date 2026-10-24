//! The two sidecars as processes: where their files are, how they start, where their
//! output goes, when they are ready, and how their whole tree stops. What to do about an
//! exit is `lokki_logic::supervise`'s.
use crate::leftover;
use crate::process::{self, Tree};
use lokki_logic::firstrun::Config;
use lokki_logic::logs::{self, Rotating};
use lokki_logic::supervise::{
    self, Action, Child, Clock, Policy, ShutdownHttp, Stopped, Supervisor,
};
use lokki_logic::{env, Result, CLIPROXY_SIDECAR, CORE_SIDECAR};
use std::ffi::OsString;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Command, ExitStatus, Stdio};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

/// How long a sidecar has to become ready, and a core subcommand to finish.
const READY_WAIT: Duration = Duration::from_secs(30);
const SUBCOMMAND_WAIT: Duration = Duration::from_secs(60);
/// How long a stopped sidecar's group has to be gone: a reaped leader can leave a member
/// that is still dying for a few milliseconds.
pub const GONE_WAIT: Duration = Duration::from_secs(2);

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Service {
    Core,
    Cliproxy,
}

impl Service {
    pub fn parse(name: &str) -> Option<Service> {
        match name {
            "core" => Some(Service::Core),
            "cliproxy" => Some(Service::Cliproxy),
            _ => None,
        }
    }

    pub fn name(self) -> &'static str {
        match self {
            Service::Core => "core",
            Service::Cliproxy => "cliproxy",
        }
    }

    /// The name a sentence uses.
    pub fn title(self) -> &'static str {
        match self {
            Service::Core => "the core",
            Service::Cliproxy => "CLIProxyAPI",
        }
    }

    /// The name that opens a sentence.
    pub fn subject(self) -> &'static str {
        match self {
            Service::Core => "The core",
            Service::Cliproxy => "CLIProxyAPI",
        }
    }

    pub fn port(self, conf: &Config) -> u16 {
        match self {
            Service::Core => conf.core_port,
            Service::Cliproxy => conf.cliproxy_port,
        }
    }

    pub fn log_path(self, dir: &Path) -> PathBuf {
        dir.join("logs").join(format!("{}.log", self.name()))
    }

    fn file(self) -> &'static str {
        match self {
            Service::Core => CORE_SIDECAR,
            Service::Cliproxy => CLIPROXY_SIDECAR,
        }
    }
}

/// The sidecar files as the bundler places them beside the executable: the target triple
/// stripped, `.exe` kept on Windows.
pub fn sidecar_file_names() -> [String; 2] {
    [Service::Core, Service::Cliproxy]
        .map(|s| format!("{}{}", s.file(), std::env::consts::EXE_SUFFIX))
}

pub fn exe_dir() -> Result<PathBuf> {
    let exe = std::env::current_exe()
        .map_err(|e| format!("Lokki cannot find its own executable: {e}"))?;
    exe.parent()
        .map(Path::to_path_buf)
        .ok_or_else(|| "Lokki's executable has no directory".to_string())
}

fn program(service: Service) -> Result<PathBuf> {
    Ok(exe_dir()?.join(format!(
        "{}{}",
        service.file(),
        std::env::consts::EXE_SUFFIX
    )))
}

/// The shell's own log, `logs/app.log`. It opens once `init` has made `logs/`; a line
/// written before that goes to stderr. No caller passes a token or the secret.
pub struct AppLog {
    path: PathBuf,
    file: Mutex<Option<Rotating>>,
}

impl AppLog {
    pub fn new(dir: &Path) -> AppLog {
        AppLog {
            path: dir.join("logs").join("app.log"),
            file: Mutex::new(None),
        }
    }

    pub fn line(&self, msg: impl std::fmt::Display) {
        let line = format!("{} {msg}\n", utc_now());
        let mut file = self.file.lock().unwrap();
        if file.is_none() && self.path.parent().is_some_and(Path::is_dir) {
            *file = Rotating::new(&self.path, logs::CAP).ok();
        }
        let written = file
            .as_mut()
            .is_some_and(|f| f.write_all(line.as_bytes()).is_ok());
        if !written {
            eprint!("{line}");
        }
    }
}

/// `YYYY-MM-DDTHH:MM:SSZ`, from the system clock.
fn utc_now() -> String {
    utc(SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_secs()))
}

/// `YYYY-MM-DDTHH:MM:SSZ` for seconds since the Unix epoch.
fn utc(secs: u64) -> String {
    let (days, rem) = (secs / 86_400, secs % 86_400);
    // Howard Hinnant's civil_from_days.
    let z = days as i64 + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = yoe + era * 400 + i64::from(m <= 2);
    format!(
        "{y:04}-{m:02}-{d:02}T{:02}:{:02}:{:02}Z",
        rem / 3600,
        rem % 3600 / 60,
        rem % 60
    )
}

/// A command in the data directory with the allow-listed environment.
fn command(program: &Path, dir: &Path) -> Command {
    let mut cmd = process::command(program);
    cmd.current_dir(dir)
        .env_clear()
        .envs(env::for_sidecars(std::env::vars_os()));
    cmd
}

fn drain(mut pipe: impl Read + Send + 'static) -> thread::JoinHandle<Vec<u8>> {
    thread::spawn(move || {
        let mut buf = Vec::new();
        let _ = pipe.read_to_end(&mut buf);
        buf
    })
}

/// Runs `lokki-core <args>` (`init`, `shell-config`, `sync-cliproxy`, `set-port`) and returns its
/// stdout, which may hold the operator token and is never logged. Its stderr goes to
/// `app.log` and, on a non-zero exit, is the error.
pub fn run_core(dir: &Path, args: &[&str], log: &AppLog) -> Result<String> {
    let sub = args.first().copied().unwrap_or_default();
    let program = program(Service::Core)?;
    // `init` runs before the data directory exists: its nearest existing ancestor is the
    // working directory then.
    let cwd = dir.ancestors().find(|p| p.is_dir()).unwrap_or(dir);
    let (mut child, tree) = process::spawn(
        command(&program, cwd)
            .args(args)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped()),
    )
    .map_err(|e| format!("could not run {}: {e}", program.display()))?;
    let (out, err) = (
        drain(child.stdout.take().unwrap()),
        drain(child.stderr.take().unwrap()),
    );
    let mut proc = Proc::new(child, tree);
    let deadline = Instant::now() + SUBCOMMAND_WAIT;
    while !proc.try_wait() {
        if Instant::now() > deadline {
            proc.kill_tree();
            return Err(format!(
                "lokki-core {sub} did not finish within {} s",
                SUBCOMMAND_WAIT.as_secs()
            ));
        }
        thread::sleep(Duration::from_millis(20));
    }
    let (out, err) = (
        out.join().unwrap_or_default(),
        err.join().unwrap_or_default(),
    );
    let err = String::from_utf8_lossy(&err).trim().to_string();
    for line in err.lines() {
        log.line(format_args!("lokki-core {sub}: {line}"));
    }
    match proc.status {
        Some(status) if status.success() => String::from_utf8(out)
            .map_err(|_| format!("lokki-core {sub} printed bytes that are not UTF-8")),
        status => Err(format!(
            "lokki-core {sub} failed ({}): {err}",
            describe(status)
        )),
    }
}

/// `shell-config`'s line, read again before each start: a port or the panel may have changed.
pub fn shell_config(dir: &Path, log: &AppLog) -> Result<Config> {
    let dir_arg = dir
        .to_str()
        .ok_or("the data directory's path is not valid UTF-8")?;
    let line = run_core(dir, &["shell-config", "--data-dir", dir_arg], log)?;
    // The line holds the token: what is wrong with it is said, it is not shown.
    serde_json::from_str(line.trim())
        .map_err(|_| "shell-config printed a line the shell cannot read".to_string())
}

/// `set-port`: the service's port in the settings, back to `port`, CLIProxyAPI's file in step.
pub fn set_port(dir: &Path, service: Service, port: u16, log: &AppLog) -> Result<()> {
    let dir_arg = dir
        .to_str()
        .ok_or("the data directory's path is not valid UTF-8")?;
    let flag = match service {
        Service::Core => "--core-port",
        Service::Cliproxy => "--cliproxy-port",
    };
    let port = port.to_string();
    run_core(dir, &["set-port", "--data-dir", dir_arg, flag, &port], log).map(|_| ())
}

fn describe(status: Option<ExitStatus>) -> String {
    status.map_or_else(|| "no exit status".to_string(), |s| s.to_string())
}

/// A sidecar process and its tree.
pub struct Proc {
    child: std::process::Child,
    tree: Tree,
    status: Option<ExitStatus>,
}

impl Proc {
    fn new(child: std::process::Child, tree: Tree) -> Proc {
        Proc {
            child,
            tree,
            status: None,
        }
    }

    pub fn pid(&self) -> u32 {
        self.child.id()
    }
}

impl Child for Proc {
    fn try_wait(&mut self) -> bool {
        if self.status.is_some() {
            return true;
        }
        match self.child.try_wait() {
            Ok(None) => false,
            Ok(Some(status)) => {
                self.status = Some(status);
                // A one-file PyInstaller bootloader can die and leave its Python child
                // holding the port; nothing of the tree outlives its leader.
                self.tree.kill();
                true
            }
            Err(_) => true,
        }
    }

    fn terminate(&mut self) {
        if self.status.is_some() {
            return;
        }
        // One that cannot be sent is waited out like one not heard, then killed.
        self.tree.interrupt();
    }

    fn kill_tree(&mut self) {
        if self.status.is_some() {
            return;
        }
        self.tree.kill();
        if let Ok(status) = self.child.wait() {
            self.status = Some(status);
        }
    }
}

/// The wall clock `supervise` reads, from a fixed start.
pub struct RealClock(pub Instant);

impl Clock for RealClock {
    fn now(&self) -> Duration {
        self.0.elapsed()
    }
    fn sleep(&self, d: Duration) {
        thread::sleep(d)
    }
}

/// Loopback HTTP to the core. The client is built once, off the async runtime: a blocking
/// client may not be used inside it.
pub struct CoreHttp<'a> {
    pub client: &'a reqwest::blocking::Client,
    pub port: u16,
}

impl CoreHttp<'_> {
    fn url(&self, path: &str) -> String {
        format!("http://127.0.0.1:{}{path}", self.port)
    }

    pub fn healthy(&self) -> bool {
        self.client
            .get(self.url("/healthz"))
            .timeout(Duration::from_secs(2))
            .send()
            .is_ok_and(|r| r.status().as_u16() == 200)
    }

    /// An operator call; the JSON body of a 2xx.
    pub fn json(
        &self,
        method: reqwest::Method,
        path: &str,
        token: &str,
        body: Option<&serde_json::Value>,
    ) -> Result<serde_json::Value> {
        let mut req = self
            .client
            .request(method, self.url(path))
            .timeout(Duration::from_secs(10))
            .header("X-Relay-Token", token);
        if let Some(body) = body {
            req = req
                .header("Content-Type", "application/json")
                .body(body.to_string());
        }
        let resp = req
            .send()
            .map_err(|e| format!("the core did not answer {path}: {e}"))?;
        let status = resp.status().as_u16();
        if !(200..300).contains(&status) {
            return Err(format!("the core answered {path} with {status}"));
        }
        let bytes = resp
            .bytes()
            .map_err(|e| format!("the core's answer to {path} was cut: {e}"))?;
        serde_json::from_slice(&bytes)
            .map_err(|_| format!("the core's answer to {path} is not JSON"))
    }
}

impl ShutdownHttp for CoreHttp<'_> {
    fn post_shutdown(&self, token: &str) -> Result<(u16, Option<f64>)> {
        let resp = self
            .client
            .post(self.url("/api/shutdown"))
            .timeout(Duration::from_secs(10))
            .header("X-Relay-Token", token)
            .send()
            .map_err(|e| e.to_string())?;
        let status = resp.status().as_u16();
        let drain = resp
            .bytes()
            .ok()
            .and_then(|b| serde_json::from_slice::<serde_json::Value>(&b).ok())
            .and_then(|v| v.get("drain_seconds").and_then(serde_json::Value::as_f64));
        Ok((status, drain))
    }
}

/// What the shell's supervision needs from its surroundings.
pub struct Context {
    pub dir: PathBuf,
    pub log: AppLog,
    pub http: reqwest::blocking::Client,
    pub clock: RealClock,
}

struct Slot {
    proc: Option<Proc>,
    sup: Supervisor,
    restart_at: Option<Instant>,
}

impl Slot {
    fn new(service: Service) -> Slot {
        Slot {
            proc: None,
            sup: Supervisor::new(service.name(), Policy::default()),
            restart_at: None,
        }
    }
}

/// The two supervised sidecars. One lock holds this, so a stop or a start the shell asked
/// for and the exits the monitor sees never interleave.
pub struct Sidecars {
    core: Slot,
    cliproxy: Slot,
    /// The pid each service's last `stop` took, for the proof that its group is gone.
    stopped: Vec<(Service, u32)>,
}

impl Default for Sidecars {
    fn default() -> Sidecars {
        Sidecars {
            core: Slot::new(Service::Core),
            cliproxy: Slot::new(Service::Cliproxy),
            stopped: Vec::new(),
        }
    }
}

impl Sidecars {
    /// The sidecars behind their lock, also after a panic while another thread held it: the
    /// slots still say which processes run, so a stop or a start can go on from them.
    pub fn lock(side: &Mutex<Sidecars>) -> MutexGuard<'_, Sidecars> {
        side.lock().unwrap_or_else(PoisonError::into_inner)
    }

    fn slot(&mut self, service: Service) -> &mut Slot {
        match service {
            Service::Core => &mut self.core,
            Service::Cliproxy => &mut self.cliproxy,
        }
    }

    /// The running pids, core first.
    pub fn pids(&self) -> (Option<u32>, Option<u32>) {
        (
            self.core.proc.as_ref().map(Proc::pid),
            self.cliproxy.proc.as_ref().map(Proc::pid),
        )
    }

    /// `sync-cliproxy`, CLIProxyAPI until it accepts, then the core until `/healthz` is 200.
    /// An error names the sidecar that did not start.
    pub fn start_all(
        &mut self,
        cx: &Context,
        conf: &Config,
    ) -> std::result::Result<(), (Service, String)> {
        for service in [Service::Cliproxy, Service::Core] {
            self.start(cx, conf, service, true)
                .map_err(|e| (service, e))?;
        }
        Ok(())
    }

    /// Starts one sidecar (CLIProxyAPI after `sync-cliproxy`), waiting until it is ready
    /// when `wait` is set. A sidecar that exits while starting stays in its slot, so its
    /// exit reaches the supervisor.
    pub fn start(
        &mut self,
        cx: &Context,
        conf: &Config,
        service: Service,
        wait: bool,
    ) -> Result<()> {
        let dir = &cx.dir;
        let args: Vec<OsString> = match service {
            Service::Cliproxy => {
                let dir_arg = dir
                    .to_str()
                    .ok_or("the data directory's path is not valid UTF-8")?;
                run_core(dir, &["sync-cliproxy", "--data-dir", dir_arg], &cx.log)?;
                vec![
                    "-config".into(),
                    dir.join("cliproxy").join("config.yaml").into(),
                ]
            }
            Service::Core => vec!["--config".into(), dir.join("config.yaml").into()],
        };
        let port = service.port(conf);
        // Something already answering on the port is not this sidecar: lokki never takes
        // over a CLIProxyAPI, or a core, it did not start (`leftover` stops what an earlier
        // run left before the first start).
        if port_in_use(port) {
            return Err(format!(
                "port {port} is already in use by {}, so Lokki did not start {}",
                leftover::holder(port),
                service.title()
            ));
        }
        let proc = spawn(service, dir, &args)?;
        cx.log.line(format_args!(
            "{} started, pid {}",
            service.name(),
            proc.pid()
        ));
        let slot = self.slot(service);
        slot.restart_at = None;
        let proc = slot.proc.insert(proc);
        if !wait {
            return Ok(());
        }
        let ready = || match service {
            Service::Core => CoreHttp {
                client: &cx.http,
                port,
            }
            .healthy(),
            Service::Cliproxy => port_in_use(port),
        };
        let what = match service {
            Service::Core => format!("answer /healthz on port {port}"),
            Service::Cliproxy => format!("accept on port {port}"),
        };
        match wait_ready(proc, &cx.clock, READY_WAIT, ready) {
            Ok(()) => Ok(()),
            Err(NotReady::Exited) => Err(format!(
                "{} exited ({}) before it would {what}; see logs/{}.log",
                service.subject(),
                describe(proc.status),
                service.name()
            )),
            Err(NotReady::TimedOut) => Err(format!(
                "{} did not {what} within {} s; see logs/{}.log",
                service.subject(),
                READY_WAIT.as_secs(),
                service.name()
            )),
        }
    }

    /// Stops one sidecar: the core is drained before any kill, CLIProxyAPI is terminated and
    /// then killed. Its exit is the shell's own and counts for nothing.
    pub fn stop(
        &mut self,
        cx: &Context,
        conf: &Config,
        token: &str,
        service: Service,
    ) -> Option<Stopped> {
        // A service with no child keeps no pid: the one it had may belong to anything now.
        self.stopped.retain(|(s, _)| *s != service);
        let slot = self.slot(service);
        slot.restart_at = None;
        let mut proc = slot.proc.take()?;
        self.stopped.push((service, proc.pid()));
        let slot = self.slot(service);
        slot.sup.expect_stop();
        let stopped = match service {
            Service::Core => supervise::stop_core(
                &CoreHttp {
                    client: &cx.http,
                    port: conf.core_port,
                },
                token,
                &mut proc,
                &cx.clock,
            ),
            Service::Cliproxy => supervise::stop_cliproxy(&mut proc, &cx.clock),
        };
        slot.sup.on_exit(cx.clock.now());
        cx.log.line(format_args!(
            "{} stopped, pid {}: drained={} killed={}",
            service.name(),
            proc.pid(),
            stopped.drained,
            stopped.killed
        ));
        Some(stopped)
    }

    /// The pids of the children the last `stop` of each service took.
    pub fn stopped_pids(&self) -> Vec<(Service, u32)> {
        self.stopped.clone()
    }

    /// The stopped pids whose group still has a live member once `limit` has passed,
    /// probed every 100 ms of `clock`.
    pub fn alive_after_stop(&self, clock: &impl Clock, limit: Duration) -> Vec<(Service, u32)> {
        let end = clock.now() + limit;
        loop {
            let mut alive = self.stopped_pids();
            alive.retain(|&(_, pid)| group_alive(pid));
            if alive.is_empty() || clock.now() >= end {
                return alive;
            }
            clock.sleep(Duration::from_millis(100));
        }
    }

    /// Forgets the exits a sidecar had: the user asked for one more try.
    pub fn forgive(&mut self, service: Service) {
        let slot = self.slot(service);
        slot.sup = Supervisor::new(service.name(), Policy::default());
        slot.restart_at = None;
    }

    /// One pass of the monitor: feeds exits to the supervisors and runs the restarts that
    /// are due. Returns the sidecars that gave up, each with the sentence for the error page.
    pub fn poll(&mut self, cx: &Context, conf: &Config) -> Vec<(Service, String)> {
        let mut gave_up = Vec::new();
        for service in [Service::Cliproxy, Service::Core] {
            let slot = self.slot(service);
            let exited = slot.proc.as_mut().is_some_and(|p| p.try_wait());
            if exited {
                let proc = slot.proc.take().unwrap();
                let what = format!(
                    "{} exited ({}), pid {}",
                    service.name(),
                    describe(proc.status),
                    proc.pid()
                );
                if let Some(sentence) = self.on_exit(cx, service, &what) {
                    gave_up.push((service, sentence));
                }
            }
            let slot = self.slot(service);
            if slot.restart_at.is_some_and(|at| Instant::now() >= at) {
                slot.restart_at = None;
                if let Err(e) = self.start(cx, conf, service, false) {
                    let what = format!("{} did not restart: {e}", service.name());
                    if let Some(sentence) = self.on_exit(cx, service, &what) {
                        gave_up.push((service, sentence));
                    }
                }
            }
        }
        gave_up
    }

    fn on_exit(&mut self, cx: &Context, service: Service, what: &str) -> Option<String> {
        let slot = self.slot(service);
        match slot.sup.on_exit(cx.clock.now()) {
            Action::Restart(after) => {
                cx.log
                    .line(format_args!("{what}; restarting in {} s", after.as_secs()));
                slot.restart_at = Some(Instant::now() + after);
                None
            }
            Action::GiveUp => {
                cx.log.line(format_args!(
                    "{what}; four exits within a minute, not restarting"
                ));
                Some(format!(
                    "{} exited four times within a minute, so Lokki stopped restarting it.",
                    service.subject()
                ))
            }
            Action::Expected => None,
        }
    }
}

/// Whether any member of the process group `pid` leads is still alive.
/// `pid` is a group leader the shell started, so a member that answers `EPERM` cannot be
/// ours: only `ESRCH` says the group is gone. On Windows the probe is the leader's pid alone;
/// the end of its job took its tree.
#[cfg(unix)]
pub fn group_alive(pid: u32) -> bool {
    let answered = unsafe { libc::kill(-(pid as libc::pid_t), 0) };
    answered == 0 || std::io::Error::last_os_error().raw_os_error() != Some(libc::ESRCH)
}

#[cfg(windows)]
pub fn group_alive(pid: u32) -> bool {
    let filter = format!("PID eq {pid}");
    let pid = pid.to_string();
    process::command("tasklist")
        .args(["/FI", &filter, "/NH"])
        .stderr(Stdio::null())
        .output()
        .map(|out| {
            String::from_utf8_lossy(&out.stdout)
                .split_whitespace()
                .any(|word| word == pid)
        })
        // A probe that cannot run proves nothing gone.
        .unwrap_or(true)
}

/// Whether something accepts connections on `127.0.0.1:<port>`.
pub fn port_in_use(port: u16) -> bool {
    TcpStream::connect_timeout(
        &SocketAddr::from(([127, 0, 0, 1], port)),
        Duration::from_millis(500),
    )
    .is_ok()
}

#[derive(Debug, PartialEq, Eq)]
enum NotReady {
    Exited,
    TimedOut,
}

/// Waits until `ready` holds while the child is alive. The child is checked after the
/// probe: another program answering on the port while this child has died on its bind is
/// not this child being ready.
fn wait_ready(
    child: &mut impl Child,
    clock: &impl Clock,
    limit: Duration,
    mut ready: impl FnMut() -> bool,
) -> std::result::Result<(), NotReady> {
    let end = clock.now() + limit;
    loop {
        let up = ready();
        if child.try_wait() {
            return Err(NotReady::Exited);
        }
        if up {
            return Ok(());
        }
        if clock.now() >= end {
            return Err(NotReady::TimedOut);
        }
        clock.sleep(Duration::from_millis(100));
    }
}

/// Spawns a sidecar with its stdout and stderr appended, whole lines at a time, to its
/// rotating log.
fn spawn(service: Service, dir: &Path, args: &[OsString]) -> Result<Proc> {
    let program = program(service)?;
    let sink = Arc::new(Mutex::new(
        Rotating::new(service.log_path(dir), logs::CAP)
            .map_err(|e| format!("could not open logs/{}.log: {e}", service.name()))?,
    ));
    let (mut child, tree) = process::spawn(
        command(&program, dir)
            .args(args)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped()),
    )
    .map_err(|e| format!("could not start {}: {e}", program.display()))?;
    let pipes: [Box<dyn Read + Send>; 2] = [
        Box::new(child.stdout.take().unwrap()),
        Box::new(child.stderr.take().unwrap()),
    ];
    for pipe in pipes {
        let sink = Arc::clone(&sink);
        thread::spawn(move || {
            let mut reader = BufReader::new(pipe);
            let mut line = Vec::new();
            while reader.read_until(b'\n', &mut line).is_ok_and(|n| n > 0) {
                let _ = sink.lock().unwrap().write_all(&line);
                line.clear();
            }
        });
    }
    Ok(Proc::new(child, tree))
}

/// The last `n` lines of a log, for the error page.
pub fn tail(path: &Path, n: usize) -> String {
    let text = std::fs::read(path).unwrap_or_default();
    let text = String::from_utf8_lossy(&text);
    let lines: Vec<&str> = text.lines().collect();
    lines[lines.len().saturating_sub(n)..].join("\n")
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testing::FakeClock;
    use std::net::TcpListener;

    struct FakeChild<'a> {
        clock: &'a FakeClock,
        exits_at: Option<u64>,
    }

    impl Child for FakeChild<'_> {
        fn try_wait(&mut self) -> bool {
            self.exits_at
                .is_some_and(|at| self.clock.now() >= Duration::from_secs(at))
        }
        fn terminate(&mut self) {}
        fn kill_tree(&mut self) {}
    }

    #[test]
    fn a_start_is_ready_only_while_its_own_child_lives() {
        let s = Duration::from_secs;
        // (label, child exits at, probe answers from, outcome)
        type Row = (
            &'static str,
            Option<u64>,
            Option<u64>,
            std::result::Result<(), NotReady>,
        );
        let rows: &[Row] = &[
            (
                "another program answers while the child has died on its bind",
                Some(0),
                Some(0),
                Err(NotReady::Exited),
            ),
            (
                "the child dies as the other program starts answering",
                Some(1),
                Some(1),
                Err(NotReady::Exited),
            ),
            ("the child answers at once", None, Some(0), Ok(())),
            ("the child answers after 2 s", None, Some(2), Ok(())),
            (
                "the child dies and nothing answers",
                Some(1),
                None,
                Err(NotReady::Exited),
            ),
            (
                "the child lives and never answers",
                None,
                None,
                Err(NotReady::TimedOut),
            ),
        ];
        for (label, exits_at, ready_from, want) in rows {
            let clock = FakeClock::default();
            let mut child = FakeChild {
                clock: &clock,
                exits_at: *exits_at,
            };
            let ready = || ready_from.is_some_and(|at| clock.now() >= s(at));
            let got = wait_ready(&mut child, &clock, s(30), ready);
            assert_eq!(&got, want, "{label}");
        }
    }

    #[test]
    fn a_port_something_listens_on_is_in_use() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        assert!(port_in_use(port));
        drop(listener);
        // A child another test forks holds every open socket until it execs, so the port is
        // free again soon after the drop, not at once.
        let deadline = Instant::now() + Duration::from_secs(1);
        while port_in_use(port) {
            assert!(Instant::now() < deadline, "the port is still in use");
            thread::sleep(Duration::from_millis(10));
        }
    }

    #[test]
    fn utc_renders_the_civil_date() {
        let rows = [
            (0, "1970-01-01T00:00:00Z"),
            (951_782_400, "2000-02-29T00:00:00Z"),
            (1_709_251_199, "2024-02-29T23:59:59Z"),
            (4_107_542_399, "2100-02-28T23:59:59Z"),
        ];
        for (secs, want) in rows {
            assert_eq!(utc(secs), want, "{secs}");
        }
    }

    #[test]
    fn tail_keeps_the_last_lines() {
        let dir = tempfile::tempdir().unwrap();
        let log = dir.path().join("core.log");
        std::fs::write(&log, "one\ntwo\r\nthree\n").unwrap();
        // (lines asked, what comes back)
        let rows = [(2, "two\nthree"), (50, "one\ntwo\nthree"), (0, "")];
        for (n, want) in rows {
            assert_eq!(tail(&log, n), want, "{n} lines");
        }
        assert_eq!(tail(&dir.path().join("missing.log"), 50), "");
    }

    /// A `sleep 30` started as a sidecar is.
    #[cfg(unix)]
    fn sleeper() -> Proc {
        let (child, tree) = process::spawn(process::command("sleep").arg("30")).unwrap();
        Proc::new(child, tree)
    }

    #[cfg(unix)]
    #[test]
    fn a_process_group_is_alive_until_its_members_are_gone() {
        let mut proc = sleeper();
        let pid = proc.pid();
        let side = Sidecars {
            stopped: vec![(Service::Core, pid)],
            ..Default::default()
        };
        assert!(group_alive(pid), "a group with a live member");
        let clock = FakeClock::default();
        assert_eq!(
            side.alive_after_stop(&clock, GONE_WAIT),
            vec![(Service::Core, pid)],
            "a live group outlasts the wait"
        );
        assert!(clock.now() >= GONE_WAIT, "the wait ran its full limit");
        proc.kill_tree();
        assert!(!group_alive(pid), "a group whose only member is reaped");
        let clock = FakeClock::default();
        assert_eq!(side.alive_after_stop(&clock, GONE_WAIT), vec![]);
        assert_eq!(clock.now(), Duration::ZERO, "a gone group answers at once");
    }

    /// A context on a scratch directory, and a configuration CLIProxyAPI's stop never reads
    /// (it makes no HTTP call).
    #[cfg(unix)]
    fn stop_rig(dir: &Path) -> (Context, Config) {
        let cx = Context {
            dir: dir.to_path_buf(),
            log: AppLog::new(dir),
            http: reqwest::blocking::Client::new(),
            clock: RealClock(Instant::now()),
        };
        let conf = Config {
            core_port: 0,
            cliproxy_port: 0,
            operator_token: String::new(),
            management_secret_file: PathBuf::new(),
            panel: false,
        };
        (cx, conf)
    }

    #[cfg(unix)]
    #[test]
    fn stop_records_the_pid_it_took_and_nothing_for_a_service_without_one() {
        let dir = tempfile::tempdir().unwrap();
        let (cx, conf) = stop_rig(dir.path());
        let stop = |side: &mut Sidecars, service| side.stop(&cx, &conf, "", service);
        let stop_a_child = |side: &mut Sidecars| {
            let proc = sleeper();
            let pid = proc.pid();
            side.cliproxy.proc = Some(proc);
            stop(side, Service::Cliproxy);
            pid
        };
        let mut side = Sidecars::default();

        let first = stop_a_child(&mut side);
        assert_eq!(side.stopped_pids(), vec![(Service::Cliproxy, first)]);
        assert!(!group_alive(first), "the stop took the whole group");
        stop(&mut side, Service::Core);
        assert_eq!(
            side.stopped_pids(),
            vec![(Service::Cliproxy, first)],
            "the core had no child"
        );
        let second = stop_a_child(&mut side);
        assert_eq!(
            side.stopped_pids(),
            vec![(Service::Cliproxy, second)],
            "a second stop replaces the entry"
        );
        stop(&mut side, Service::Cliproxy);
        assert_eq!(
            side.stopped_pids(),
            vec![],
            "a stop with no child forgets the pid, which may be reused"
        );
    }

    #[cfg(unix)]
    #[test]
    fn the_sidecars_can_still_be_stopped_after_a_panic_under_their_lock() {
        let dir = tempfile::tempdir().unwrap();
        let (cx, conf) = stop_rig(dir.path());
        let side = Mutex::new(Sidecars::default());
        let proc = sleeper();
        let pid = proc.pid();
        Sidecars::lock(&side).cliproxy.proc = Some(proc);
        let _ = std::panic::catch_unwind(|| {
            let _held = side.lock().unwrap();
            panic!("a stop panicked");
        });
        assert!(side.is_poisoned());
        Sidecars::lock(&side).stop(&cx, &conf, "", Service::Cliproxy);
        assert_eq!(
            Sidecars::lock(&side).stopped_pids(),
            vec![(Service::Cliproxy, pid)]
        );
        assert!(!group_alive(pid), "the child the panic left was stopped");
    }
}
