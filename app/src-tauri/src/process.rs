//! Every process the shell starts, and what the shell can learn of a process it did not
//! start. A child made here opens no console window on Windows. A sidecar's whole tree
//! stops with it, and on Windows with the shell too, however the shell ends.
use std::ffi::OsStr;
use std::io;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::time::Duration;

#[cfg(windows)]
use windows_sys::Win32::System::Threading::CREATE_NO_WINDOW;

/// A command with no standard input and, on Windows, no console window. Every process the
/// shell starts is made here: a release build is a GUI-subsystem program, so a child
/// without the flag would open a console window of its own.
pub fn command(program: impl AsRef<OsStr>) -> Command {
    let mut cmd = Command::new(program);
    cmd.stdin(Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }
    cmd
}

/// A sidecar's whole tree: its process group on POSIX, its job on Windows.
pub struct Tree {
    pid: u32,
    #[cfg(windows)]
    job: win::Job,
}

/// Starts `cmd` as a sidecar. On POSIX it leads a process group of its own. On Windows it
/// starts suspended, goes into a job of its own and only then runs, so no child of it can
/// start outside the job. The job ends every process in it when its last handle closes;
/// the shell holds that handle, so the system ends the sidecar when the shell ends, however
/// it ends. The shell itself is in no such job, so what it starts otherwise, such as the
/// updater's installer, outlives it.
pub fn spawn(cmd: &mut Command) -> io::Result<(Child, Tree)> {
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        let child = cmd.process_group(0).spawn()?;
        let pid = child.id();
        Ok((child, Tree { pid }))
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        use windows_sys::Win32::System::Threading::{CREATE_NEW_PROCESS_GROUP, CREATE_SUSPENDED};
        let job = win::Job::new()?;
        // A console process group of its own, so that the stop reaches it alone.
        let mut child = cmd
            .creation_flags(CREATE_NO_WINDOW | CREATE_SUSPENDED | CREATE_NEW_PROCESS_GROUP)
            .spawn()?;
        if let Err(e) = job.assign(&child).and_then(|()| win::resume(child.id())) {
            let _ = child.kill();
            let _ = child.wait();
            return Err(e);
        }
        let pid = child.id();
        Ok((child, Tree { pid, job }))
    }
}

impl Tree {
    /// The stop a sidecar hears: SIGTERM to its group on POSIX; on Windows
    /// `CTRL_BREAK_EVENT` to its console process group, which CLIProxyAPI reads as SIGINT.
    /// The core on Windows has no handler for it and ends at once. False when it could not
    /// be sent.
    pub fn interrupt(&self) -> bool {
        #[cfg(unix)]
        {
            // The group's id is the leader's pid (`process_group(0)`). The kernel keeps that
            // id from reuse while any member is alive, which is when this signal matters.
            unsafe { libc::kill(-(self.pid as libc::pid_t), libc::SIGTERM) == 0 }
        }
        #[cfg(windows)]
        {
            win::interrupt(self.pid)
        }
    }

    /// Ends every process of the tree at once.
    pub fn kill(&self) {
        #[cfg(unix)]
        unsafe {
            libc::kill(-(self.pid as libc::pid_t), libc::SIGKILL);
        }
        #[cfg(windows)]
        self.job.terminate();
    }
}

/// How long a kill waits for the processes it ended to be gone.
const KILL_WAIT: Duration = Duration::from_secs(5);

/// The process listening on a loopback port, or one that started it, held so that its pid
/// cannot name another process while the shell acts on it: by a handle on Windows and a
/// pidfd on Linux. macOS has no such hold; there the pid is checked against the port again
/// before each signal.
pub struct Listener {
    pub pid: u32,
    /// Its executable, when the system says.
    pub exe: Option<PathBuf>,
    /// Whether it runs as the user the shell runs as; false when the system does not say.
    pub same_user: bool,
    #[cfg(windows)]
    handle: std::os::windows::io::OwnedHandle,
    #[cfg(target_os = "linux")]
    pidfd: std::os::fd::OwnedFd,
}

/// The process that holds `port` in the listening state on this machine, if the system
/// says. It is found, held, and found again: a pid that no longer holds the port is not it.
/// macOS has no hold and asks `lsof` once. A second ask would only catch the pid reused in
/// between, which needs the holder to have ended, and its port can change hands then anyway.
pub fn listener(port: u16) -> Option<Listener> {
    let pid = owner(port)?;
    #[cfg(windows)]
    let found = win::open(pid).map(|handle| Listener {
        pid,
        exe: win::image(&handle),
        same_user: win::same_user(&handle),
        handle,
    });
    #[cfg(target_os = "linux")]
    let found = pidfd_open(pid).map(|pidfd| held(pid, pidfd));
    #[cfg(target_os = "macos")]
    let found = Some(Listener {
        pid,
        exe: proc_pidpath(pid),
        same_user: same_user(pid),
    });
    found.filter(|_| cfg!(target_os = "macos") || owner(port) == Some(pid))
}

/// Whether `pid` runs as the user the shell runs as. `/proc/<pid>` belongs to the
/// process's effective user.
#[cfg(target_os = "linux")]
fn same_user(pid: u32) -> bool {
    use std::os::unix::fs::MetadataExt;
    std::fs::metadata(format!("/proc/{pid}")).is_ok_and(|m| m.uid() == unsafe { libc::geteuid() })
}

