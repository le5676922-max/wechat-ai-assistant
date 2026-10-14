import ctypes
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest

from run_wechat_ai import run_worker
from wechat_runtime import AutoReplyRunner, ModelStalled, RuntimeStore, appended_start, decode_rows


def key(number, text):
    return [[42, number], "text", text]


def row(text, friend="12"):
    return {"消息发送人": friend, "消息类型": "文本", "消息内容": text}


class FakeAssistant:
    def __init__(self):
        self.config = SimpleNamespace(allowed_friends=("12",))
        self.auto_send = True
        self.calls = 0
        self.failures = 0
        self.release = None

    def prepare_reply(self, friend, rows, history):
        self.calls += 1
        if self.release:
            self.release.wait(timeout=3)
        if self.calls <= self.failures:
            raise TimeoutError("test network outage")
        prompt = "\n".join(item["消息内容"] for item in rows)
        return prompt, "回复：" + prompt


class FakeUI:
    def __init__(self, store, clock):
        self.store, self.clock = store, clock
        self.polls = 0
        self.incoming = []
        self.sent = []
        self.fail_after_send = False

    def poll(self):
        self.polls += 1
        if self.incoming:
            rows = self.incoming.pop(0)
            self.store.capture("12", [key(self.polls, str(rows))], rows, self.clock())

    def snapshot(self, friend):
        return [key(i, text) for i, text in enumerate(self.sent)]

    def send(self, friend, text, anchor=None):
        current = self.snapshot(friend)
        if any(item[2] == text for item in current[appended_start(anchor or [], current):]):
            return
        self.sent.append(text)
        if self.fail_after_send:
            self.fail_after_send = False
            raise TimeoutError("sent but confirmation interrupted")


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "queue.sqlite3"
        self.store = RuntimeStore(self.path)
        self.now = 100.0
        self.ai = FakeAssistant()
        self.ui = FakeUI(self.store, lambda: self.now)
        self.runner = AutoReplyRunner(self.ai, self.ui, self.store, lambda: self.now)

    def tearDown(self):
        if self.ai.release:
            self.ai.release.set()
        self.runner.executor.shutdown(wait=True, cancel_futures=True)
        self.store.close()
        self.folder.cleanup()

    def spin_until(self, predicate):
        deadline = time.monotonic() + 2
        while not predicate() and time.monotonic() < deadline:
            self.runner.tick()
            time.sleep(0.005)
        self.assertTrue(predicate(), self.store.counts())

    def test_runtime_id_reuse_scroll_and_repeated_text(self):
        old = [key(1, "你好"), key(2, "你好呀")]
        self.assertEqual(appended_start(old, [key(2, "你好呀"), key(1, "人呢")]), 1)
        self.assertEqual(appended_start(old, old + [key(3, "你好")]), 2)
        self.assertEqual(appended_start([key(1, "你好")], [key(7, "你好"), key(8, "人呢")]), 1)
        self.assertEqual(appended_start(old, old), 2)

    def test_queue_and_image_survive_model_process_restart(self):
        image = {"消息发送人": "12", "消息类型": "图片", "图片数据": b"test-picture"}
        snapshot = [key(1, "图片")]
        self.store.capture("12", snapshot, [image], now=100)
        job = self.store.claim(("12",), 101)
        self.store.close()
        self.store = RuntimeStore(self.path)
        recovered = self.store.claim(("12",), 102)
        self.assertEqual(recovered["id"], job["id"])
        self.assertEqual(decode_rows(recovered["rows"])[0]["图片数据"], b"test-picture")
        self.assertEqual(self.store.cursor("12"), snapshot)

    def test_capture_is_atomic_with_cursor(self):
        self.store.db.execute("CREATE TRIGGER fail_cursor BEFORE INSERT ON cursors BEGIN SELECT RAISE(ABORT, 'disk failure'); END")
        with self.assertRaises(Exception):
            self.store.capture("12", [key(1, "你好")], [row("你好")], 100)
        self.assertEqual(self.store.counts(), {})
        self.assertIsNone(self.store.cursor("12"))

    def test_slow_model_does_not_stop_receiving_a_burst(self):
        self.ai.release = threading.Event()
        self.store.capture("12", [key(1, "第一条")], [row("第一条")], now=99)
        self.runner.tick()
        self.ui.incoming = [[row(f"消息{i}")] for i in range(12)]
        for _ in range(12):
            self.runner.tick()
        self.assertEqual(self.ui.polls, 13)
        pending = self.store.db.execute("SELECT rows FROM jobs WHERE status='pending'").fetchall()
        self.assertEqual(sum(len(decode_rows(item[0])) for item in pending), 12)
        self.ai.release.set()
        self.now = 103
        self.spin_until(lambda: self.store.counts().get("done") == 2)
        self.assertEqual(len(self.ui.sent), 2)
        self.assertIn("消息11", self.ui.sent[1])

    def test_timeout_is_retried_without_dropping_message(self):
        self.ai.failures = 1
        self.store.capture("12", [], [row("人呢")], now=99)
        self.spin_until(lambda: self.store.counts().get("pending") == 1 and self.ai.calls == 1)
        self.now = 103
        self.spin_until(lambda: self.store.counts().get("done") == 1)
        self.assertEqual(self.ui.sent, ["回复：人呢"])

    def test_send_confirmation_failure_keeps_same_reply_and_does_not_send_twice(self):
        self.ui.fail_after_send = True
        self.store.capture("12", [], [row("邀请你打游戏")], now=99)
        self.spin_until(lambda: self.store.counts().get("sending") == 1)
        self.now = 103
        self.spin_until(lambda: self.store.counts().get("done") == 1)
        self.assertEqual(self.ai.calls, 1)
        self.assertEqual(len(self.ui.sent), 1)
        self.assertEqual(len(self.store.history("12")), 2)

    def test_restart_after_send_restores_exact_draft_and_anchor(self):
        self.store.capture("12", [], [row("你好")], now=99)
        job = self.store.claim(("12",), 100)
        self.store.ready(job["id"], "你好", "你好呀")
        self.store.sending(job["id"], [key(1, "以前的消息")])
        self.store.close()
        self.store = RuntimeStore(self.path)
        recovered = self.store.next_send(("12",), 101)
        self.assertEqual(recovered["status"], "sending")
        self.assertEqual(recovered["reply"], "你好呀")
        self.assertEqual(json.loads(recovered["anchor"]), [key(1, "以前的消息")])

    def test_stuck_model_has_a_total_deadline(self):
        self.ai.release = threading.Event()
        self.store.capture("12", [], [row("你好")], now=99)
        self.runner.tick()
        self.now = 146
        with self.assertRaises(ModelStalled):
            self.runner.tick()
        self.assertEqual(self.store.counts(), {"working": 1})

    def test_other_contact_jobs_are_not_sent(self):
        self.store.capture("平安", [], [row("你好", "平安")], now=99)
        self.runner.tick()
        self.assertEqual(self.ai.calls, 0)
        self.assertEqual(self.ui.sent, [])


