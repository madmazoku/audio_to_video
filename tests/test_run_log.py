"""Console mirroring, exception retention and child-process diagnostics."""
from contextlib import redirect_stdout, redirect_stderr
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from aligned_song_video_runner import run_cmd
from core.comfyui import log as comfy_log
from core.run_log import mirror_console


class RunLogTests(unittest.TestCase):
    def test_live_stdout_stderr_and_generator_messages_share_one_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "logs/run.log"
            stdout, stderr = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                with mirror_console(path):
                    print("captain")
                    print("warning", file=sys.stderr)
                    comfy_log("sampler progress 1/2")
                    # No context exit is needed to read the latest output.
                    content = path.read_text(encoding="utf-8")
                    self.assertIn("captain\nwarning\n", content)
                    self.assertIn("sampler progress 1/2", content)
                self.assertIs(sys.stdout, stdout)
                self.assertIs(sys.stderr, stderr)
            self.assertIn("sampler progress 1/2", stdout.getvalue())
            self.assertEqual(stderr.getvalue(), "warning\n")

    def test_failure_is_saved_and_original_streams_are_restored(self):
        for error in (RuntimeError("GPU failed"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "run.log"
                stdout, stderr = sys.stdout, sys.stderr
                with self.assertRaises(type(error)):
                    with mirror_console(path):
                        raise error
                self.assertIs(sys.stdout, stdout)
                self.assertIs(sys.stderr, stderr)
                self.assertIn(type(error).__name__, path.read_text(encoding="utf-8"))

    def test_utf8_and_next_run_overwrites_existing_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.log"
            with redirect_stdout(io.StringIO()), mirror_console(path):
                print("\u0422\u043e\u0440\u0433\u043e\u0432\u0435\u0446")
            content = path.read_text(encoding="utf-8")
            self.assertIn("\u0422\u043e\u0440\u0433\u043e\u0432\u0435\u0446", content)
            with redirect_stdout(io.StringIO()), mirror_console(path):
                print("next run")
            self.assertEqual(path.read_text(encoding="utf-8"), "next run\n")

    def test_child_process_output_and_nonzero_exit_are_retained(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.log"
            terminal = io.StringIO()
            with redirect_stdout(terminal):
                with self.assertRaises(subprocess.CalledProcessError):
                    with mirror_console(path):
                        run_cmd([sys.executable, "-c",
                                 "import sys; print('child-out', flush=True); print('child-error', file=sys.stderr, flush=True); sys.exit(2)"])
            content = path.read_text(encoding="utf-8")
            self.assertIn("child-out\nchild-error\n", content)
            self.assertIn("CalledProcessError", content)
            self.assertIn("child-out\nchild-error\n", terminal.getvalue())


if __name__ == "__main__":
    unittest.main()
