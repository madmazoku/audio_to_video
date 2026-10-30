"""Mirror terminal output to a UTF-8 file for the duration of a runner call."""
from contextlib import contextmanager
from pathlib import Path
import sys
from threading import RLock
import traceback


class TeeStream:
    def __init__(self, terminal, log_file, lock):
        self.terminal, self.log_file, self.lock = terminal, log_file, lock

    def write(self, text):
        with self.lock:
            self.terminal.write(text)
            self.log_file.write(text)
            self.flush()
        return len(text)

    def flush(self):
        with self.lock:
            self.terminal.flush()
            self.log_file.flush()

    def __getattr__(self, name):
        return getattr(self.terminal, name)


@contextmanager
def mirror_console(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    stdout, stderr = sys.stdout, sys.stderr
    lock = RLock()
    with path.open("w", encoding="utf-8", newline="") as log_file:
        sys.stdout = TeeStream(stdout, log_file, lock)
        sys.stderr = TeeStream(stderr, log_file, lock)
        try:
            yield
        except BaseException:
            # The top-level interpreter prints the traceback after streams have
            # been restored. Save it here without duplicating it in the terminal.
            traceback.print_exc(file=log_file)
            raise
        finally:
            try:
                sys.stdout.flush()
                sys.stderr.flush()
            finally:
                sys.stdout, sys.stderr = stdout, stderr
