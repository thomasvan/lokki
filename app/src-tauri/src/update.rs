//! The updater's state and the order of an install (spec 7): one state the page reads,
//! a check that never overlaps, an install that verifies, stops, proves, installs.
use lokki_logic::mac::MacBundle;
use lokki_logic::supervise::Clock;
use serde::Serialize;
#[cfg(unix)]
use std::path::Path;
use std::sync::Mutex;
use std::time::Duration;

pub const FIRST_CHECK: Duration = Duration::from_secs(60);
pub const DAILY: Duration = Duration::from_secs(24 * 3600);
pub const NO_PLATFORM: &str = "no build for this platform in the latest release";
/// How long the endpoint has to answer a check.
const CHECK_WAIT: Duration = Duration::from_secs(30);
/// How long a package has to download, body included.
const DOWNLOAD_WAIT: Duration = Duration::from_secs(15 * 60);

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Found {
    pub version: String,
    pub notes_url: String,
}

pub trait Source {
    /// What `download` verified; only `install` takes it.
    type Package;
    fn check(&self) -> Result<Option<Found>, String>;
    fn download(&self, found: &Found) -> Result<Self::Package, String>;
    fn install(&self, package: Self::Package) -> Result<(), String>;
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Os {
    Linux,
    Macos,
    Windows,
}

impl Os {
    pub fn current() -> Self {
        if cfg!(target_os = "windows") {
            Os::Windows
        } else if cfg!(target_os = "macos") {
            Os::Macos
        } else {
            Os::Linux
        }
    }
}

#[derive(Clone, Debug)]
pub struct Policy {
    pub can_install: bool,
    pub reason: Option<String>,
}

/// Computed at start, again when an install is asked, and again after the stop, so a
/// bundle that became unreplaceable meanwhile is refused before `install` (decision 15).
pub type PolicyOf = Box<dyn Fn() -> Policy + Send + Sync>;

impl Policy {
    /// Linux installs only from an AppImage; Windows always; macOS from a bundle the user can
    /// replace (spec 7.4), so the plugin never reaches its administrator prompt for one they
    /// cannot.
    pub fn detect(os: Os, env: impl Fn(&str) -> Option<String>, mac: &MacBundle) -> Self {
        let no = |reason: &str| Policy {
            can_install: false,
            reason: Some(reason.into()),
        };
        let yes = Policy {
            can_install: true,
            reason: None,
        };
        match os {
            Os::Windows => yes,
            Os::Linux if env("APPIMAGE").is_some() => yes,
            Os::Linux => no("this lokki is not an AppImage; the updater replaces AppImages only, so download the new package"),
            Os::Macos if mac.app.is_none() => no("this Lokki is not running from lokki.app, so it cannot replace itself; download the new .dmg"),
            Os::Macos if mac.translocated => no("macOS is running Lokki from a temporary copy, which cannot be updated: quit Lokki, move lokki.app into Applications with Finder, open it from there, then install"),
            Os::Macos if mac.read_only => no("Lokki is running from a read-only disk, such as its disk image: move lokki.app into Applications, open it from there, then install"),
            Os::Macos if !mac.writable => no("your macOS user cannot change lokki.app or the folder it is in, and Lokki does not ask for an administrator's password; download the new .dmg, or move lokki.app to a folder you can write to"),
            Os::Macos => yes,
        }
    }
}

/// The bundle the running executable is in. Read on every unix, so it is compiled and checked
/// off a Mac; only macOS's policy reads it.
#[cfg(unix)]
pub fn mac_bundle(exe: &Path) -> MacBundle {
    use lokki_logic::mac::{app_of, translocated};
    let app = app_of(exe);
    let (read_only, writable) = app.as_deref().map_or((false, false), probe);
    MacBundle {
        translocated: app.as_deref().is_some_and(translocated),
        app,
        read_only,
        writable,
    }
}

/// `statvfs`'s flags say read-only: `ST_RDONLY`, the POSIX form of `statfs`'s `MNT_RDONLY`.
#[cfg(unix)]
pub fn read_only_from(f_flag: libc::c_ulong) -> bool {
    f_flag & libc::ST_RDONLY != 0
}

/// (read-only, writable) for `app`: its file system's `ST_RDONLY` flag, and `access(W_OK)` on
/// it and on its parent, since a directory renamed to another parent needs write on both.
/// `access` answers for the real user, as the rename will run. A read that fails is not
/// read-only and not writable.
#[cfg(unix)]
pub fn probe(app: &Path) -> (bool, bool) {
    use std::ffi::CString;
    use std::os::unix::ffi::OsStrExt;
    let c = |p: &Path| CString::new(p.as_os_str().as_bytes()).ok();
    let writable = |p: Option<&Path>| {
        // SAFETY: a NUL-terminated path that outlives the call.
        p.and_then(c)
            .is_some_and(|p| unsafe { libc::access(p.as_ptr(), libc::W_OK) } == 0)
    };
    let read_only = c(app).is_some_and(|p| {
        let mut fs = std::mem::MaybeUninit::<libc::statvfs>::uninit();
        // SAFETY: `fs` is read only after `statvfs` answered 0, which filled it.
        unsafe {
            libc::statvfs(p.as_ptr(), fs.as_mut_ptr()) == 0
                && read_only_from(fs.assume_init().f_flag)
        }
    });
    (read_only, writable(Some(app)) && writable(app.parent()))
}

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
pub struct Status {
    pub state: String,
    pub current_version: String,
    pub version: Option<String>,
    pub notes_url: Option<String>,
    pub can_install: bool,
    pub reason: Option<String>,
    pub error: Option<String>,
}

struct Inner {
    state: &'static str,
    /// The last policy computed.
    policy: Policy,
    found: Option<Found>,
    error: Option<String>,
    reason: Option<String>,
    checked_at: Option<Duration>,
    checking: bool,
    installing: bool,
}

pub struct Machine<S: Source, C: Clock> {
    pub source: S,
    pub clock: C,
    current: String,
    policy_of: PolicyOf,
    started: Duration,
    inner: Mutex<Inner>,
}

impl<S: Source, C: Clock> Machine<S, C> {
    pub fn new(source: S, clock: C, current: &str, policy_of: PolicyOf) -> Self {
        let started = clock.now();
        let policy = policy_of();
        Machine {
            source,
            clock,
            current: current.into(),
            policy_of,
            started,
            inner: Mutex::new(Inner {
                state: "idle",
                policy,
                found: None,
                error: None,
                reason: None,
                checked_at: None,
                checking: false,
                installing: false,
            }),
        }
    }

