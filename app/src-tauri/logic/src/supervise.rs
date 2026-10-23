use crate::Result;
use std::time::Duration;

#[derive(Clone, Debug)]
pub struct Policy {
    pub delays: [Duration; 3],
    pub window: Duration,
}

impl Default for Policy {
    /// Delays of 1, 2 and 4 seconds; a window of 60 seconds.
    fn default() -> Policy {
        Policy {
            delays: [
                Duration::from_secs(1),
                Duration::from_secs(2),
                Duration::from_secs(4),
            ],
            window: Duration::from_secs(60),
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Action {
    Restart(Duration),
    GiveUp,
    /// The exit was one `expect_stop` announced: nothing to do.
    Expected,
}

/// Decides, from the exits it is told about, whether a sidecar restarts. `now` is the time
/// since any fixed start.
pub struct Supervisor {
    name: String,
    policy: Policy,
    exits: Vec<Duration>,
    expecting_stop: bool,
}

impl Supervisor {
    pub fn new(name: &str, policy: Policy) -> Supervisor {
        Supervisor {
            name: name.to_string(),
            policy,
            exits: Vec::new(),
            expecting_stop: false,
        }
    }

    pub fn name(&self) -> &str {
        &self.name
    }

    /// The first, second and third exit inside the window restart after the policy's delays;
    /// a fourth gives up. Exits older than the window are forgotten.
    pub fn on_exit(&mut self, now: Duration) -> Action {
        if std::mem::take(&mut self.expecting_stop) {
            return Action::Expected;
        }
        let window = self.policy.window;
        self.exits.retain(|at| now.saturating_sub(*at) < window);
        let action = match self.policy.delays.get(self.exits.len()) {
            Some(after) => Action::Restart(*after),
            None => Action::GiveUp,
        };
        self.exits.push(now);
        action
    }

    /// The next exit is the shell's own and counts for nothing.
    pub fn expect_stop(&mut self) {
        self.expecting_stop = true;
    }
}

pub trait ShutdownHttp {
    /// `POST /api/shutdown` with `X-Relay-Token`: the status and the body's `drain_seconds`.
    fn post_shutdown(&self, token: &str) -> Result<(u16, Option<f64>)>;
}

pub trait Child {
    /// Whether the process has exited.
    fn try_wait(&mut self) -> bool;
    /// SIGTERM to the group (`CTRL_BREAK_EVENT` to its console process group on Windows).
    fn terminate(&mut self);
    /// The whole process tree.
    fn kill_tree(&mut self);
}

pub trait Clock {
    fn now(&self) -> Duration;
    fn sleep(&self, d: Duration);
}

/// What the log line says about a stop.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Stopped {
    pub drained: bool,
    pub killed: bool,
}

const POLL: Duration = Duration::from_millis(100);
/// The grace after `drain_seconds` (spec 6.1).
const DRAIN_GRACE_SECS: f64 = 5.0;
/// The most a stop waits for a drain: a longer `drain_seconds` is not a drain, and an
/// unbounded one would panic `Duration` or hold a quit for hours.
const MAX_DRAIN_SECS: f64 = 3600.0;
/// How long to wait for a core whose drain time is unknown, for want of a usable
/// `drain_seconds` or because it refused the order and SIGTERM starts the drain: the
/// schema's default `drain_seconds` (15) plus the grace.
const UNKNOWN_DRAIN_WAIT: Duration = Duration::from_secs(15 + DRAIN_GRACE_SECS as u64);
pub const CLIPROXY_WAIT: Duration = Duration::from_secs(5);

/// Polls until the child exits or `limit` has passed on `clock`; true when it exited.
fn exited_within(child: &mut impl Child, clock: &impl Clock, limit: Duration) -> bool {
    let end = clock.now() + limit;
    loop {
        if child.try_wait() {
            return true;
        }
        if clock.now() >= end {
            return false;
        }
        clock.sleep(POLL);
    }
}

fn kill_unless_exited(child: &mut impl Child, clock: &impl Clock, limit: Duration) -> Stopped {
    if exited_within(child, clock, limit) {
        return Stopped {
            drained: true,
            killed: false,
        };
    }
    child.kill_tree();
    Stopped {
        drained: false,
        killed: true,
    }
}

/// How long a core that took the order to drain has: its `drain_seconds` and the grace,
/// or `UNKNOWN_DRAIN_WAIT` for want of a usable `drain_seconds`.
pub fn drain_wait(drain: Option<f64>) -> Duration {
    match drain.filter(|d| d.is_finite() && *d >= 0.0) {
        Some(d) => Duration::from_secs_f64(d.min(MAX_DRAIN_SECS) + DRAIN_GRACE_SECS),
        None => UNKNOWN_DRAIN_WAIT,
    }
}

/// Orders the core to drain with `POST /api/shutdown`, and kills it only once
/// `drain_seconds + 5` s from the 202 have passed. Any other answer means it is alive
/// but did not take the order, so SIGTERM starts its drain; no answer means nothing
/// is serving, so there is nothing to wait for.
pub fn stop_core(
    http: &impl ShutdownHttp,
    token: &str,
    child: &mut impl Child,
    clock: &impl Clock,
) -> Stopped {
    match http.post_shutdown(token) {
        Ok((202, drain)) => kill_unless_exited(child, clock, drain_wait(drain)),
        Ok(_) => {
            child.terminate();
            kill_unless_exited(child, clock, UNKNOWN_DRAIN_WAIT)
        }
        Err(_) => {
            child.kill_tree();
            Stopped {
                drained: false,
                killed: true,
            }
        }
    }
}

pub fn stop_cliproxy(child: &mut impl Child, clock: &impl Clock) -> Stopped {
    child.terminate();
    kill_unless_exited(child, clock, CLIPROXY_WAIT)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::{Cell, RefCell};
    use std::rc::Rc;

    fn s(n: u64) -> Duration {
        Duration::from_secs(n)
    }

    #[test]
    fn a_fourth_exit_in_a_minute_gives_up() {
        use Action::{GiveUp, Restart};
        // The window alone is the rule: exits older than it are forgotten.
        let rows: &[(&[u64], &[Action])] = &[
            (
                &[0, 2, 6, 10],
                &[Restart(s(1)), Restart(s(2)), Restart(s(4)), GiveUp],
            ),
            (
                &[0, 2, 6, 70],
                &[Restart(s(1)), Restart(s(2)), Restart(s(4)), Restart(s(1))],
            ),
            (
                &[0, 30, 61, 62],
                &[Restart(s(1)), Restart(s(2)), Restart(s(2)), Restart(s(4))],
            ),
        ];
        for (times, want) in rows {
            let mut sup = Supervisor::new("core", Policy::default());
            let got: Vec<Action> = times.iter().map(|t| sup.on_exit(s(*t))).collect();
            assert_eq!(&got, want, "exits at {times:?}");
        }
    }

    #[test]
    fn an_expected_stop_counts_for_nothing() {
        let mut sup = Supervisor::new("core", Policy::default());
        assert_eq!(sup.on_exit(s(0)), Action::Restart(s(1)));
        assert_eq!(sup.on_exit(s(1)), Action::Restart(s(2)));
        sup.expect_stop();
        assert_eq!(sup.on_exit(s(2)), Action::Expected);
        // Counted, this would be the fourth exit; it is the third, and only the next one.
        assert_eq!(sup.on_exit(s(3)), Action::Restart(s(4)));
    }

    // A fake clock, child and HTTP sharing one timeline and one event log.
    #[derive(Clone, Default)]
    struct Rig {
        t: Rc<Cell<Duration>>,
        log: Rc<RefCell<Vec<(String, Duration)>>>,
        polls: Rc<Cell<u32>>,
    }

    impl Rig {
        fn event(&self, what: impl Into<String>) {
            self.log.borrow_mut().push((what.into(), self.t.get()));
        }
        fn child(&self, exits_at: Option<Duration>) -> FakeChild {
            FakeChild {
                rig: self.clone(),
                exits_at,
            }
        }
    }

    impl Clock for Rig {
        fn now(&self) -> Duration {
            self.t.get()
        }
        fn sleep(&self, d: Duration) {
            self.t.set(self.t.get() + d);
        }
    }

    struct FakeChild {
        rig: Rig,
        exits_at: Option<Duration>,
    }

    impl Child for FakeChild {
        fn try_wait(&mut self) -> bool {
            self.rig.polls.set(self.rig.polls.get() + 1);
            assert!(self.rig.polls.get() < 100_000, "polled without sleeping");
            self.exits_at.is_some_and(|at| self.rig.t.get() >= at)
        }
        fn terminate(&mut self) {
            self.rig.event("terminate");
        }
        fn kill_tree(&mut self) {
            self.rig.event("kill");
        }
    }

    struct FakeHttp {
        rig: Rig,
        answer: Result<(u16, Option<f64>)>,
    }

    impl ShutdownHttp for FakeHttp {
        fn post_shutdown(&self, token: &str) -> Result<(u16, Option<f64>)> {
            self.rig.event(format!("post {token}"));
            self.answer.clone()
        }
    }

    /// The events as `(name, time)`; a kill or terminate may come up to one second after its
    /// deadline (a poll step), never before.
    fn assert_events(rig: &Rig, want: &[(&str, Duration)], label: &str) {
        let got = rig.log.borrow();
        assert_eq!(
            got.iter().map(|(n, _)| n.as_str()).collect::<Vec<_>>(),
            want.iter().map(|(n, _)| *n).collect::<Vec<_>>(),
            "{label}: {got:?}"
        );
        for ((name, at), (_, due)) in got.iter().zip(want) {
            assert!(
                at >= due && *at < *due + s(1),
                "{label}: {name} at {at:?}, due {due:?}"
            );
        }
    }

    #[test]
    fn the_core_is_drained_before_it_is_killed() {
        let tok = "tok-test";
        let post = format!("post {tok}");
        let p = post.as_str();
        // (label, the answer, when the child exits, events, drained, killed); `drained` is None
        // where the row binds only whether the child was killed.
        type DrainRow<'a> = (
            &'a str,
            Result<(u16, Option<f64>)>,
            Option<u64>,
            Vec<(&'a str, Duration)>,
            Option<bool>,
            bool,
        );
        let rows: Vec<DrainRow> = vec![
            (
                "202, exits while draining",
                Ok((202, Some(3.0))),
                Some(2),
                vec![(p, s(0))],
                Some(true),
                false,
            ),
            (
                "202, never exits: killed at drain + 5",
                Ok((202, Some(3.0))),
                None,
                vec![(p, s(0)), ("kill", s(8))],
                Some(false),
                true,
            ),
            (
                "202, exits at the last second before the kill",
                Ok((202, Some(3.0))),
                Some(7),
                vec![(p, s(0))],
                Some(true),
                false,
            ),
            // No usable drain_seconds: wait as long as for a refused order, and never panic on the number.
            (
                "202 without drain_seconds",
                Ok((202, None)),
                None,
                vec![(p, s(0)), ("kill", s(20))],
                Some(false),
                true,
            ),
            (
                "202 with a negative drain_seconds",
                Ok((202, Some(-1.0))),
                None,
                vec![(p, s(0)), ("kill", s(20))],
                Some(false),
                true,
            ),
            (
                "202 with a NaN drain_seconds",
                Ok((202, Some(f64::NAN))),
                None,
                vec![(p, s(0)), ("kill", s(20))],
                Some(false),
                true,
            ),
            (
                "202 with a huge drain_seconds: capped at an hour",
                Ok((202, Some(1e300))),
                None,
                vec![(p, s(0)), ("kill", s(3605))],
                Some(false),
                true,
            ),
            (
                "401: terminate, then kill at 20 s",
                Ok((401, None)),
                None,
                vec![(p, s(0)), ("terminate", s(0)), ("kill", s(20))],
                Some(false),
                true,
            ),
            (
                "500: the same",
                Ok((500, None)),
                None,
                vec![(p, s(0)), ("terminate", s(0)), ("kill", s(20))],
                Some(false),
                true,
            ),
            (
                "401, exits after the terminate",
                Ok((401, None)),
                Some(3),
                vec![(p, s(0)), ("terminate", s(0))],
                None,
                false,
            ),
            (
                "transport failure: nothing serving, kill at once",
                Err("connection refused".into()),
                None,
                vec![(p, s(0)), ("kill", s(0))],
                Some(false),
                true,
            ),
        ];
        for (label, answer, exits_at, events, drained, killed) in rows {
            let rig = Rig::default();
            let http = FakeHttp {
                rig: rig.clone(),
                answer,
            };
            let mut child = rig.child(exits_at.map(s));
            let stopped = stop_core(&http, tok, &mut child, &rig);
            assert_events(&rig, &events, label);
            assert_eq!(stopped.killed, killed, "{label}");
            if let Some(d) = drained {
                assert_eq!(stopped.drained, d, "{label}");
            }
        }
    }

    #[test]
    fn cliproxy_is_terminated_then_killed() {
        for (label, exits_at, events, killed) in [
            (
                "exits on the signal",
                Some(s(1)),
                vec![("terminate", s(0))],
                false,
            ),
            (
                "holds on: killed after 5 s",
                None,
                vec![("terminate", s(0)), ("kill", s(5))],
                true,
            ),
        ] {
            let rig = Rig::default();
            let mut child = rig.child(exits_at);
            let stopped = stop_cliproxy(&mut child, &rig);
            assert_events(&rig, &events, label);
            assert_eq!(stopped.killed, killed, "{label}");
        }
    }
}