/// Whether `pid` runs as the user the shell runs as: its effective user.
#[cfg(target_os = "macos")]
fn same_user(pid: u32) -> bool {
    proc_bsdinfo(pid).map(|i| i.pbi_uid) == Some(unsafe { libc::geteuid() })
}

impl Listener {
    /// Stops it and reports whether it is gone. On POSIX SIGTERM, then SIGKILL once `wait`
    /// has passed; on Windows it is killed at once, since a process the shell did not start
    /// is in no console group the shell can reach.
    pub fn stop(&self, port: u16, wait: Duration) -> bool {
        #[cfg(windows)]
        {
            let _ = port;
            win::terminate(&self.handle, wait)
        }
        #[cfg(target_os = "linux")]
        {
            let _ = port;
            self.signal(libc::SIGTERM);
            if self.exited(wait) {
                return true;
            }
            self.signal(libc::SIGKILL);
            self.exited(Duration::from_secs(2))
        }
        #[cfg(target_os = "macos")]
        {
            // With no hold on the pid, a signal goes only while the pid still holds the port,
            // and the process counts as gone once it no longer does.
            let signal = |sig| {
                if owner(port) == Some(self.pid) {
                    unsafe { libc::kill(self.pid as libc::pid_t, sig) };
                }
            };
            signal(libc::SIGTERM);
            if released(port, self.pid, wait) {
                return true;
            }
            signal(libc::SIGKILL);
            released(port, self.pid, Duration::from_secs(2))
        }
    }

    /// The process that started it, held as it is, while the system still names one.
    pub fn parent(&self) -> Option<Listener> {
        #[cfg(windows)]
        {
            let pid = win::parent(self.pid)?;
            let handle = win::open(pid)?;
            // Windows keeps a parent's pid after the parent is gone, and the pid may name a
            // later process: the parent is the one that was created before its child. That
            // holds whether or not the child still runs, so it is not asked: a later process
            // under the pid was created after the parent ended, which is after the child began.
            (win::created(&handle)? <= win::created(&self.handle)?).then(|| Listener {
                pid,
                exe: win::image(&handle),
                same_user: win::same_user(&handle),
                handle,
            })
        }
        #[cfg(target_os = "linux")]
        {
            // A live process's parent is alive, so a pid this process still names as its
            // parent after the pidfd is open is the parent that pidfd holds.
            let pid = parent_pid(self.pid)?;
            let parent = held(pid, pidfd_open(pid)?);
            let live = |p: &Listener| !p.exited(Duration::ZERO);
            (parent_pid(self.pid) == Some(pid) && live(self) && live(&parent)).then_some(parent)
        }
        #[cfg(target_os = "macos")]
        {
            // Checked again before the kill, as the listener's own pid is.
            let pid = proc_bsdinfo(self.pid)?.pbi_ppid;
            Some(Listener {
                pid,
                exe: proc_pidpath(pid),
                same_user: same_user(pid),
            })
        }
    }

    /// Kills it and `ancestors`, the processes that started it from the nearest up, at once
    /// and with nothing any of them can handle. True when every one is gone; on macOS, when
    /// the port is let go. There each ancestor is killed only while the listener still holds
    /// `port` and every process between it and the ancestor still names the next as parent.
    pub fn kill(&self, port: u16, ancestors: &[Listener]) -> bool {
        // The ancestors first: on macOS the line to them runs through the live listener.
        #[cfg(windows)]
        {
            let _ = port;
            // Every one is ended, whichever of them fails.
            let gone: Vec<bool> = ancestors
                .iter()
                .chain([self])
                .map(|p| win::terminate(&p.handle, KILL_WAIT))
                .collect();
            gone.into_iter().all(|g| g)
        }
        #[cfg(target_os = "linux")]
        {
            let _ = port;
            let all = || ancestors.iter().chain([self]);
            all().for_each(|p| p.signal(libc::SIGKILL));
            all().all(|p| p.exited(KILL_WAIT))
        }
        #[cfg(target_os = "macos")]
        {
            if owner(port) == Some(self.pid) {
                let mut child = self.pid;
                let line: Vec<u32> = ancestors
                    .iter()
                    .map(|a| a.pid)
                    .take_while(|&pid| {
                        let linked = proc_bsdinfo(child).map(|i| i.pbi_ppid) == Some(pid);
                        child = pid;
                        linked
                    })
                    .collect();
                for pid in line {
                    unsafe { libc::kill(pid as libc::pid_t, libc::SIGKILL) };
                }
                if owner(port) == Some(self.pid) {
                    unsafe { libc::kill(self.pid as libc::pid_t, libc::SIGKILL) };
                }
            }
            released(port, self.pid, KILL_WAIT)
        }
    }

    #[cfg(target_os = "linux")]
    fn signal(&self, sig: libc::c_int) {
        use std::os::fd::AsRawFd;
        unsafe {
            libc::syscall(
                libc::SYS_pidfd_send_signal,
                self.pidfd.as_raw_fd(),
                sig,
                std::ptr::null::<libc::siginfo_t>(),
                0,
            );
        }
    }

    /// Whether it exits within `wait`: a pidfd turns readable when its process exits.
    #[cfg(target_os = "linux")]
    fn exited(&self, wait: Duration) -> bool {
        use std::os::fd::AsRawFd;
        let mut fd = libc::pollfd {
            fd: self.pidfd.as_raw_fd(),
            events: libc::POLLIN,
            revents: 0,
        };
        let ms = wait.as_millis().min(i32::MAX as u128) as libc::c_int;
        unsafe { libc::poll(&mut fd, 1, ms) == 1 }
    }
}