    pub fn status(&self) -> Status {
        let i = self.inner.lock().unwrap();
        Status {
            state: i.state.into(),
            current_version: self.current.clone(),
            version: i.found.as_ref().map(|f| f.version.clone()),
            notes_url: i.found.as_ref().map(|f| f.notes_url.clone()),
            can_install: i.policy.can_install,
            reason: i.reason.clone().or_else(|| i.policy.reason.clone()),
            error: i.error.clone(),
        }
    }

    /// A check is due a minute after boot and then a day after the last one.
    pub fn due(&self) -> bool {
        let now = self.clock.now();
        let i = self.inner.lock().unwrap();
        match i.checked_at {
            None => now.saturating_sub(self.started) >= FIRST_CHECK,
            Some(at) => now.saturating_sub(at) >= DAILY,
        }
    }

    pub fn check(&self) -> Status {
        let busy = {
            let mut i = self.inner.lock().unwrap();
            let busy = i.checking || i.installing;
            if !busy {
                i.checking = true;
                i.state = "checking";
            }
            busy
        };
        if busy {
            return self.status();
        }
        let answer = caught("the update check", || self.source.check());
        {
            let mut i = self.inner.lock().unwrap();
            i.checking = false;
            i.checked_at = Some(self.clock.now());
            i.error = None;
            i.reason = None;
            match answer {
                Ok(Some(found)) => {
                    i.found = Some(found);
                    i.state = "available";
                }
                Ok(None) => {
                    i.found = None;
                    i.state = "up_to_date";
                }
                Err(e) if e == NO_PLATFORM => {
                    i.found = None;
                    i.state = "up_to_date";
                    i.reason = Some(e);
                }
                Err(e) => {
                    i.state = "error";
                    i.error = Some(e);
                }
            }
        }
        self.status()
    }

