from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from wechat_lock import acquire_file_lock
from run_wechat_ai import stop_worker


@unittest.skipUnless(sys.platform == "win32", "Windows byte-range locks")
class WindowsLockTests(unittest.TestCase):
    def test_duplicate_supervisor_and_worker_report_running_without_traceback(self):
        for module, filename, setup, call in (
            ("run_wechat_ai", ".wechat_ai_supervisor.lock", "m.ROOT = folder", "m.lock_supervisor()"),
            ("wechat_ai", ".wechat_ai.lock", "m.LOCK_PATH = folder / '.wechat_ai.lock'", "m.acquire_instance_lock()"),
        ):
            with self.subTest(module=module), tempfile.TemporaryDirectory() as directory:
                folder = Path(directory)
                source = (
                    f"from pathlib import Path\nimport {module} as m\n"
                    f"folder=Path({directory!r})\n{setup}\n"
                    f"try:\n {call}\n print('acquired')\n"
                    "except RuntimeError as e:\n print(type(e).__name__)\n"
                )
                handle = acquire_file_lock(folder / filename)
                try:
                    result = subprocess.run([sys.executable, "-c", source], capture_output=True, timeout=10)
                    self.assertEqual(result.returncode, 0)
                    self.assertNotIn(b"acquired", result.stdout)
                    self.assertNotIn(b"PermissionError", result.stdout + result.stderr)
                    self.assertEqual(result.stderr, b"")
                finally:
                    handle.close()
                result = subprocess.run([sys.executable, "-c", source], capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 0)
                self.assertIn(b"acquired", result.stdout)

    def test_stop_waits_for_running_supervisor_to_release_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            holder_source = (
                "from pathlib import Path\nimport time\nfrom wechat_lock import acquire_file_lock\n"
                f"folder=Path({directory!r})\n"
                "handle=acquire_file_lock(folder / '.wechat_ai_supervisor.lock')\n"
                "print('ready',flush=True)\n"
                "end=time.monotonic()+10\n"
                "while not (folder / '.wechat_ai_stop').exists() and time.monotonic()<end:\n time.sleep(.02)\n"
                "handle.close()\n"
            )
            holder = subprocess.Popen([sys.executable, "-u", "-c", holder_source], stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(holder.stdout.readline().strip(), "ready")
                stopper = (
                    "from pathlib import Path\nimport sys\nimport run_wechat_ai as m\n"
                    f"m.ROOT=Path({directory!r})\n"
                    "m.STOP_PATH=m.ROOT / '.wechat_ai_stop'\n"
                    "sys.argv=['run_wechat_ai.py','--stop']\nraise SystemExit(m.main())\n"
                )
                result = subprocess.run([sys.executable, "-c", stopper], capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn(b"Traceback", result.stderr)
                holder.wait(timeout=3)
                self.assertFalse((folder / ".wechat_ai_stop").exists())
            finally:
                stop_worker(holder)
                holder.stdout.close()


if __name__ == "__main__":
    unittest.main()