/// `pid`, held by `pidfd`, with what `/proc` says of it.
#[cfg(target_os = "linux")]
fn held(pid: u32, pidfd: std::os::fd::OwnedFd) -> Listener {
    Listener {
        pid,
        exe: std::fs::read_link(format!("/proc/{pid}/exe")).ok(),
        same_user: same_user(pid),
        pidfd,
    }
}

/// The parent pid in `/proc/<pid>/stat`, whose fields follow the command in parentheses.
#[cfg(target_os = "linux")]
fn parent_pid(pid: u32) -> Option<u32> {
    let stat = std::fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    stat.rsplit_once(')')?
        .1
        .split_whitespace()
        .nth(1)?
        .parse()
        .ok()
}

/// Whether `pid` lets go of `port` within `wait`.
#[cfg(target_os = "macos")]
fn released(port: u16, pid: u32, wait: Duration) -> bool {
    let end = std::time::Instant::now() + wait;
    while owner(port) == Some(pid) {
        if std::time::Instant::now() >= end {
            return false;
        }
        std::thread::sleep(Duration::from_millis(200));
    }
    true
}

/// The pid that holds `port` in the listening state, from the system's own table.
#[cfg(target_os = "linux")]
fn owner(port: u16) -> Option<u32> {
    // `/proc/net/tcp{,6}`: `sl local_address rem_address st … inode`; the port is the
    // address's hex suffix, and state 0A is LISTEN.
    let port = format!(":{port:04X}");
    let mut inodes = Vec::new();
    for table in ["/proc/net/tcp", "/proc/net/tcp6"] {
        let Ok(text) = std::fs::read_to_string(table) else {
            continue;
        };
        for line in text.lines().skip(1) {
            let cols: Vec<&str> = line.split_whitespace().collect();
            if cols.len() > 9 && cols[3] == "0A" && cols[1].ends_with(&port) {
                inodes.push(format!("socket:[{}]", cols[9]));
            }
        }
    }
    if inodes.is_empty() {
        return None;
    }
    for entry in std::fs::read_dir("/proc").ok()?.flatten() {
        let Some(pid) = entry.file_name().to_str().and_then(|n| n.parse().ok()) else {
            continue;
        };
        let Ok(fds) = std::fs::read_dir(entry.path().join("fd")) else {
            continue;
        };
        for fd in fds.flatten() {
            if std::fs::read_link(fd.path()).is_ok_and(|l| inodes.iter().any(|i| l == *i)) {
                return Some(pid);
            }
        }
    }
    None
}

#[cfg(target_os = "macos")]
fn owner(port: u16) -> Option<u32> {
    // Bounded: a start that asks holds the sidecars' lock, and a quit waits on it.
    const LSOF_WAIT: Duration = Duration::from_secs(3);
    let mut lsof = command("/usr/sbin/lsof");
    lsof.args(["-nP", "-t", &format!("-iTCP:{port}"), "-sTCP:LISTEN"]);
    output_within(&mut lsof, LSOF_WAIT)?
        .lines()
        .next()?
        .trim()
        .parse()
        .ok()
}

/// What `cmd` prints, if it ends within `wait`. One that does not is killed and given a
/// moment more, never waited for: a kill does not end a process stuck in the kernel, as on
/// a hung network mount.
#[cfg(any(target_os = "macos", test))]
fn output_within(cmd: &mut Command, wait: Duration) -> Option<String> {
    let mut child = cmd
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .ok()?;
    if !ends_within(&mut child, wait) {
        let _ = child.kill();
        ends_within(&mut child, Duration::from_millis(500));
        return None;
    }
    let mut out = String::new();
    std::io::Read::read_to_string(&mut child.stdout.take()?, &mut out).ok()?;
    Some(out)
}

#[cfg(any(target_os = "macos", test))]
fn ends_within(child: &mut Child, wait: Duration) -> bool {
    let end = std::time::Instant::now() + wait;
    loop {
        match child.try_wait() {
            Ok(Some(_)) => return true,
            Ok(None) if std::time::Instant::now() < end => {
                std::thread::sleep(Duration::from_millis(20));
            }
            _ => return false,
        }
    }
}

#[cfg(windows)]
fn owner(port: u16) -> Option<u32> {
    win::owner(port)
}

#[cfg(target_os = "linux")]
fn pidfd_open(pid: u32) -> Option<std::os::fd::OwnedFd> {
    use std::os::fd::FromRawFd;
    let fd = unsafe { libc::syscall(libc::SYS_pidfd_open, pid as libc::pid_t, 0) };
    (fd >= 0).then(|| unsafe { std::os::fd::OwnedFd::from_raw_fd(fd as libc::c_int) })
}

#[cfg(target_os = "macos")]
fn proc_bsdinfo(pid: u32) -> Option<libc::proc_bsdinfo> {
    let mut info: libc::proc_bsdinfo = unsafe { std::mem::zeroed() };
    let size = size_of::<libc::proc_bsdinfo>() as libc::c_int;
    let n = unsafe {
        libc::proc_pidinfo(
            pid as libc::c_int,
            libc::PROC_PIDTBSDINFO,
            0,
            std::ptr::from_mut(&mut info).cast(),
            size,
        )
    };
    (n == size).then_some(info)
}