    /// Download and verify, then `stop` (the shell's stop sequence, which proves both
    /// sidecars gone or answers why not), then install. The policy is computed again before the
    /// download and again after the stop, so a bundle that cannot be replaced now is refused
    /// before `install`. After a failure past the stop, `resume` starts the sidecars again. Any
    /// failure after the download starts leaves `error` with the sentence; a refusal changes
    /// nothing but the policy the status reports.
    pub fn install(
        &self,
        stop: impl FnOnce() -> Result<(), String>,
        resume: impl FnOnce(),
    ) -> Result<(), String> {
        let found = {
            let mut i = self.inner.lock().unwrap();
            if i.installing {
                return Err("an install is already running".into());
            }
            let found = i
                .found
                .clone()
                .ok_or_else(|| "no update is available to install".to_string())?;
            i.policy = caught("the install check", || Ok((self.policy_of)()))?;
            refusal(&i.policy)?;
            i.installing = true;
            i.state = "downloading";
            found
        };
        // After a failure past the stop: the sidecars start again, and a resume that fails
        // joins the sentence, so the shell's log and the page both say so.
        let resumed = |e: String| match caught("starting the sidecars again", || {
            resume();
            Ok(())
        }) {
            Ok(()) => e,
            Err(also) => format!("{e}; {also}"),
        };
        let result = match caught("the download", || self.source.download(&found)) {
            Err(e) => Err(e),
            Ok(package) => match caught("the stop sequence", stop) {
                Err(e) => Err(resumed(e)),
                Ok(()) => {
                    let checked = caught("the install check", || {
                        let policy = (self.policy_of)();
                        let refused = refusal(&policy);
                        self.inner.lock().unwrap().policy = policy;
                        refused
                    });
                    match checked {
                        Err(e) => Err(resumed(e)),
                        Ok(()) => {
                            self.set("installing");
                            caught("the install", || self.source.install(package)).map_err(resumed)
                        }
                    }
                }
            },
        };
        let mut i = self.inner.lock().unwrap();
        if let Err(e) = &result {
            i.installing = false;
            i.state = "error";
            i.error = Some(e.clone());
        }
        result
    }

    fn set(&self, state: &'static str) {
        self.inner.lock().unwrap().state = state;
    }

    #[cfg(test)]
    fn begin_check_for_test(&self) {
        let mut i = self.inner.lock().unwrap();
        i.checking = true;
        i.state = "checking";
    }

    #[cfg(test)]
    fn begin_install_for_test(&self) {
        let mut i = self.inner.lock().unwrap();
        i.installing = true;
        i.state = "installing";
    }
}

fn refusal(policy: &Policy) -> Result<(), String> {
    if policy.can_install {
        Ok(())
    } else {
        Err(policy
            .reason
            .clone()
            .unwrap_or_else(|| "installing is off".into()))
    }
}

/// Runs one step of a check or an install with a panic turned into its error, so a failing
/// step never leaves `checking` or `installing` set, the updater thread gone, or the
/// sidecars stopped without `resume`.
fn caught<T>(what: &str, step: impl FnOnce() -> Result<T, String>) -> Result<T, String> {
    std::panic::catch_unwind(std::panic::AssertUnwindSafe(step)).unwrap_or_else(|panic| {
        let why = panic
            .downcast_ref::<&str>()
            .map(|s| s.to_string())
            .or_else(|| panic.downcast_ref::<String>().cloned())
            .unwrap_or_else(|| "a panic".into());
        Err(format!("{what} failed: {why}"))
    })
}

/// The plugin, from Rust only. The endpoint list is `LOKKI_UPDATE_ENDPOINT` when set, else
/// the configured one; the public key is always the configured one. Generic over the
/// runtime: the app runs on `Wry`, the loopback test on `tauri::test::MockRuntime`.
pub struct PluginSource<R: tauri::Runtime> {
    app: tauri::AppHandle<R>,
    endpoint: Result<Option<tauri::Url>, String>,
}

impl<R: tauri::Runtime> PluginSource<R> {
    pub fn new(app: tauri::AppHandle<R>, endpoint_override: Option<String>) -> Self {
        let endpoint = endpoint_override
            .map(|s| {
                s.parse::<tauri::Url>()
                    .map_err(|e| format!("LOKKI_UPDATE_ENDPOINT is not a URL: {e}"))
            })
            .transpose();
        PluginSource { app, endpoint }
    }

    fn updater(&self) -> Result<tauri_plugin_updater::Updater, String> {
        use tauri_plugin_updater::UpdaterExt;
        let mut b = self.app.updater_builder().timeout(CHECK_WAIT);
        if let Some(url) = self.endpoint.clone()? {
            // A loopback endpoint (a hand test, the test below) never goes through a proxy.
            if matches!(url.host_str(), Some("127.0.0.1" | "localhost" | "[::1]")) {
                b = b.no_proxy();
            }
            b = b
                .endpoints(vec![url])
                .map_err(|e| format!("the update endpoint is refused: {e}"))?;
        }
        b.build()
            .map_err(|e| format!("the updater could not start: {e}"))
    }

