"""Windows launcher integration checks with owned fake servers, never company data.

Run: python -m unittest scripts.tests_launcher -v
"""

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
PWSH = (
    Path.home()
    / ".cache/codex-runtimes/codex-primary-runtime/dependencies/native/powershell/pwsh.exe"
)
SERVER = """import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
if sys.argv[1] != "runserver":
    with (Path(__file__).parent / ".local" / "manage-calls.txt").open("a") as log:
        log.write(sys.argv[1] + "\\n")
    raise SystemExit(0)
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')
    def log_message(self, *args):
        pass
HTTPServer(("127.0.0.1", int(sys.argv[2].split(":")[1])), Handler).serve_forever()
"""


@unittest.skipUnless(os.name == "nt", "Windows PowerShell launcher")
class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="osee-launcher-")
        self.root = Path(self.temp.name)
        (self.root / "scripts").mkdir()
        (self.root / ".local").mkdir()
        shutil.copyfile(
            ROOT / "scripts" / "start.ps1", self.root / "scripts" / "start.ps1"
        )
        (self.root / "manage.py").write_text(SERVER, encoding="utf-8")
        (self.root / "foreign.py").write_text(SERVER, encoding="utf-8")
        (self.root / "requirements.txt").write_bytes(b"")
        (self.root / ".local" / "requirements.sha256").write_text(
            hashlib.sha256(b"").hexdigest()
        )
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", str(self.root / ".venv")],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self.port = self.free_port_block()
        self.foreign = subprocess.Popen(
            [
                sys._base_executable,
                str(self.root / "foreign.py"),
                "runserver",
                f"127.0.0.1:{self.port}",
                "--noreload",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self.wait_health(self.port)

    @staticmethod
    def free_port_block():
        # Reserve a free block briefly; no real local service is replaced.
        for _ in range(40):
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            if port > 65525:
                continue
            reservations = []
            try:
                for candidate in range(port, port + 11):
                    item = socket.socket()
                    reservations.append(item)
                    item.bind(("127.0.0.1", candidate))
                return port
            except OSError:
                continue
            finally:
                for item in reservations:
                    item.close()
        raise RuntimeError("No free fixture port block")

    @staticmethod
    def wait_health(port):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            try:
                with urlopen(f"http://127.0.0.1:{port}/health/", timeout=1) as response:
                    if json.load(response)["status"] == "ok":
                        return
            except OSError:
                time.sleep(0.1)
        raise AssertionError("Owned fixture server did not start")

    def launcher(self, *args, expected=0, shell="powershell.exe"):
        # A background Windows process can inherit pipe handles even after the
        # launcher exits. Files let this check wait for the launcher itself.
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            result = subprocess.run(
                [
                    str(shell),
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(self.root / "scripts" / "start.ps1"),
                    "-Port",
                    str(self.port),
                    *args,
                ],
                stdout=stdout,
                stderr=stderr,
                timeout=55,
                check=False,
            )
            stdout.seek(0)
            stderr.seek(0)
            result.stdout = stdout.read().decode("utf-8", errors="replace")
            result.stderr = stderr.read().decode("utf-8", errors="replace")
        self.assertEqual(
            result.returncode, expected, (result.stdout + result.stderr)[-3000:]
        )
        return result

    def state(self):
        return json.loads(
            (self.root / ".local" / "server-state.json").read_text(encoding="utf-8-sig")
        )

    def tearDown(self):
        try:
            if (self.root / ".local" / "server-state.json").exists():
                self.launcher("-Stop")
        finally:
            self.foreign.terminate()
            self.foreign.wait(timeout=5)
            self.foreign.stderr.close()
            self.temp.cleanup()

    def test_foreign_healthy_port_falls_back_reuses_owned_server_and_stops_only_ours(
        self,
    ):
        result = self.launcher()
        state = self.state()
        self.assertEqual(state["port"], self.port + 1)
        self.assertIn(f"127.0.0.1:{self.port + 1}", result.stdout)
        self.assertNotEqual(state["server"]["pid"], self.foreign.pid)
        self.wait_health(self.port + 1)
        self.launcher()
        self.assertEqual(self.state()["server"]["pid"], state["server"]["pid"])
        self.assertEqual(
            (self.root / ".local" / "manage-calls.txt").read_text().splitlines(),
            ["migrate", "seed_demo"],
        )
        self.launcher("-Status")
        self.launcher("-Stop")
        self.assertIsNone(self.foreign.poll())
        self.wait_health(self.port)
        self.assertFalse((self.root / ".local" / "server-state.json").exists())

    @unittest.skipUnless(PWSH.is_file(), "Bundled PowerShell 7 unavailable")
    def test_powershell5_state_is_reused_and_stopped_by_powershell7(self):
        self.launcher()
        state = self.state()
        self.launcher(shell=PWSH)
        self.assertEqual(self.state()["server"], state["server"])
        self.launcher("-Status", shell=PWSH)
        self.launcher("-Stop", shell=PWSH)
        self.assertFalse((self.root / ".local" / "server-state.json").exists())
        self.assertIsNone(self.foreign.poll())
        self.wait_health(self.port)

    @unittest.skipUnless(PWSH.is_file(), "Bundled PowerShell 7 unavailable")
    def test_powershell7_state_is_reused_and_stopped_by_powershell5(self):
        self.launcher(shell=PWSH)
        state = self.state()
        self.assertEqual(state["port"], self.port + 1)
        self.assertNotEqual(state["server"]["pid"], self.foreign.pid)
        self.launcher()
        self.assertEqual(self.state()["server"], state["server"])
        self.assertEqual(
            (self.root / ".local" / "manage-calls.txt").read_text().splitlines(),
            ["migrate", "seed_demo"],
        )
        self.launcher("-Status")
        self.launcher("-Stop")
        self.assertFalse((self.root / ".local" / "server-state.json").exists())
        self.assertIsNone(self.foreign.poll())
        self.wait_health(self.port)

    @unittest.skipUnless(PWSH.is_file(), "Bundled PowerShell 7 unavailable")
    def test_powershell7_preserves_exact_timestamp_and_rejects_foreign_saved_process(
        self,
    ):
        self.launcher(shell=PWSH)
        path = self.root / ".local" / "server-state.json"
        original = path.read_bytes()
        saved = self.state()
        stamp = saved["server"]["started_at"]
        self.assertRegex(stamp, r"\.\d{7}Z$")
        # One 100 ns tick is a different process identity, even though it looks
        # identical in a UI displaying milliseconds.
        changed_stamp = stamp[:-2] + ("1" if stamp[-2] == "0" else "0") + "Z"
        for mismatch in ("timestamp", "foreign_process"):
            with self.subTest(mismatch=mismatch):
                state = json.loads(original.decode("utf-8-sig"))
                if mismatch == "timestamp":
                    state["server"]["started_at"] = changed_stamp
                else:
                    state["port"] = self.port
                    state["server"]["pid"] = self.foreign.pid
                path.write_text(json.dumps(state), encoding="utf-8")
                try:
                    result = self.launcher("-Stop", shell=PWSH, expected=1)
                    self.assertIn("Identitas proses berubah", result.stderr)
                    self.wait_health(self.port + 1)
                    self.assertIsNone(self.foreign.poll())
                    self.wait_health(self.port)
                finally:
                    path.write_bytes(original)
        self.launcher("-Stop", shell=PWSH)
        self.assertFalse(path.exists())

    def test_stale_process_identity_cannot_kill_owned_or_foreign_server(self):
        self.launcher()
        path = self.root / ".local" / "server-state.json"
        original = path.read_bytes()
        state = self.state()
        state["server"]["started_at"] = "2000-01-01T00:00:00.0000000Z"
        path.write_text(json.dumps(state), encoding="utf-8")
        try:
            result = self.launcher("-Stop", expected=1)
            self.assertIn("Identitas proses berubah", result.stderr)
            self.wait_health(self.port + 1)
            self.assertIsNone(self.foreign.poll())
        finally:
            path.write_bytes(original)

    def test_foreign_script_with_workspace_command_in_trailing_arguments_is_not_ours(
        self,
    ):
        # The script executing is foreign.py. Text later in argv must not become
        # proof of ownership, even when that foreign endpoint reports healthy.
        self.foreign.terminate()
        self.foreign.wait(timeout=5)
        self.foreign.stderr.close()
        self.foreign = subprocess.Popen(
            [
                sys._base_executable,
                str(self.root / "foreign.py"),
                "runserver",
                f"127.0.0.1:{self.port}",
                "--noreload",
                str(self.root / "manage.py"),
                "runserver",
                f"127.0.0.1:{self.port}",
                "--noreload",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self.wait_health(self.port)
        self.launcher()
        state = self.state()
        self.assertEqual(state["port"], self.port + 1)
        self.assertNotEqual(state["server"]["pid"], self.foreign.pid)
        self.launcher("-Stop")
        self.assertIsNone(self.foreign.poll())
        self.wait_health(self.port)
