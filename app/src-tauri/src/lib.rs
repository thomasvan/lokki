//! The shell: first run through the core, the two supervised sidecars, the window on the
//! core's page, the tray and the error page. Every decision is `lokki_logic`'s or the core's.
mod capability;
mod commands;
mod errorpage;
mod leftover;
mod process;
mod sidecars;
mod tray;
pub mod update;
mod window;

use errorpage::Failure;
use lokki_logic::firstrun::{self, Config};
use lokki_logic::tray::{view, writes, Mode};
use lokki_logic::{paths, sidecar_files, Result};
use sidecars::{AppLog, Context, CoreHttp, RealClock, Service, Sidecars, GONE_WAIT};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::thread;
use std::time::{Duration, Instant};
use tauri::{AppHandle, Manager, RunEvent, WindowEvent};
use update::{Machine, Os, PluginSource, Policy};

/// What the headless smoke runs in the page once it has loaded: four commands its grant
/// does not name (the error page's, a plugin's with no grant anywhere, and the updater
/// plugin's two), called without arguments so that one let through does nothing, each
/// reported as `refused` (Tauri's "not allowed", worded differently in debug and release
/// builds), `allowed` or `error`; the update state and an install, which has nothing to
/// install; then a CLIProxyAPI restart through the hook and an authenticated read.
const SMOKE_PAGE: &str = r#"(async () => {
  const invoke = window.__TAURI__.core.invoke;
  const probe = (c) => invoke(c).then(() => `${c}=allowed`,
    (e) => `${c}=${/not allowed/.test(String(e)) ? "refused" : "error"}`);
  const grant = (await Promise.all([probe("try_again"), probe("plugin:clipboard-manager|read_text"),
    probe("plugin:updater|check"), probe("plugin:updater|download_and_install")])).join(" ");
  const update = await window.lokki.updateStatus().then((s) => s.state, (e) => "rejected: " + e);
  const install = await window.lokki.installUpdate().then(() => "accepted", (e) => String(e));
  window.lokki.restart("cliproxy").then(() => fetch("/api/status")).then(
    (r) => invoke("smoke_done", {status: r.status, restart: "ok", grant, update, install}),
    (e) => invoke("smoke_done", {status: 0, restart: String(e), grant, update, install}));
})()"#;

/// How often the tray reads `/api/status`; a choice in the tray reads it at once.
const TRAY_REFRESH: Duration = Duration::from_secs(10);
const MONITOR_TICK: Duration = Duration::from_millis(250);
const INSTALLING: &str = "an update is being installed";

type Updater = Machine<PluginSource<tauri::Wry>, RealClock>;

pub struct Shell {
    app: AppHandle,
    cx: Context,
    smoke: bool,
    quitting: AtomicBool,
    /// Set from the stop of an install until its sidecars run again: nothing else starts one
    /// between the stop and the proof that both are gone.
    installing: AtomicBool,
    smoke_page_run: AtomicBool,
    conf: Mutex<Option<Config>>,
    secret: Mutex<Option<String>>,
    side: Mutex<Sidecars>,
    /// The core port the main window was made for.
    window_port: Mutex<Option<u16>>,
    /// The core ports granted so far: a grant cannot be added twice.
    granted: Mutex<std::collections::HashSet<u16>>,
    /// Set once, in `setup`, when the tray builds.
    tray: OnceLock<tray::Tray>,
    /// Set once, in `setup`.
    update: OnceLock<Updater>,
    /// The data directory's lock, taken by the first boot and held until the shell ends.
    lock: OnceLock<std::fs::File>,
}