    fn check_raw(&self) -> Result<Option<tauri_plugin_updater::Update>, String> {
        let updater = self.updater()?;
        match tauri::async_runtime::block_on(updater.check()) {
            Ok(u) => Ok(u),
            Err(e) if is_no_platform(&e) => Err(NO_PLATFORM.into()),
            Err(e) => Err(format!("the update check failed: {e}")),
        }
    }
}

/// The plugin's two answers for a manifest with no build for this platform.
pub fn is_no_platform(e: &tauri_plugin_updater::Error) -> bool {
    use tauri_plugin_updater::Error;
    matches!(e, Error::TargetNotFound(_) | Error::TargetsNotFound(_))
}

impl<R: tauri::Runtime> Source for PluginSource<R> {
    type Package = (tauri_plugin_updater::Update, Vec<u8>);

    fn check(&self) -> Result<Option<Found>, String> {
        Ok(self.check_raw()?.map(|u| Found {
            notes_url: format!(
                "https://github.com/thomasvan/lokki/releases/tag/v{}",
                u.version
            ),
            version: u.version,
        }))
    }

    /// Checks again, so the package is the one the endpoint offers now, and downloads it;
    /// the plugin verifies the signature before it returns the bytes.
    fn download(&self, found: &Found) -> Result<Self::Package, String> {
        let mut update = self
            .check_raw()?
            .filter(|u| u.version == found.version)
            .ok_or_else(|| "the release changed since the check; check again".to_string())?;
        // The builder's timeout bounds the check only; the update it answers carries none.
        update.timeout = Some(DOWNLOAD_WAIT);
        let bytes = tauri::async_runtime::block_on(update.download(|_, _| {}, || {}))
            .map_err(|e| format!("the download or its signature failed: {e}"))?;
        Ok((update, bytes))
    }

