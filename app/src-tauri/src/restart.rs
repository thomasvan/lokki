//! A restart, and the way back when a moved port does not come up: the previous port is
//! written back, the restart runs again on it, and the one who asked is told both.
use crate::sidecars::Service;
use lokki_logic::firstrun::Config;
use lokki_logic::Result;

/// What a restart does: the shell's sidecars and the core's subcommands, or a test's record.
pub trait Ops {
    fn stop(&mut self, conf: &Config, service: Service);
    /// `shell-config` again, or `before` when it fails.
    fn reread(&mut self, before: &Config) -> Config;
    /// Starts one sidecar and waits until it is ready.
    fn start(&mut self, conf: &Config, service: Service) -> Result<()>;
    /// `set-port`: the service's port in the settings, back to `port`.
    fn set_port(&mut self, service: Service, port: u16) -> Result<()>;
}

#[derive(Debug, PartialEq)]
pub enum Outcome {
    /// What was restarted is ready, on the ports the `Config` names.
    Ready(Config),
    /// A start failed on a port that did not move; nothing was put back.
    Failed(String),
    /// A moved port did not come up, and Lokki is back on the previous one: the sentence
    /// says both.
    WentBack(Config, String),
    /// A moved port did not come up and the way back failed too; `port` is the one to put
    /// back by hand.
    Stuck {
        service: Service,
        port: u16,
        sentence: String,
    },
}

/// What a restart of `service` starts, in order: CLIProxyAPI before the core, which reads
/// CLIProxyAPI's address only at start.
fn services(service: Service) -> &'static [Service] {
    match service {
        Service::Cliproxy => &[Service::Cliproxy, Service::Core],
        Service::Core => &[Service::Core],
    }
}

/// Stops what the restart covers, the core first, reads the settings again and starts it
/// all. The core starts even when CLIProxyAPI did not: it serves the UI that says so.
fn cycle(
    ops: &mut impl Ops,
    before: &Config,
    service: Service,
) -> (Config, Vec<(Service, String)>) {
    for &s in services(service).iter().rev() {
        ops.stop(before, s);
    }
    let conf = ops.reread(before);
    let failed = services(service)
        .iter()
        .filter_map(|&s| ops.start(&conf, s).err().map(|e| (s, e)))
        .collect();
    (conf, failed)
}