@unittest.skipUnless(sys.platform == "win32", "Windows process tree supervision")
class WatchdogTests(unittest.TestCase):
    def test_output_spam_does_not_mask_a_hang_and_child_tree_is_killed(self):
        with tempfile.TemporaryDirectory() as folder:
            pid_file = Path(folder) / "child.pid"
            source = (
                "import subprocess,sys,time\n"
                "from pathlib import Path\n"
                "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\n"
                f"Path({str(pid_file)!r}).write_text(str(p.pid))\n"
                "while True:\n print('error spam',flush=True);time.sleep(.05)\n"
            )
            code = run_worker([sys.executable, "-u", "-c", source], watchdog_seconds=1.5,
                              poll_seconds=0.05, emit=lambda _: None)
            self.assertEqual(code, 75)
            pid = int(pid_file.read_text())
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.restype = ctypes.c_void_p
            handle = kernel.OpenProcess(0x100000, False, pid)
            if handle:
                kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
                self.assertEqual(kernel.WaitForSingleObject(handle, 0), 0)
                kernel.CloseHandle.argtypes = [ctypes.c_void_p]
                kernel.CloseHandle(handle)

    def test_real_heartbeats_keep_worker_alive_until_normal_exit(self):
        source = "import time\nfor i in range(8):\n print('::heartbeat::',flush=True);time.sleep(.15)"
        code = run_worker([sys.executable, "-u", "-c", source], watchdog_seconds=0.6,
                          poll_seconds=0.05, emit=lambda _: None)
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