    fn install(&self, (update, bytes): Self::Package) -> Result<(), String> {
        update
            .install(bytes)
            .map_err(|e| format!("the install failed: {e}"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testing::FakeClock;
    use std::cell::RefCell;
    use std::rc::Rc;

    /// Every call the fake and the closures make, in order.
    type Log = Rc<RefCell<Vec<&'static str>>>;

    struct FakeSource {
        log: Log,
        check: RefCell<Vec<Result<Option<Found>, String>>>,
        download: RefCell<Result<(), String>>,
        install: RefCell<Result<(), String>>,
        /// The one step that panics, once.
        panics_in: std::cell::Cell<Option<&'static str>>,
    }
    impl FakeSource {
        fn new(log: &Log) -> Self {
            FakeSource {
                log: Rc::clone(log),
                check: RefCell::new(Vec::new()),
                download: RefCell::new(Ok(())),
                install: RefCell::new(Ok(())),
                panics_in: Default::default(),
            }
        }
        fn step(&self, name: &'static str) {
            self.log.borrow_mut().push(name);
            if self.panics_in.get() == Some(name) {
                self.panics_in.set(None);
                panic!("{name} panicked");
            }
        }
        fn answers(self, answer: Result<Option<Found>, String>) -> Self {
            self.check.borrow_mut().push(answer);
            self
        }
    }
    impl Source for FakeSource {
        type Package = ();
        fn check(&self) -> Result<Option<Found>, String> {
            self.step("check");
            self.check.borrow_mut().remove(0)
        }
        fn download(&self, _: &Found) -> Result<(), String> {
            self.step("download");
            self.download.borrow().clone()
        }
        fn install(&self, _: ()) -> Result<(), String> {
            self.step("install");
            self.install.borrow().clone()
        }
    }
    fn found(v: &str) -> Found {
        Found {
            version: v.into(),
            notes_url: format!("https://github.com/thomasvan/lokki/releases/tag/v{v}"),
        }
    }
    fn installs() -> Policy {
        Policy {
            can_install: true,
            reason: None,
        }
    }
    fn downloads_only() -> Policy {
        Policy {
            can_install: false,
            reason: Some("this Lokki is not an AppImage".into()),
        }
    }
    fn machine(source: FakeSource, policy: Policy) -> Machine<FakeSource, FakeClock> {
        Machine::new(
            source,
            FakeClock::default(),
            "0.1.0",
            Box::new(move || policy.clone()),
        )
    }
    /// A policy that answers each of `answers` in turn, one per computation.
    fn turning(answers: Vec<Policy>) -> PolicyOf {
        let answers = Mutex::new(answers.into_iter());
        Box::new(move || answers.lock().unwrap().next().expect("one more policy"))
    }
    fn stop_ok(log: &Log) -> impl FnOnce() -> Result<(), String> {
        let log = Rc::clone(log);
        move || {
            log.borrow_mut().push("stop");
            Ok(())
        }
    }
    fn resume(log: &Log) -> impl FnOnce() {
        let log = Rc::clone(log);
        move || log.borrow_mut().push("resume")
    }

    #[test]
    fn a_check_reports_available_up_to_date_or_the_error() {
        // (label, the source's answer, state, version, error contains)
        let rows: Vec<(
            &str,
            Result<Option<Found>, String>,
            &str,
            Option<&str>,
            Option<&str>,
        )> = vec![
            (
                "a newer release",
                Ok(Some(found("0.2.0"))),
                "available",
                Some("0.2.0"),
                None,
            ),
            ("nothing newer", Ok(None), "up_to_date", None, None),
            (
                "the endpoint failed",
                Err("the update check failed: 404".into()),
                "error",
                None,
                Some("404"),
            ),
        ];
        for (label, answer, state, version, error) in rows {
            let log = Log::default();
            let m = machine(FakeSource::new(&log).answers(answer), installs());
            let status = m.check();
            assert_eq!(status.state, state, "{label}");
            assert_eq!(status.version.as_deref(), version, "{label}");
            assert_eq!(status.error.is_some(), error.is_some(), "{label}");
            if let Some(e) = error {
                assert!(status.error.unwrap().contains(e), "{label}");
            }
            assert_eq!(status.current_version, "0.1.0");
            assert_eq!(status.notes_url.is_some(), version.is_some(), "{label}");
        }
    }

    #[test]
    fn a_manifest_without_this_platform_is_up_to_date_with_a_reason() {
        let log = Log::default();
        let m = machine(
            FakeSource::new(&log)
                .answers(Err(NO_PLATFORM.into()))
                .answers(Ok(None)),
            installs(),
        );
        let status = m.check();
        assert_eq!(status.state, "up_to_date");
        assert_eq!(status.reason.as_deref(), Some(NO_PLATFORM));
        assert!(status.error.is_none());
        assert!(
            m.check().reason.is_none(),
            "the next check clears the reason"
        );
    }

    #[test]
    fn the_policy_decides_can_install() {
        // The reasons a user reads, word for word (spec 7.4).
        const NO_APP: &str = "this Lokki is not running from lokki.app, so it cannot replace itself; download the new .dmg";
        const TRANSLOCATED: &str = "macOS is running Lokki from a temporary copy, which cannot be updated: quit Lokki, move lokki.app into Applications with Finder, open it from there, then install";
        const READ_ONLY: &str = "Lokki is running from a read-only disk, such as its disk image: move lokki.app into Applications, open it from there, then install";
        const NOT_WRITABLE: &str = "your macOS user cannot change lokki.app or the folder it is in, and Lokki does not ask for an administrator's password; download the new .dmg, or move lokki.app to a folder you can write to";
        let app = Some(std::path::PathBuf::from("/Applications/lokki.app"));
        let writable = MacBundle {
            app: app.clone(),
            writable: true,
            ..MacBundle::default()
        };
        let no_app = MacBundle::default();
        // (label, os, APPIMAGE set, the bundle, can_install, reason)
        let rows = [
            (
                "an AppImage on Linux",
                Os::Linux,
                true,
                no_app.clone(),
                true,
                None,
            ),
            (
                "not an AppImage on Linux",
                Os::Linux,
                false,
                writable.clone(),
                false,
                Some("this Lokki is not an AppImage; the updater replaces AppImages only, so download the new package"),
            ),
            ("Windows", Os::Windows, false, no_app.clone(), true, None),
            ("macOS, no app", Os::Macos, false, no_app.clone(), false, Some(NO_APP)),
            (
                "macOS, translocated",
                Os::Macos,
                false,
                MacBundle {
                    translocated: true,
                    ..writable.clone()
                },
                false,
                Some(TRANSLOCATED),
            ),
            (
                "macOS, read-only",
                Os::Macos,
                false,
                MacBundle {
                    read_only: true,
                    ..writable.clone()
                },
                false,
                Some(READ_ONLY),
            ),
            (
                "macOS, not writable",
                Os::Macos,
                false,
                MacBundle {
                    writable: false,
                    ..writable.clone()
                },
                false,
                Some(NOT_WRITABLE),
            ),
            ("macOS, writable", Os::Macos, false, writable.clone(), true, None),
            (
                "macOS, translocated and read-only",
                Os::Macos,
                false,
                MacBundle {
                    translocated: true,
                    read_only: true,
                    writable: false,
                    ..writable.clone()
                },
                false,
                Some(TRANSLOCATED),
            ),
            (
                "macOS, read-only and not writable",
                Os::Macos,
                false,
                MacBundle {
                    read_only: true,
                    writable: false,
                    ..writable.clone()
                },
                false,
                Some(READ_ONLY),
            ),
        ];
        for (label, os, appimage, mac, want, reason) in rows {
            let env = |name: &str| {
                (appimage && name == "APPIMAGE").then(|| "/opt/lokki.AppImage".to_string())
            };
            let p = Policy::detect(os, env, &mac);
            assert_eq!(p.can_install, want, "{label}");
            assert_eq!(p.reason.as_deref(), reason, "{label}");
        }
    }

    #[cfg(unix)]
    #[test]
    fn read_only_from_reads_the_read_only_bit() {
        let other = libc::ST_NOSUID;
        assert!(read_only_from(libc::ST_RDONLY));
        assert!(!read_only_from(0));
        assert!(read_only_from(libc::ST_RDONLY | other));
        assert!(!read_only_from(other));
    }

    #[cfg(unix)]
    #[test]
    fn the_writability_probe_reads_the_folder_and_its_parent() {
        use std::os::unix::fs::PermissionsExt;
        let mode = |p: &Path, m: u32| {
            std::fs::set_permissions(p, std::fs::Permissions::from_mode(m)).unwrap()
        };
        let tmp = tempfile::tempdir().unwrap();
        let parent = tmp.path().join("Applications");
        let app = parent.join("lokki.app");
        std::fs::create_dir_all(&app).unwrap();
        // `access` grants root everything, so as root only the first row can hold.
        let root = unsafe { libc::geteuid() } == 0;
        let owned = probe(&app);
        mode(&app, 0o555);
        let app_locked = probe(&app);
        mode(&app, 0o755);
        mode(&parent, 0o555);
        let parent_locked = probe(&app);
        mode(&parent, 0o755);
        assert_eq!(owned, (false, true), "a folder the user owns");
        assert_eq!(app_locked, (false, root), "the app chmod 555");
        assert_eq!(parent_locked, (false, root), "its parent chmod 555");
    }

    #[test]
    fn a_policy_that_turns_false_before_the_press_refuses_before_the_download() {
        let log = Log::default();
        let m = Machine::new(
            FakeSource::new(&log).answers(Ok(Some(found("0.2.0")))),
            FakeClock::default(),
            "0.1.0",
            turning(vec![installs(), downloads_only()]),
        );
        assert!(m.status().can_install);
        m.check();
        let e = m.install(stop_ok(&log), resume(&log)).unwrap_err();
        assert!(e.contains("AppImage"), "{e}");
        assert_eq!(*log.borrow(), vec!["check"], "no download, no stop");
        let status = m.status();
        assert!(!status.can_install, "the status reports the last policy");
        assert_eq!(status.state, "available");
    }

    #[test]
    fn a_policy_that_turns_false_after_the_stop_resumes_the_sidecars_and_reports_the_reason() {
        let log = Log::default();
        let m = Machine::new(
            FakeSource::new(&log).answers(Ok(Some(found("0.2.0")))),
            FakeClock::default(),
            "0.1.0",
            turning(vec![installs(), installs(), downloads_only()]),
        );
        m.check();
        let e = m.install(stop_ok(&log), resume(&log)).unwrap_err();
        assert!(e.contains("AppImage"), "{e}");
        assert_eq!(
            *log.borrow(),
            vec!["check", "download", "stop", "resume"],
            "no install"
        );
        let status = m.status();
        assert_eq!(status.state, "error");
        assert_eq!(status.error.as_deref(), Some(e.as_str()));
        assert!(!status.can_install);
    }

    #[test]
    fn a_package_that_fails_verification_stops_nothing() {
        let log = Log::default();
        let source = FakeSource::new(&log).answers(Ok(Some(found("0.2.0"))));
        *source.download.borrow_mut() =
            Err("the download or its signature failed: signature verification failed".into());
        let m = machine(source, installs());
        m.check();
        let r = m.install(stop_ok(&log), resume(&log));
        assert!(r.unwrap_err().contains("signature"));
        assert_eq!(
            *log.borrow(),
            vec!["check", "download"],
            "neither stop nor resume ran"
        );
        assert_eq!(m.status().state, "error");
        assert_eq!(
            m.status().version.as_deref(),
            Some("0.2.0"),
            "the update stays known"
        );
    }

    #[test]
    fn an_alive_sidecar_blocks_the_install_and_the_sidecars_come_back() {
        let log = Log::default();
        let m = machine(
            FakeSource::new(&log).answers(Ok(Some(found("0.2.0")))),
            installs(),
        );
        m.check();
        let stop = {
            let log = Rc::clone(&log);
            move || {
                log.borrow_mut().push("stop");
                Err("the core (pid 4242) is still alive after the stop sequence".to_string())
            }
        };
        let r = m.install(stop, resume(&log));
        assert!(r.unwrap_err().contains("4242"));
        assert_eq!(*log.borrow(), vec!["check", "download", "stop", "resume"]);
        assert_eq!(m.status().state, "error");
    }

    #[test]
    fn a_failed_install_restarts_the_sidecars() {
        let log = Log::default();
        let source = FakeSource::new(&log).answers(Ok(Some(found("0.2.0"))));
        *source.install.borrow_mut() = Err("the install failed: permission denied".into());
        let m = machine(source, installs());
        m.check();
        let r = m.install(stop_ok(&log), resume(&log));
        assert!(r.unwrap_err().contains("permission"));
        assert_eq!(
            *log.borrow(),
            vec!["check", "download", "stop", "install", "resume"]
        );
        assert_eq!(m.status().state, "error");
    }

    #[test]
    fn an_install_runs_download_then_stop_then_install_once() {
        let log = Log::default();
        let m = machine(
            FakeSource::new(&log).answers(Ok(Some(found("0.2.0")))),
            installs(),
        );
        m.check();
        m.install(stop_ok(&log), resume(&log)).unwrap();
        assert_eq!(*log.borrow(), vec!["check", "download", "stop", "install"]);
        assert_eq!(m.status().state, "installing");
    }

    #[test]
    fn install_is_refused_without_an_available_update_or_the_policy_or_while_one_runs() {
        // (label, policy, check first, error contains)
        let rows = [
            ("nothing available", installs(), false, "no update"),
            ("not an AppImage", downloads_only(), true, "AppImage"),
        ];
        for (label, policy, check, err) in rows {
            let log = Log::default();
            let m = machine(
                FakeSource::new(&log).answers(Ok(Some(found("0.2.0")))),
                policy,
            );
            if check {
                m.check();
            }
            let r = m.install(stop_ok(&log), resume(&log));
            assert!(r.unwrap_err().contains(err), "{label}");
            assert!(!log.borrow().contains(&"download"), "{label}");
        }
        let log = Log::default();
        let m = machine(
            FakeSource::new(&log).answers(Ok(Some(found("0.2.0")))),
            installs(),
        );
        m.check();
        m.begin_install_for_test();
        assert!(m
            .install(stop_ok(&log), resume(&log))
            .unwrap_err()
            .contains("already running"));
        assert_eq!(
            m.check().state,
            "installing",
            "no check runs during an install"
        );
    }

    #[test]
    fn a_panic_in_a_check_or_an_install_is_an_error_and_the_next_one_runs() {
        let log = Log::default();
        let source = FakeSource::new(&log)
            .answers(Ok(Some(found("0.2.0"))))
            .answers(Ok(Some(found("0.2.0"))));
        source.panics_in.set(Some("check"));
        let m = machine(source, installs());
        let status = m.check();
        assert_eq!(status.state, "error");
        assert!(status.error.unwrap().contains("check panicked"));
        assert_eq!(m.check().state, "available", "the next check runs");

        // (the step that panics, the install fails on its own, the calls, what the error holds)
        let rows: [(&str, bool, &[&str], &[&str]); 4] = [
            (
                "download",
                false,
                &["download"],
                &["the download failed: download panicked"],
            ),
            (
                "stop",
                false,
                &["download", "stop", "resume"],
                &["the stop sequence failed: stop panicked"],
            ),
            (
                "install",
                false,
                &["download", "stop", "install", "resume"],
                &["the install failed: install panicked"],
            ),
            (
                "resume",
                true,
                &["download", "stop", "install", "resume"],
                &[
                    "permission denied",
                    "starting the sidecars again failed: resume panicked",
                ],
            ),
        ];
        for (step, install_fails, calls, holds) in rows {
            let log = Log::default();
            let m = machine(
                FakeSource::new(&log).answers(Ok(Some(found("0.2.0")))),
                installs(),
            );
            m.check();
            log.borrow_mut().clear();
            if install_fails {
                *m.source.install.borrow_mut() =
                    Err("the install failed: permission denied".into());
            }
            m.source.panics_in.set(Some(step));
            let stop = || {
                m.source.step("stop");
                Ok(())
            };
            let e = m.install(stop, || m.source.step("resume")).unwrap_err();
            assert_eq!(log.borrow().as_slice(), calls, "{step}");
            for h in holds {
                assert!(e.contains(h), "{step}: {e}");
            }
            assert_eq!(m.status().state, "error", "{step}");
            *m.source.install.borrow_mut() = Ok(());
            assert!(
                m.install(stop_ok(&log), resume(&log)).is_ok(),
                "{step}: the next install runs"
            );
        }
    }

    #[test]
    fn a_check_while_one_runs_returns_the_running_state() {
        let log = Log::default();
        let m = machine(FakeSource::new(&log), installs());
        m.begin_check_for_test();
        assert_eq!(m.check().state, "checking");
        assert!(log.borrow().is_empty(), "the source was not asked");
    }

    #[test]
    fn checks_are_due_a_minute_after_boot_then_daily_and_an_error_waits_for_the_tick() {
        let log = Log::default();
        let m = machine(
            FakeSource::new(&log)
                .answers(Err("boom".into()))
                .answers(Ok(None)),
            installs(),
        );
        assert!(!m.due(), "not before the first minute");
        m.clock.sleep(FIRST_CHECK);
        assert!(m.due());
        m.check();
        assert_eq!(m.status().state, "error");
        m.clock.sleep(Duration::from_secs(3600));
        assert!(!m.due(), "nothing retries inside the day");
        m.clock.sleep(DAILY);
        assert!(m.due());
        assert_eq!(m.check().state, "up_to_date");
        assert!(
            m.status().error.is_none(),
            "the next check clears the error"
        );
    }

    /// One HTTP/1.1 answer on an ephemeral loopback port; the thread ends after that answer,
    /// or with the test process if nothing ever connects.
    fn serve_once(body: String) -> String {
        use std::io::{Read, Write};
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!(
            "http://127.0.0.1:{}/latest.json",
            listener.local_addr().unwrap().port()
        );
        std::thread::spawn(move || {
            let (mut s, _) = listener.accept().unwrap();
            let mut buf = [0u8; 4096];
            let _ = s.read(&mut buf);
            let _ = write!(
                s,
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(),
                body
            );
        });
        url
    }

    /// The public key `tauri.conf.json` carries, so the test and the app hold one key.
    fn configured_pubkey() -> String {
        let conf: serde_json::Value =
            serde_json::from_str(include_str!("../tauri.conf.json")).unwrap();
        conf["plugins"]["updater"]["pubkey"]
            .as_str()
            .expect("tauri.conf.json names the updater's public key")
            .into()
    }

    fn mock_app() -> tauri::App<tauri::test::MockRuntime> {
        // `mock_context` already names the version 0.1.0.
        let mut context = tauri::test::mock_context(tauri::test::noop_assets());
        context.config_mut().plugins.0.insert(
            "updater".into(),
            serde_json::json!({
                "pubkey": configured_pubkey(),
                "endpoints": ["https://example.invalid/latest.json"],
            }),
        );
        tauri::test::mock_builder()
            .plugin(tauri_plugin_updater::Builder::new().build())
            .build(context)
            .unwrap()
    }

    #[test]
    fn the_plugin_answers_from_a_loopback_manifest() {
        let here = format!(
            "{}-{}",
            if cfg!(target_os = "macos") {
                "darwin"
            } else if cfg!(windows) {
                "windows"
            } else {
                "linux"
            },
            std::env::consts::ARCH
        );
        let platform = |key: &str| {
            format!(r#""{key}": {{"signature": "AAAA", "url": "http://127.0.0.1:1/none"}}"#)
        };
        // (label, the manifest's version, its platforms, the answer)
        let rows = [
            (
                "newer, for this platform",
                "9.9.9",
                platform(&here),
                Ok(Some("9.9.9")),
            ),
            ("not newer", "0.1.0", platform(&here), Ok(None)),
            (
                "no platform",
                "9.9.9",
                platform("plan9-mips"),
                Err(NO_PLATFORM),
            ),
        ];
        let app = mock_app();
        for (label, version, platforms, want) in rows {
            let url = serve_once(format!(
                r#"{{"version": "{version}", "notes": "", "pub_date": "2026-01-01T00:00:00Z", "platforms": {{{platforms}}}}}"#
            ));
            let source = PluginSource::new(app.handle().clone(), Some(url));
            let got = source.check().map(|f| f.map(|f| f.version));
            match want {
                Ok(v) => assert_eq!(got.unwrap().as_deref(), v, "{label}"),
                Err(e) => assert_eq!(got.unwrap_err(), e, "{label}"),
            }
        }
        let bad = PluginSource::new(app.handle().clone(), Some("not a url".into()));
        assert!(bad.check().unwrap_err().contains("LOKKI_UPDATE_ENDPOINT"));
    }

    #[test]
    fn the_no_platform_errors_are_the_plugins_two() {
        use tauri_plugin_updater::Error;
        assert!(is_no_platform(&Error::TargetsNotFound(vec![
            "linux-x86_64".into()
        ])));
        assert!(is_no_platform(&Error::TargetNotFound(
            "linux-x86_64".into()
        )));
        assert!(!is_no_platform(&Error::ReleaseNotFound));
    }
}