impl Shell {
    fn side(&self) -> std::sync::MutexGuard<'_, Sidecars> {
        Sidecars::lock(&self.side)
    }

    fn conf(&self) -> Option<Config> {
        self.conf.lock().unwrap().clone()
    }

    fn secret(&self) -> Option<String> {
        self.secret.lock().unwrap().clone()
    }

    fn core_http(&self, port: u16) -> CoreHttp<'_> {
        CoreHttp {
            client: &self.cx.http,
            port,
        }
    }

    fn show_error(&self, failure: Failure) {
        self.cx
            .log
            .line(format_args!("error page: {}", failure.title));
        if let Err(e) = errorpage::show(&self.app, &failure) {
            self.cx
                .log
                .line(format_args!("the error page did not open: {e}"));
        }
    }

    fn log_tail(&self, service: Service) -> String {
        sidecars::tail(&service.log_path(&self.cx.dir), errorpage::TAIL_LINES)
    }

    /// First run, both sidecars, the window; a failure opens the error page.
    fn boot(self: &Arc<Self>) {
        if let Err(failure) = self.try_boot() {
            self.show_error(failure);
        }
    }

    fn try_boot(self: &Arc<Self>) -> std::result::Result<(), Failure> {
        let dir = &self.cx.dir;
        let conf = firstrun::ensure(dir, |args| sidecars::run_core(dir, args, &self.cx.log))
            .map_err(|e| Failure {
                title: "Lokki could not set up its data directory".into(),
                what: format!(
                    "The core's first-run step failed for {}. What it said:",
                    dir.display()
                ),
                name: "init".into(),
                tail: e,
            })?;
        *self.secret.lock().unwrap() = std::fs::read_to_string(&conf.management_secret_file)
            .ok()
            .map(|s| s.trim().to_string());
        if self.lock.get().is_none() {
            let file = leftover::lock(dir, &self.cx.clock).map_err(|e| {
                let (title, what) = match e {
                    leftover::NotLocked::Held(what) => ("Lokki is already running", what),
                    leftover::NotLocked::Failed(what) => {
                        ("Lokki could not lock its data directory", what)
                    }
                };
                Failure {
                    title: title.into(),
                    what,
                    name: "start".into(),
                    tail: String::new(),
                }
            })?;
            let _ = self.lock.set(file);
        }
        let mut side = self.side();
        if self.quitting.load(Ordering::SeqCst) || self.installing.load(Ordering::SeqCst) {
            return Ok(());
        }
        // A boot again after a failed one starts from nothing running.
        let stop_both = |side: &mut Sidecars| {
            for s in [Service::Core, Service::Cliproxy] {
                side.stop(&self.cx, &conf, &conf.operator_token, s);
            }
        };
        stop_both(&mut side);
        for line in leftover::stop(&self.cx, &conf) {
            self.cx.log.line(line);
        }
        if let Err((service, e)) = side.start_all(&self.cx, &conf) {
            stop_both(&mut side);
            return Err(Failure {
                title: "Lokki could not start its services".into(),
                what: format!("{e}. The last lines of logs/{}.log:", service.name()),
                name: "start".into(),
                tail: self.log_tail(service),
            });
        }
        *self.conf.lock().unwrap() = Some(conf.clone());
        if self.smoke {
            println!(
                "smoke: core={} cliproxy={}",
                conf.core_port, conf.cliproxy_port
            );
            println!("smoke: revision={}", self.revision(&conf));
        }
        self.report_started(&side, &conf);
        drop(side);
        self.open_window(&conf).map_err(|e| Failure {
            title: "Lokki could not open its window".into(),
            what: e,
            name: "start".into(),
            tail: String::new(),
        })?;
        errorpage::close(&self.app);
        self.refresh_tray();
        Ok(())
    }

    /// `/api/status.revision`, or `unknown`.
    fn revision(&self, conf: &Config) -> String {
        self.core_http(conf.core_port)
            .json(
                reqwest::Method::GET,
                "/api/status",
                &conf.operator_token,
                None,
            )
            .ok()
            .and_then(|s| s.get("revision")?.as_str().map(String::from))
            .unwrap_or_else(|| "unknown".into())
    }

    fn report_started(&self, side: &Sidecars, conf: &Config) {
        let pid = |p: Option<u32>| p.map_or_else(|| "none".to_string(), |p| p.to_string());
        let (core, cliproxy) = side.pids();
        self.cx.log.line(format_args!(
            "ready: core on port {} pid {}, cliproxy on port {} pid {}",
            conf.core_port,
            pid(core),
            conf.cliproxy_port,
            pid(cliproxy)
        ));
        if self.smoke {
            // Each start prints its pids, so the smoke checks every process it was shown.
            println!("smoke: pids={} {}", pid(core), pid(cliproxy));
        }
    }

    /// Grants the core's origin and opens the main window on it, unless one is open there.
    fn open_window(self: &Arc<Self>, conf: &Config) -> Result<()> {
        let mut window_port = self.window_port.lock().unwrap();
        if *window_port == Some(conf.core_port) {
            return Ok(());
        }
        if self.granted.lock().unwrap().insert(conf.core_port) {
            capability::grant_main(&self.app, conf.core_port, self.smoke)
                .map_err(|e| format!("the window's grant was refused: {e}"))?;
        }
        let shell = Arc::clone(self);
        window::open_main(&self.app, conf.core_port, &conf.operator_token, move |w| {
            if shell.smoke && !shell.smoke_page_run.swap(true, Ordering::SeqCst) {
                let _ = w.eval(SMOKE_PAGE);
            }
        })
        .map_err(|e| format!("the window did not open: {e}"))?;
        *window_port = Some(conf.core_port);
        Ok(())
    }

    /// `shell-config` again, keeping what was read before when it fails.
    fn reread_conf(&self, before: Config) -> Config {
        let conf = sidecars::shell_config(&self.cx.dir, &self.cx.log).unwrap_or_else(|e| {
            self.cx.log.line(format_args!(
                "shell-config failed, keeping the ports read before: {e}"
            ));
            before
        });
        *self.conf.lock().unwrap() = Some(conf.clone());
        conf
    }

    /// The core is drained and stopped and started again. `Cliproxy` also stops CLIProxyAPI
    /// after the core and starts it before, since the core reads CLIProxyAPI's address only
    /// at start. Returns once what was started is ready.
    fn restart(self: &Arc<Self>, service: Service) -> Result<()> {
        let mut side = self.side();
        if self.quitting.load(Ordering::SeqCst) {
            return Err("Lokki is quitting".into());
        }
        if self.installing.load(Ordering::SeqCst) {
            return Err(INSTALLING.into());
        }
        let conf = self.conf().ok_or("Lokki has not started its services")?;
        self.cx
            .log
            .line(format_args!("restart of {} asked", service.name()));
        side.stop(&self.cx, &conf, &conf.operator_token, Service::Core);
        if service == Service::Cliproxy {
            side.stop(&self.cx, &conf, &conf.operator_token, Service::Cliproxy);
        }
        let conf = self.reread_conf(conf);
        let cliproxy = match service {
            Service::Cliproxy => side.start(&self.cx, &conf, Service::Cliproxy, true),
            Service::Core => Ok(()),
        };
        // The core starts even when CLIProxyAPI did not: it serves the UI that says so.
        let started = cliproxy.and(side.start(&self.cx, &conf, Service::Core, true));
        if started.is_ok() {
            self.report_started(&side, &conf);
        }
        drop(side);
        started?;
        self.open_window(&conf)
    }

    /// The error page's button: a sidecar that gave up gets one start more, with its count
    /// of exits forgotten; a failed first run or start runs it all again.
    fn try_again(self: &Arc<Self>, name: &str) -> Result<()> {
        if self.installing.load(Ordering::SeqCst) {
            return Err(INSTALLING.into());
        }
        let Some(service) = Service::parse(name) else {
            self.boot();
            return Ok(());
        };
        let mut side = self.side();
        if self.quitting.load(Ordering::SeqCst) {
            return Err("Lokki is quitting".into());
        }
        if self.installing.load(Ordering::SeqCst) {
            return Err(INSTALLING.into());
        }
        let conf = self.conf().ok_or("Lokki has not started its services")?;
        side.forgive(service);
        let conf = self.reread_conf(conf);
        let started = side.start(&self.cx, &conf, service, true);
        if started.is_ok() {
            self.report_started(&side, &conf);
        }
        drop(side);
        match started {
            Ok(()) => {
                errorpage::close(&self.app);
                window::show_main(&self.app);
                Ok(())
            }
            Err(e) => {
                self.show_error(gave_up_page(service, &e, self.log_tail(service)));
                Err(e)
            }
        }
    }

    /// The monitor: exits go to the supervisors, restarts run when due, a give-up opens
    /// the error page, and a termination signal quits.
    fn monitor(self: &Arc<Self>) {
        while !self.quitting.load(Ordering::SeqCst) {
            thread::sleep(MONITOR_TICK);
            if TERMINATE.load(Ordering::SeqCst) {
                self.quit();
                break;
            }
            let Some(conf) = self.conf() else { continue };
            let gave_up = {
                let mut side = self.side();
                if self.quitting.load(Ordering::SeqCst) {
                    break;
                }
                side.poll(&self.cx, &conf)
            };
            for (service, sentence) in gave_up {
                self.show_error(gave_up_page(service, &sentence, self.log_tail(service)));
            }
        }
    }

    /// The tray follows `/api/status`: the decision, its reason, the checked mode and the
    /// icon. Runs off the event loop, which `tray::bar` and every menu change wait on.
    fn refresh_tray(&self) {
        let status = self.conf().and_then(|conf| self.status(&conf));
        self.show_tray(status.as_ref());
    }

    fn status(&self, conf: &Config) -> Option<serde_json::Value> {
        self.core_http(conf.core_port)
            .json(
                reqwest::Method::GET,
                "/api/status",
                &conf.operator_token,
                None,
            )
            .ok()
    }

    /// The tray as `status` says, or as for a core that does not answer.
    fn show_tray(&self, status: Option<&serde_json::Value>) {
        let Some(tray) = self.tray.get() else { return };
        let (ink, px) = tray::bar(&self.app);
        if let Err(e) = tray.apply(&view(status), ink, px) {
            self.cx
                .log
                .line(format_args!("the tray did not update: {e}"));
        }
    }

    /// A mode chosen in the tray is written unless the core already reports it. The click's
    /// own toggle of its check never stands: the marks go back as shown before the core is
    /// read, and the tray then shows that read, or after a write reads the core again.
    fn choose(&self, mode: Mode) {
        if let Some(Err(e)) = self.tray.get().map(tray::Tray::recheck) {
            self.cx
                .log
                .line(format_args!("the tray did not update: {e}"));
        }
        let Some(conf) = self.conf() else {
            return self.show_tray(None);
        };
        let status = self.status(&conf);
        if !writes(&view(status.as_ref()), mode) {
            // One read, so a core that does not answer costs one timeout, not two.
            return self.show_tray(status.as_ref());
        }
        match self.core_http(conf.core_port).json(
            reqwest::Method::PUT,
            "/api/settings",
            &conf.operator_token,
            Some(&mode.body()),
        ) {
            Ok(_) => self
                .cx
                .log
                .line(format_args!("routing set to {} from the tray", mode.id())),
            Err(e) => self
                .cx
                .log
                .line(format_args!("the tray's mode choice failed: {e}")),
        }
        self.refresh_tray();
    }

    /// Both sidecars stopped, the core drained first. Idempotent.
    fn stop_all(&self) {
        let mut side = self.side();
        if let Some(conf) = self.conf() {
            for service in [Service::Core, Service::Cliproxy] {
                side.stop(&self.cx, &conf, &conf.operator_token, service);
            }
        }
    }

    fn update(&self) -> &Updater {
        self.update.get().expect("setup made the updater")
    }

    fn update_status(&self) -> update::Status {
        self.update().status()
    }

    fn check_for_update(&self) -> update::Status {
        self.update().check()
    }

    /// Download and verify, stop both sidecars and prove them gone, install, restart. A
    /// failure after the stop starts the sidecars again and is reported.
    fn install_update(self: &Arc<Self>) -> Result<()> {
        let stop = || {
            self.installing.store(true, Ordering::SeqCst);
            self.stop_all();
            let alive = self.side().alive_after_stop(&self.cx.clock, GONE_WAIT);
            if alive.is_empty() {
                Ok(())
            } else {
                Err(alive
                    .iter()
                    .map(|(s, pid)| {
                        format!(
                            "{} (pid {pid}) is still alive after the stop sequence",
                            s.title()
                        )
                    })
                    .collect::<Vec<_>>()
                    .join("; "))
            }
        };
        let resume = || {
            // Cleared however this ends, so a failed resume cannot leave the commands refused.
            struct Done<'a>(&'a AtomicBool);
            impl Drop for Done<'_> {
                fn drop(&mut self) {
                    self.0.store(false, Ordering::SeqCst);
                }
            }
            let _done = Done(&self.installing);
            if let Some(conf) = self.conf() {
                let mut side = self.side();
                match side.start_all(&self.cx, &conf) {
                    Ok(()) => self.report_started(&side, &conf),
                    Err((service, e)) => self.cx.log.line(format_args!(
                        "after the failed install, {} did not start: {e}",
                        service.name()
                    )),
                }
            }
        };
        self.cx.log.line("update install asked");
        if let Err(e) = self.update().install(stop, resume) {
            self.cx.log.line(format_args!("update not installed: {e}"));
            return Err(e);
        }
        self.cx.log.line("update installed; restarting");
        self.app.restart()
    }

    /// The stop sequence, then the exit. Runs once.
    fn quit(&self) {
        if self.quitting.swap(true, Ordering::SeqCst) {
            return;
        }
        self.cx.log.line("quitting");
        self.stop_all();
        self.app.exit(0);
    }
}

