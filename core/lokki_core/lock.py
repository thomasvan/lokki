"""The run-log lock, one way on each OS family.

`core.acquire_runs_lock` keeps a lock file open for the life of the process so a
second writer of one run log is refused at startup. POSIX has `flock`; Windows
has `msvcrt.locking`, which locks a byte range from the file position, so the
first byte is the lock (a region past the end of an empty file can be locked).
Both are non-blocking: the caller reports, never waits.
"""
import os

if os.name == "nt":
    import msvcrt

    def acquire(fd):
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def release(fd):
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl

    def acquire(fd):
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def release(fd):
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