pub fn restart(ops: &mut impl Ops, before: &Config, service: Service) -> Outcome {
    let (conf, failed) = cycle(ops, before, service);
    let Some((_, first)) = failed.first() else {
        return Outcome::Ready(conf);
    };
    let moved: Vec<(Service, u16, &String)> = failed
        .iter()
        .filter(|(s, _)| s.port(&conf) != s.port(before))
        .map(|(s, e)| (*s, s.port(before), e))
        .collect();
    if moved.is_empty() {
        return Outcome::Failed(first.clone());
    }
    let what = moved
        .iter()
        .map(|(_, _, e)| e.as_str())
        .collect::<Vec<_>>()
        .join("; ");
    let ports = moved
        .iter()
        .map(|(_, port, _)| port.to_string())
        .collect::<Vec<_>>()
        .join(" and ");
    for &(s, port, _) in &moved {
        if let Err(e) = ops.set_port(s, port) {
            return Outcome::Stuck {
                service: s,
                port,
                sentence: format!("{what}; Lokki could not go back to port {port}: {e}"),
            };
        }
    }
    let (back, failed) = cycle(ops, &conf, service);
    match failed.first() {
        None => Outcome::WentBack(back, format!("{what}; Lokki is back on port {ports}")),
        Some((s, e)) => Outcome::Stuck {
            service: *s,
            port: s.port(before),
            sentence: format!("{what}; on the way back to port {ports}, {e}"),
        },
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashSet;
    use std::path::PathBuf;

    fn conf(core_port: u16, cliproxy_port: u16) -> Config {
        Config {
            core_port,
            cliproxy_port,
            operator_token: String::new(),
            management_secret_file: PathBuf::new(),
            panel: false,
        }
    }

    /// The settings file as two ports, and the ports on which a start fails.
    struct Fake {
        file: Config,
        taken: HashSet<u16>,
        set_port_fails: bool,
        calls: Vec<String>,
    }

    impl Ops for Fake {
        fn stop(&mut self, conf: &Config, service: Service) {
            self.calls
                .push(format!("stop {} {}", service.name(), service.port(conf)));
        }
        fn reread(&mut self, _: &Config) -> Config {
            self.file.clone()
        }
        fn start(&mut self, conf: &Config, service: Service) -> Result<()> {
            let port = service.port(conf);
            self.calls.push(format!("start {} {port}", service.name()));
            if self.taken.contains(&port) {
                return Err(format!("port {port} is taken"));
            }
            Ok(())
        }
        fn set_port(&mut self, service: Service, port: u16) -> Result<()> {
            self.calls.push(format!("set-port {} {port}", service.name()));
            if self.set_port_fails {
                return Err("the file is read-only".into());
            }
            match service {
                Service::Core => self.file.core_port = port,
                Service::Cliproxy => self.file.cliproxy_port = port,
            }
            Ok(())
        }
    }

    #[test]
    fn a_moved_port_that_does_not_come_up_goes_back_to_the_previous_one() {
        let running = conf(1001, 2001);
        // (label, service, the file, ports taken, set-port fails, outcome, calls)
        type Row = (
            &'static str,
            Service,
            Config,
            &'static [u16],
            bool,
            Outcome,
            &'static [&'static str],
        );
        let rows: [Row; 7] = [
            (
                "the core comes up on its new port",
                Service::Core,
                conf(1002, 2001),
                &[],
                false,
                Outcome::Ready(conf(1002, 2001)),
                &["stop core 1001", "start core 1002"],
            ),
            (
                "the core's new port is taken: back on the old one",
                Service::Core,
                conf(1002, 2001),
                &[1002],
                false,
                Outcome::WentBack(
                    conf(1001, 2001),
                    "port 1002 is taken; Lokki is back on port 1001".into(),
                ),
                &[
                    "stop core 1001",
                    "start core 1002",
                    "set-port core 1001",
                    "stop core 1002",
                    "start core 1001",
                ],
            ),
            (
                "CLIProxyAPI's new port is taken: both restart on the old one",
                Service::Cliproxy,
                conf(1001, 2002),
                &[2002],
                false,
                Outcome::WentBack(
                    conf(1001, 2001),
                    "port 2002 is taken; Lokki is back on port 2001".into(),
                ),
                &[
                    "stop core 1001",
                    "stop cliproxy 2001",
                    "start cliproxy 2002",
                    "start core 1001",
                    "set-port cliproxy 2001",
                    "stop core 1001",
                    "stop cliproxy 2002",
                    "start cliproxy 2001",
                    "start core 1001",
                ],
            ),
            (
                "a failure on a port that did not move puts nothing back",
                Service::Core,
                conf(1001, 2001),
                &[1001],
                false,
                Outcome::Failed("port 1001 is taken".into()),
                &["stop core 1001", "start core 1001"],
            ),
            (
                "the core moved but CLIProxyAPI failed on its own port: nothing put back",
                Service::Cliproxy,
                conf(1002, 2001),
                &[2001],
                false,
                Outcome::Failed("port 2001 is taken".into()),
                &[
                    "stop core 1001",
                    "stop cliproxy 2001",
                    "start cliproxy 2001",
                    "start core 1002",
                ],
            ),
            (
                "the port cannot be written back",
                Service::Core,
                conf(1002, 2001),
                &[1002],
                true,
                Outcome::Stuck {
                    service: Service::Core,
                    port: 1001,
                    sentence: "port 1002 is taken; Lokki could not go back to port 1001: \
                               the file is read-only"
                        .into(),
                },
                &["stop core 1001", "start core 1002", "set-port core 1001"],
            ),
            (
                "the old port is taken too",
                Service::Core,
                conf(1002, 2001),
                &[1001, 1002],
                false,
                Outcome::Stuck {
                    service: Service::Core,
                    port: 1001,
                    sentence: "port 1002 is taken; on the way back to port 1001, \
                               port 1001 is taken"
                        .into(),
                },
                &[
                    "stop core 1001",
                    "start core 1002",
                    "set-port core 1001",
                    "stop core 1002",
                    "start core 1001",
                ],
            ),
        ];
        for (label, service, file, taken, set_port_fails, want, calls) in rows {
            let mut ops = Fake {
                file,
                taken: taken.iter().copied().collect(),
                set_port_fails,
                calls: Vec::new(),
            };
            assert_eq!(restart(&mut ops, &running, service), want, "{label}");
            assert_eq!(ops.calls, calls, "{label}");
        }
    }
}