/// Set by SIGTERM, SIGINT or SIGHUP. The sidecars run in process groups of their own, so a
/// signal to the shell reaches neither: the monitor turns it into the stop sequence.
static TERMINATE: AtomicBool = AtomicBool::new(false);

#[cfg(unix)]
fn on_termination_signals() {
    extern "C" fn flag(_: libc::c_int) {
        TERMINATE.store(true, Ordering::SeqCst);
    }
    for signal in [libc::SIGTERM, libc::SIGINT, libc::SIGHUP] {
        unsafe {
            libc::signal(signal, flag as *const () as libc::sighandler_t);
        }
    }
}

fn gave_up_page(service: Service, sentence: &str, tail: String) -> Failure {
    Failure {
        title: format!("Lokki stopped {}", service.title()),
        what: format!("{sentence} The last lines of logs/{}.log:", service.name()),
        name: service.name().into(),
        tail,
    }
}

pub fn run() {
    // Absolute: the core's subcommands run with the data directory as their working directory.
    let dir: PathBuf = paths::data_dir();
    let dir = std::path::absolute(&dir).unwrap_or(dir);
    let smoke = std::env::var_os("LOKKI_SMOKE").is_some();
    // Built here, off the async runtime; loopback only, so no proxy applies.
    let http = reqwest::blocking::Client::builder()
        .no_proxy()
        .build()
        .expect("the loopback HTTP client builds");
    let log = AppLog::new(&dir);
    if let Ok(exe_dir) = sidecars::exe_dir() {
        let keep = sidecars::sidecar_file_names();
        for removed in sidecar_files::prune(&exe_dir, &[&keep[0], &keep[1]]) {
            log.line(format_args!(
                "removed an old sidecar file: {}",
                removed.display()
            ));
        }
    }
    let cx = Context {
        dir,
        log,
        http,
        clock: RealClock(Instant::now()),
    };
    let mut cx = Some(cx);
    #[cfg(unix)]
    on_termination_signals();

    let app = tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_clipboard_manager::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .invoke_handler(tauri::generate_handler![
            commands::restart_service,
            commands::open_external,
            commands::open_panel,
            commands::copy_management_secret,
            commands::try_again,
            commands::smoke_done,
            commands::update_status,
            commands::check_for_update,
            commands::install_update,
        ])
        .setup(move |app| {
            let shell = Arc::new(Shell {
                app: app.handle().clone(),
                cx: cx.take().expect("setup runs once"),
                smoke,
                quitting: AtomicBool::new(false),
                installing: AtomicBool::new(false),
                smoke_page_run: AtomicBool::new(false),
                conf: Mutex::new(None),
                secret: Mutex::new(None),
                side: Mutex::new(Sidecars::default()),
                window_port: Mutex::new(None),
                granted: Mutex::new(Default::default()),
                tray: OnceLock::new(),
                update: OnceLock::new(),
                lock: OnceLock::new(),
            });
            let _ = shell.update.set(Machine::new(
                PluginSource::new(
                    app.handle().clone(),
                    std::env::var("LOKKI_UPDATE_ENDPOINT").ok(),
                ),
                RealClock(Instant::now()),
                // The `version` the build's config override set from core/VERSION.
                &app.package_info().version.to_string(),
                Box::new(|| {
                    // Only macOS decides from the bundle it runs from (spec 7.4).
                    #[cfg(target_os = "macos")]
                    let mac = update::mac_bundle(&std::env::current_exe().unwrap_or_default());
                    #[cfg(not(target_os = "macos"))]
                    let mac = Default::default();
                    Policy::detect(Os::current(), |n| std::env::var(n).ok(), &mac)
                }),
            ));
            app.manage(Arc::clone(&shell));
            capability::grant_error(app.handle())?;
            match tray::build(app.handle()) {
                Ok(tray) => {
                    let _ = shell.tray.set(tray);
                }
                Err(e) => shell.cx.log.line(format_args!("no tray: {e}")),
            }
            let s = Arc::clone(&shell);
            thread::spawn(move || s.monitor());
            let s = Arc::clone(&shell);
            thread::spawn(move || loop {
                thread::sleep(TRAY_REFRESH);
                s.refresh_tray();
            });
            // The smoke makes no network call, so it starts no check.
            if !smoke {
                let s = Arc::clone(&shell);
                thread::spawn(move || {
                    while !s.quitting.load(Ordering::SeqCst) {
                        thread::sleep(MONITOR_TICK * 4);
                        if s.update().due() {
                            s.update().check();
                        }
                    }
                });
            }
            thread::spawn(move || shell.boot());
            Ok(())
        })
        .on_window_event(|window, event| {
            if !window::is_main(window.label()) {
                return;
            }
            if let WindowEvent::CloseRequested { api, .. } = event {
                let shell = Arc::clone(&window.state::<Arc<Shell>>());
                api.prevent_close();
                // With a tray the window hides and the tray brings it back; without one,
                // closing it is quitting.
                if shell.tray.get().is_some() {
                    let _ = window.hide();
                } else {
                    thread::spawn(move || shell.quit());
                }
            }
        })
        .build(tauri::generate_context!())
        .expect("lokki could not start its window system");

    app.run(|app, event| match event {
        // The last window closed (the error page, with no main window behind it).
        RunEvent::ExitRequested {
            code: None, api, ..
        } => {
            api.prevent_exit();
            let shell = Arc::clone(&app.state::<Arc<Shell>>());
            thread::spawn(move || shell.quit());
        }
        // However the app ends, no sidecar outlives it.
        RunEvent::Exit => app.state::<Arc<Shell>>().stop_all(),
        _ => {}
    });
}

/// Test doubles shared by the modules' tests.
#[cfg(test)]
pub(crate) mod testing {
    use lokki_logic::supervise::Clock;
    use std::cell::Cell;
    use std::time::Duration;

    /// A clock that moves only when something sleeps.
    #[derive(Default)]
    pub(crate) struct FakeClock(Cell<Duration>);

    impl Clock for FakeClock {
        fn now(&self) -> Duration {
            self.0.get()
        }
        fn sleep(&self, d: Duration) {
            self.0.set(self.0.get() + d);
        }
    }
}