#[cfg(target_os = "macos")]
fn proc_pidpath(pid: u32) -> Option<PathBuf> {
    use std::os::unix::ffi::OsStrExt;
    let mut buf = vec![0u8; libc::PROC_PIDPATHINFO_MAXSIZE as usize];
    let n = unsafe {
        libc::proc_pidpath(
            pid as libc::c_int,
            buf.as_mut_ptr().cast(),
            buf.len() as u32,
        )
    };
    (n > 0).then(|| PathBuf::from(OsStr::from_bytes(&buf[..n as usize])))
}

#[cfg(windows)]
mod win {
    use std::io;
    use std::os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle};
    use std::path::PathBuf;
    use std::process::Child;
    use std::sync::{Mutex, PoisonError};
    use std::time::Duration;
    use windows_sys::Win32::Foundation::{
        ERROR_INSUFFICIENT_BUFFER, FILETIME, HANDLE, INVALID_HANDLE_VALUE, WAIT_OBJECT_0,
    };
    use windows_sys::Win32::NetworkManagement::IpHelper::{
        GetExtendedTcpTable, MIB_TCPROW_OWNER_PID, MIB_TCPTABLE_OWNER_PID,
        TCP_TABLE_OWNER_PID_LISTENER,
    };
    use windows_sys::Win32::Networking::WinSock::AF_INET;
    use windows_sys::Win32::Security::{
        EqualSid, GetTokenInformation, TokenUser, TOKEN_QUERY, TOKEN_USER,
    };
    use windows_sys::Win32::System::Console::{
        AttachConsole, FreeConsole, GenerateConsoleCtrlEvent, CTRL_BREAK_EVENT,
    };
    use windows_sys::Win32::System::Diagnostics::ToolHelp::{
        CreateToolhelp32Snapshot, Process32FirstW, Process32NextW, Thread32First, Thread32Next,
        PROCESSENTRY32W, TH32CS_SNAPPROCESS, TH32CS_SNAPTHREAD, THREADENTRY32,
    };
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
        SetInformationJobObject, TerminateJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };
    use windows_sys::Win32::System::Threading::{
        GetCurrentProcess, GetProcessTimes, OpenProcess, OpenProcessToken, OpenThread,
        QueryFullProcessImageNameW, ResumeThread, TerminateProcess, WaitForSingleObject,
        PROCESS_NAME_WIN32, PROCESS_QUERY_LIMITED_INFORMATION, PROCESS_SYNCHRONIZE,
        PROCESS_TERMINATE, THREAD_SUSPEND_RESUME,
    };

    /// A job that ends every process in it when its last handle closes.
    pub struct Job(OwnedHandle);

    impl Job {
        pub fn new() -> io::Result<Job> {
            let raw = unsafe { CreateJobObjectW(std::ptr::null(), std::ptr::null()) };
            if raw.is_null() {
                return Err(io::Error::last_os_error());
            }
            let job = Job(unsafe { OwnedHandle::from_raw_handle(raw) });
            let mut info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            let set = unsafe {
                SetInformationJobObject(
                    job.raw(),
                    JobObjectExtendedLimitInformation,
                    std::ptr::from_ref(&info).cast(),
                    size_of_val(&info) as u32,
                )
            };
            if set == 0 {
                return Err(io::Error::last_os_error());
            }
            Ok(job)
        }

        pub fn raw(&self) -> HANDLE {
            self.0.as_raw_handle()
        }

        pub fn assign(&self, child: &Child) -> io::Result<()> {
            if unsafe { AssignProcessToJobObject(self.raw(), child.as_raw_handle()) } == 0 {
                return Err(io::Error::last_os_error());
            }
            Ok(())
        }

        pub fn terminate(&self) {
            unsafe { TerminateJobObject(self.raw(), 1) };
        }
    }

    /// Resumes every thread of the suspended process `pid`.
    pub fn resume(pid: u32) -> io::Result<()> {
        let snap = unsafe { CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0) };
        if snap == INVALID_HANDLE_VALUE {
            return Err(io::Error::last_os_error());
        }
        let snap = unsafe { OwnedHandle::from_raw_handle(snap) };
        let mut entry = THREADENTRY32 {
            dwSize: size_of::<THREADENTRY32>() as u32,
            ..Default::default()
        };
        let mut resumed = 0;
        let mut more = unsafe { Thread32First(snap.as_raw_handle(), &mut entry) } != 0;
        while more {
            if entry.th32OwnerProcessID == pid {
                let thread = unsafe { OpenThread(THREAD_SUSPEND_RESUME, 0, entry.th32ThreadID) };
                if !thread.is_null() {
                    let thread = unsafe { OwnedHandle::from_raw_handle(thread) };
                    if unsafe { ResumeThread(thread.as_raw_handle()) } != u32::MAX {
                        resumed += 1;
                    }
                }
            }
            more = unsafe { Thread32Next(snap.as_raw_handle(), &mut entry) } != 0;
        }
        if resumed == 0 {
            return Err(io::Error::other(format!(
                "no thread of process {pid} could be resumed"
            )));
        }
        Ok(())
    }

    /// `CTRL_BREAK_EVENT` to the console process group `pid` leads. The shell, a
    /// GUI-subsystem program, has no console of its own and attaches to the sidecar's for
    /// the call. A debug build has a console, cannot attach, and its stop ends in the kill.
    pub fn interrupt(pid: u32) -> bool {
        // A process has one console at a time.
        static CONSOLE: Mutex<()> = Mutex::new(());
        let _one = CONSOLE.lock().unwrap_or_else(PoisonError::into_inner);
        unsafe {
            if AttachConsole(pid) == 0 {
                return false;
            }
            let sent = GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, pid) != 0;
            FreeConsole();
            sent
        }
    }

    /// The listening IPv4 socket on `port` and its owner, from the system's TCP table.
    pub fn owner(port: u16) -> Option<u32> {
        let mut size = 0u32;
        for _ in 0..4 {
            // u32s, so the table is aligned for its rows.
            let mut buf = vec![0u32; (size as usize).div_ceil(4).max(1)];
            let mut have = (buf.len() * 4) as u32;
            let rc = unsafe {
                GetExtendedTcpTable(
                    buf.as_mut_ptr().cast(),
                    &mut have,
                    0,
                    u32::from(AF_INET),
                    TCP_TABLE_OWNER_PID_LISTENER,
                    0,
                )
            };
            if rc == ERROR_INSUFFICIENT_BUFFER {
                size = have;
                continue;
            }
            if rc != 0 {
                return None;
            }
            let table = buf.as_ptr().cast::<MIB_TCPTABLE_OWNER_PID>();
            let rows: &[MIB_TCPROW_OWNER_PID] = unsafe {
                std::slice::from_raw_parts((*table).table.as_ptr(), (*table).dwNumEntries as usize)
            };
            // The port is in network byte order in the low 16 bits.
            return rows
                .iter()
                .find(|r| u16::from_be(r.dwLocalPort as u16) == port)
                .map(|r| r.dwOwningPid);
        }
        None
    }

    /// A handle that keeps `pid` from naming another process while it is open.
    pub fn open(pid: u32) -> Option<OwnedHandle> {
        let access = PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_TERMINATE | PROCESS_SYNCHRONIZE;
        let raw = unsafe { OpenProcess(access, 0, pid) };
        (!raw.is_null()).then(|| unsafe { OwnedHandle::from_raw_handle(raw) })
    }

    pub fn image(process: &OwnedHandle) -> Option<PathBuf> {
        let mut buf = vec![0u16; 32_768];
        let mut len = buf.len() as u32;
        let ok = unsafe {
            QueryFullProcessImageNameW(
                process.as_raw_handle(),
                PROCESS_NAME_WIN32,
                buf.as_mut_ptr(),
                &mut len,
            )
        };
        (ok != 0).then(|| PathBuf::from(String::from_utf16_lossy(&buf[..len as usize])))
    }

    /// Whether `process` runs as the user of this process: the two tokens' user SIDs are
    /// equal. False when either token cannot be read.
    pub fn same_user(process: &OwnedHandle) -> bool {
        // TOKEN_USER and the SID it points to, in one buffer of pointer alignment.
        fn user(process: HANDLE) -> Option<Vec<usize>> {
            let mut token: HANDLE = std::ptr::null_mut();
            if unsafe { OpenProcessToken(process, TOKEN_QUERY, &mut token) } == 0 {
                return None;
            }
            let token = unsafe { OwnedHandle::from_raw_handle(token) };
            let mut buf = vec![0usize; 64];
            let mut len = 0u32;
            let ok = unsafe {
                GetTokenInformation(
                    token.as_raw_handle(),
                    TokenUser,
                    buf.as_mut_ptr().cast(),
                    (buf.len() * size_of::<usize>()) as u32,
                    &mut len,
                )
            };
            (ok != 0).then_some(buf)
        }
        let sid = |buf: &[usize]| unsafe { (*buf.as_ptr().cast::<TOKEN_USER>()).User.Sid };
        match (
            user(process.as_raw_handle()),
            user(unsafe { GetCurrentProcess() }),
        ) {
            (Some(theirs), Some(ours)) => unsafe { EqualSid(sid(&theirs), sid(&ours)) != 0 },
            _ => false,
        }
    }

    /// The pid `pid` was started by, from the system's process list.
    pub fn parent(pid: u32) -> Option<u32> {
        let snap = unsafe { CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0) };
        if snap == INVALID_HANDLE_VALUE {
            return None;
        }
        let snap = unsafe { OwnedHandle::from_raw_handle(snap) };
        let mut entry = PROCESSENTRY32W {
            dwSize: size_of::<PROCESSENTRY32W>() as u32,
            ..Default::default()
        };
        let mut more = unsafe { Process32FirstW(snap.as_raw_handle(), &mut entry) } != 0;
        while more {
            if entry.th32ProcessID == pid {
                return Some(entry.th32ParentProcessID);
            }
            more = unsafe { Process32NextW(snap.as_raw_handle(), &mut entry) } != 0;
        }
        None
    }

    /// When `process` was created, in the system's 100 ns ticks.
    pub fn created(process: &OwnedHandle) -> Option<u64> {
        let mut times = [FILETIME::default(); 4];
        let [created, exited, kernel, user] = &mut times;
        let ok = unsafe { GetProcessTimes(process.as_raw_handle(), created, exited, kernel, user) };
        let at = times[0];
        (ok != 0).then(|| (u64::from(at.dwHighDateTime) << 32) | u64::from(at.dwLowDateTime))
    }

    pub fn terminate(process: &OwnedHandle, wait: Duration) -> bool {
        unsafe {
            TerminateProcess(process.as_raw_handle(), 1);
            let ms = wait.as_millis().min(u128::from(u32::MAX - 1)) as u32;
            WaitForSingleObject(process.as_raw_handle(), ms) == WAIT_OBJECT_0
        }
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use std::io::{BufRead, BufReader, Read, Write};
    use std::net::TcpListener;
    use std::path::Path;

    /// The Rust files the shell runs: this crate's and `lokki_logic`'s, each without the
    /// test module that ends it.
    fn shell_sources() -> Vec<(PathBuf, String)> {
        let root = Path::new(env!("CARGO_MANIFEST_DIR"));
        let mut files = Vec::new();
        for dir in [root.join("src"), root.join("logic/src")] {
            for entry in std::fs::read_dir(&dir).unwrap().flatten() {
                let path = entry.path();
                if path.extension().is_some_and(|e| e == "rs") {
                    let text = std::fs::read_to_string(&path).unwrap();
                    let code = text.split("mod tests {").next().unwrap().to_string();
                    files.push((path, code));
                }
            }
        }
        assert!(files.len() > 10, "the walk found the sources");
        files
    }

    #[test]
    fn every_process_the_shell_starts_opens_no_console_window() {
        // The check reads text: it holds the forms a call is written in, with any spacing
        // and through a renamed import, and not an alias made some other way.
        let mut news = Vec::new();
        for (path, code) in shell_sources() {
            let name = path.file_name().unwrap().to_string_lossy().into_owned();
            let code: String = code.split_whitespace().collect();
            news.extend(code.matches("Command::new(").map(|_| name.clone()));
            assert!(
                !code.contains("Commandas"),
                "{name}: Command is imported under another name"
            );
            // Flags replace the ones set before: each call names the window flag again.
            for (at, _) in code.match_indices(".creation_flags(") {
                let call = whole_call(&code[at..]);
                assert!(
                    call.contains("CREATE_NO_WINDOW"),
                    "{name}: {call} drops CREATE_NO_WINDOW"
                );
            }
        }
        assert_eq!(
            news,
            ["process.rs"],
            "a process is made only by process::command"
        );
    }

    /// `text` up to the parenthesis that closes its first one.
    fn whole_call(text: &str) -> &str {
        let mut depth = 0;
        for (i, c) in text.char_indices() {
            match c {
                '(' => depth += 1,
                ')' if depth == 1 => return &text[..=i],
                ')' => depth -= 1,
                _ => {}
            }
        }
        text
    }

    /// This test binary, run as one of the roles `role` plays.
    pub(crate) fn me(role: &str) -> Command {
        let mut cmd = command(std::env::current_exe().unwrap());
        cmd.args([
            "process::tests::role",
            "--exact",
            "--ignored",
            "--nocapture",
        ])
        .env("LOKKI_TEST_ROLE", role);
        cmd
    }

    /// A copy of this test binary named `file` (with the system's executable suffix) in
    /// `dir`, run as `role`.
    pub(crate) fn me_as(dir: &Path, file: &str, role: &str) -> Command {
        me_at(&copy_of_me(dir, file), role)
    }

    /// A copy of this test binary named `file` (with the system's executable suffix) in `dir`.
    pub(crate) fn copy_of_me(dir: &Path, file: &str) -> PathBuf {
        let copy = dir.join(format!("{file}{}", std::env::consts::EXE_SUFFIX));
        std::fs::copy(std::env::current_exe().unwrap(), &copy).unwrap();
        copy
    }

    /// The copy of this test binary at `exe`, run as `role`.
    fn me_at(exe: &Path, role: &str) -> Command {
        let model = me(role);
        let mut cmd = command(exe);
        cmd.args(model.get_args()).envs(
            model
                .get_envs()
                .filter_map(|(k, v)| Some((k.to_owned(), v?.to_owned()))),
        );
        cmd
    }

    /// Ends the process `pid` at once, if it still runs.
    pub(crate) fn end(pid: u32) {
        #[cfg(unix)]
        unsafe {
            libc::kill(pid as libc::pid_t, libc::SIGKILL);
        }
        #[cfg(windows)]
        if let Some(process) = win::open(pid) {
            win::terminate(&process, Duration::from_secs(5));
        }
    }

    /// Reads `child`'s output until a line starts with `prefix`, and returns the rest.
    pub(crate) fn line_after(child: &mut Child, prefix: &str) -> String {
        let out = BufReader::new(child.stdout.as_mut().unwrap());
        for line in out.lines() {
            let line = line.unwrap();
            if let Some(rest) = line.strip_prefix(prefix) {
                return rest.trim().to_string();
            }
        }
        panic!("the child ended before it printed {prefix:?}");
    }

    /// Not a test: the processes the tests start. A shell starts a sidecar as `spawn` does,
    /// or without it; a sidecar starts a child and prints both pids; a starter starts the
    /// first file of the list `LOKKI_TEST_CHILD`, as a starter of the rest or, the last, as
    /// an HTTP listener, and prints both pids; a listener holds a loopback port and prints
    /// it, and one that ends on a request or closes it ends on the first request or closes
    /// each unanswered; an HTTP listener also prints the first line of each
    /// request, and answers `LOKKI_TEST_STATUS` to one that carries `LOKKI_TEST_CREDENTIAL`
    /// and 401 to any other, and exits once it has answered 202 unless `LOKKI_TEST_STAYS` is
    /// set, as a core whose drain died does. A connection that sends nothing, as a probe of
    /// the port does, is no request.
    #[test]
    #[ignore = "a role the tests run in a child process"]
    fn role() {
        let hold = || std::thread::sleep(Duration::from_secs(60));
        match std::env::var("LOKKI_TEST_ROLE").as_deref() {
            Ok("shell") => {
                let _tree = spawn(&mut me("sidecar")).unwrap();
                hold();
            }
            Ok("shell-without-job") => {
                let _child = me("sidecar").spawn().unwrap();
                hold();
            }
            Ok("sidecar") => {
                let child = me("child").spawn().unwrap();
                println!("pids {} {}", std::process::id(), child.id());
                hold();
            }
            Ok("starter") => {
                let files = std::env::var_os("LOKKI_TEST_CHILD").unwrap();
                let mut line: Vec<PathBuf> = std::env::split_paths(&files).collect();
                let file = line.remove(0);
                let child = if line.is_empty() {
                    me_at(&file, "http").spawn().unwrap()
                } else {
                    me_at(&file, "starter")
                        .env("LOKKI_TEST_CHILD", std::env::join_paths(line).unwrap())
                        .spawn()
                        .unwrap()
                };
                println!("pids {} {}", std::process::id(), child.id());
                hold();
            }
            Ok("ends-on-a-request") => {
                let l = TcpListener::bind("127.0.0.1:0").unwrap();
                println!("port {}", l.local_addr().unwrap().port());
                for stream in l.incoming() {
                    if stream.unwrap().read(&mut [0u8]).unwrap_or(0) == 1 {
                        std::process::exit(1);
                    }
                }
            }
            Ok("closes-on-a-request") => {
                let l = TcpListener::bind("127.0.0.1:0").unwrap();
                println!("port {}", l.local_addr().unwrap().port());
                std::thread::spawn(move || {
                    hold();
                    std::process::exit(0);
                });
                for stream in l.incoming() {
                    drop(stream);
                }
            }
            Ok("listener") => {
                let l = TcpListener::bind("127.0.0.1:0").unwrap();
                println!("port {}", l.local_addr().unwrap().port());
                hold();
            }
            Ok("http") => {
                let l = TcpListener::bind("127.0.0.1:0").unwrap();
                println!("port {}", l.local_addr().unwrap().port());
                let credential = std::env::var("LOKKI_TEST_CREDENTIAL").unwrap();
                let ok: u16 = std::env::var("LOKKI_TEST_STATUS").unwrap().parse().unwrap();
                let stays = std::env::var_os("LOKKI_TEST_STAYS").is_some();
                if stays {
                    // No longer than a holder that never answers.
                    std::thread::spawn(move || {
                        hold();
                        std::process::exit(0);
                    });
                }
                for stream in l.incoming() {
                    // Printed before the answer, so a caller that has its answer can read it.
                    let (_, status) = answer(stream.unwrap(), |head| {
                        if let Some(first) = head.lines().next() {
                            println!("request {first}");
                        }
                        if head.contains(&credential) {
                            ok
                        } else {
                            401
                        }
                    });
                    if status == 202 && !stays {
                        return;
                    }
                }
            }
            _ => hold(),
        }
    }

    /// Reads one request head from `stream` and answers it with `status(head)` and a body
    /// of `drain_seconds` 0. Returns the head and the status.
    pub(crate) fn answer(
        mut stream: std::net::TcpStream,
        status: impl Fn(&str) -> u16,
    ) -> (String, u16) {
        let mut head = Vec::new();
        let mut byte = [0u8];
        while !head.ends_with(b"\r\n\r\n") && stream.read(&mut byte).unwrap_or(0) == 1 {
            head.push(byte[0]);
        }
        let head = String::from_utf8_lossy(&head).into_owned();
        let status = status(&head);
        let body = r#"{"drain_seconds": 0}"#;
        let _ = write!(
            stream,
            "HTTP/1.1 {status} X\r\nContent-Type: application/json\r\n\
             Content-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        );
        (head, status)
    }

    #[test]
    fn the_listener_on_a_port_is_found_and_stopped() {
        let own = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = own.local_addr().unwrap().port();
        let found = listener(port).expect("this process's listener is found");
        assert_eq!(found.pid, std::process::id());
        assert!(found.same_user, "this process runs as its own user");
        let exe = |p: &Path| std::fs::canonicalize(p).unwrap();
        assert_eq!(
            found.exe.as_deref().map(exe),
            Some(exe(&std::env::current_exe().unwrap()))
        );
        drop(found);
        drop(own);

        let mut child = me("listener").stdout(Stdio::piped()).spawn().unwrap();
        let port: u16 = line_after(&mut child, "port ").parse().unwrap();
        let found = listener(port).expect("the child's listener is found");
        assert_eq!(found.pid, child.id());
        assert!(
            found.stop(port, Duration::from_secs(5)),
            "the stop ended it"
        );
        let status = child.try_wait().unwrap();
        assert!(status.is_some(), "the child the stop reported gone is gone");
        #[cfg(unix)]
        {
            use std::os::unix::process::ExitStatusExt;
            let signal = status.unwrap().signal();
            assert_eq!(signal, Some(libc::SIGTERM), "asked to stop, not killed");
        }
        assert!(listener(port).is_none(), "nothing holds the port");
    }

    #[test]
    fn a_process_of_another_user_is_not_this_users() {
        // pid 1 runs as root, and the System process, pid 4, as SYSTEM. A test run as root
        // runs as pid 1's user, so the row says nothing then and is skipped.
        #[cfg(any(target_os = "linux", target_os = "macos"))]
        if unsafe { libc::geteuid() } != 0 {
            assert!(!same_user(1), "pid 1");
        }
        #[cfg(windows)]
        {
            use std::os::windows::io::{FromRawHandle, OwnedHandle};
            use windows_sys::Win32::System::Threading::{
                OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION,
            };
            let raw = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, 4) };
            assert!(!raw.is_null(), "the System process can be asked about");
            let system = unsafe { OwnedHandle::from_raw_handle(raw) };
            assert!(!win::same_user(&system), "the System process");
        }
    }

    #[test]
    fn a_command_that_does_not_end_is_given_up_within_its_wait() {
        let start = std::time::Instant::now();
        let out = output_within(&mut me("listener"), Duration::from_millis(300));
        assert_eq!(out, None);
        assert!(
            start.elapsed() < Duration::from_secs(5),
            "{:?}",
            start.elapsed()
        );
    }

    #[cfg(unix)]
    #[test]
    fn a_tree_is_interrupted_and_killed_whole() {
        // (label, what is done to the tree, the leader's signal)
        let rows: &[(&str, fn(&Tree) -> bool, libc::c_int)] = &[
            ("interrupt", Tree::interrupt, libc::SIGTERM),
            (
                "kill",
                |t| {
                    t.kill();
                    true
                },
                libc::SIGKILL,
            ),
        ];
        for (label, act, want) in rows {
            let (mut child, tree) = spawn(command("sleep").arg("30")).unwrap();
            assert!(act(&tree), "{label}");
            use std::os::unix::process::ExitStatusExt;
            assert_eq!(child.wait().unwrap().signal(), Some(*want), "{label}");
        }
    }

    #[cfg(windows)]
    mod windows {
        use super::*;
        use std::os::windows::io::AsRawHandle;
        use windows_sys::Win32::Foundation::WAIT_OBJECT_0;
        use windows_sys::Win32::System::JobObjects::IsProcessInJob;
        use windows_sys::Win32::System::Threading::{GetCurrentProcess, WaitForSingleObject};

        fn in_job(process: windows_sys::Win32::Foundation::HANDLE, tree: &Tree) -> bool {
            let mut result = 0;
            assert_ne!(
                unsafe { IsProcessInJob(process, tree.job.raw(), &mut result) },
                0
            );
            result != 0
        }

        #[test]
        fn only_the_sidecar_and_its_children_are_in_its_job() {
            let mut sidecar = me("sidecar");
            let (mut child, tree) = spawn(sidecar.stdout(Stdio::piped())).unwrap();
            let pids = line_after(&mut child, "pids ");
            let grandchild: u32 = pids.split(' ').nth(1).unwrap().parse().unwrap();
            let grandchild = win::open(grandchild).unwrap();
            let mut other = me("child").spawn().unwrap();
            // The installer the updater starts is the shell's own child, as `other` is.
            assert!(in_job(child.as_raw_handle(), &tree), "the sidecar");
            assert!(
                in_job(grandchild.as_raw_handle(), &tree),
                "a child the sidecar started"
            );
            assert!(!in_job(unsafe { GetCurrentProcess() }, &tree), "the shell");
            assert!(
                !in_job(other.as_raw_handle(), &tree),
                "another child of the shell"
            );
            tree.kill();
            other.kill().unwrap();
            assert!(child.wait().is_ok());
        }

        #[test]
        fn a_sidecar_and_its_children_end_when_the_shell_is_killed() {
            // (the shell's role, whether its sidecar's tree outlives it). The second row is
            // the start without the job, and shows this kill leaves a tree behind.
            for (role, outlives) in [("shell", false), ("shell-without-job", true)] {
                let mut shell = me(role).stdout(Stdio::piped()).spawn().unwrap();
                let pids = line_after(&mut shell, "pids ");
                let tree: Vec<_> = pids
                    .split(' ')
                    .map(|p| win::open(p.parse().unwrap()).unwrap())
                    .collect();
                // TerminateProcess: no handler of the shell runs.
                shell.kill().unwrap();
                shell.wait().unwrap();
                for (process, what) in tree.iter().zip(["the sidecar", "its child"]) {
                    let ended = unsafe { WaitForSingleObject(process.as_raw_handle(), 5_000) }
                        == WAIT_OBJECT_0;
                    assert_eq!(ended, !outlives, "{role}: {what}");
                    win::terminate(process, Duration::from_secs(5));
                }
            }
        }

        #[test]
        fn a_sidecar_hears_the_stop() {
            // The test binary has a console, and only a process without one can attach to
            // the sidecar's, as the shell does.
            unsafe { windows_sys::Win32::System::Console::FreeConsole() };
            let script = "import signal, sys, time\n\
                          signal.signal(signal.SIGBREAK, lambda *a: sys.exit(7))\n\
                          print('ready', flush=True)\n\
                          while True: time.sleep(0.05)\n";
            let mut python = command("python");
            python.args(["-c", script]).stdout(Stdio::piped());
            let (mut child, tree) = spawn(&mut python).unwrap();
            line_after(&mut child, "ready");
            assert!(tree.interrupt(), "the stop was sent");
            let end = std::time::Instant::now() + Duration::from_secs(5);
            let status = loop {
                if let Some(status) = child.try_wait().unwrap() {
                    break status;
                }
                assert!(std::time::Instant::now() < end, "the sidecar did not stop");
                std::thread::sleep(Duration::from_millis(50));
            };
            assert_eq!(status.code(), Some(7), "its handler ran");
        }
    }
}
