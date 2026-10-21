use std::fs::{self, File, OpenOptions};
use std::io::{self, Write};
use std::path::PathBuf;

/// The cap at which a sidecar log rotates.
pub const CAP: u64 = 5 * 1024 * 1024;

/// A log file that, once past its cap, is renamed to `<path>.1` (one previous file is kept).
pub struct Rotating {
    path: PathBuf,
    cap: u64,
    file: File,
    size: u64,
}

fn open(path: &PathBuf) -> io::Result<(File, u64)> {
    let file = OpenOptions::new().create(true).append(true).open(path)?;
    let size = file.metadata()?.len();
    Ok((file, size))
}

impl Rotating {
    pub fn new(path: impl Into<PathBuf>, cap: u64) -> io::Result<Rotating> {
        let path = path.into();
        let (file, size) = open(&path)?;
        Ok(Rotating {
            path,
            cap,
            file,
            size,
        })
    }

    fn rotate(&mut self) -> io::Result<()> {
        let mut previous = self.path.clone().into_os_string();
        previous.push(".1");
        // Windows refuses a rename onto an existing file.
        let _ = fs::remove_file(&previous);
        fs::rename(&self.path, &previous)?;
        (self.file, self.size) = open(&self.path)?;
        Ok(())
    }
}

impl Write for Rotating {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        // A write is never split: the one that would pass the cap starts a fresh file.
        if self.size > 0 && self.size + buf.len() as u64 > self.cap {
            self.rotate()?;
        }
        self.file.write_all(buf)?;
        self.size += buf.len() as u64;
        Ok(buf.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        self.file.flush()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    #[test]
    fn rotation_keeps_one_previous_file() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("core.log");
        let previous = dir.path().join("core.log.1");
        let cap = 25;
        let lines: Vec<String> = (0..10).map(|i| format!("line-{i:03}\n")).collect(); // 9 bytes each
        let mut log = Rotating::new(&path, cap).unwrap();
        for l in &lines {
            log.write_all(l.as_bytes()).unwrap();
        }
        log.flush().unwrap();

        let live = fs::read_to_string(&path).unwrap_or_default();
        let old = fs::read_to_string(&previous).unwrap();
        // Nothing but core.log and core.log.1 exists, each within the cap.
        for entry in fs::read_dir(dir.path()).unwrap() {
            let name = entry.unwrap().file_name().to_string_lossy().into_owned();
            assert!(
                name == "core.log" || name == "core.log.1",
                "unexpected file {name}"
            );
        }
        assert!(
            live.len() as u64 <= cap && old.len() as u64 <= cap,
            "live {} old {}",
            live.len(),
            old.len()
        );
        // What is kept is the newest text, in order, with the oldest dropped.
        let all = lines.concat();
        assert!(
            all.ends_with(&format!("{old}{live}")),
            "the two files are not the tail of what was written"
        );
        assert!(all.ends_with("line-009\n") && !format!("{old}{live}").contains("line-000"));
    }
}
