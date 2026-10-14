"""持久消息队列、窗口消息对比和非阻塞回复调度。"""

from __future__ import annotations

import base64
import json
import sqlite3
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any


def appended_start(previous: list, current: list) -> int:
    """匹配上次窗口尾部与本次窗口头部；编号复用但文本不同仍是新消息。"""
    if not previous:
        return 0
    for size in range(min(len(previous), len(current)), 0, -1):
        if previous[-size:] == current[:size]:
            return size
    # 窗口重建会改变全部控件编号，内容序列仍可用于接着读。
    old_content = [item[1:] for item in previous]
    new_content = [item[1:] for item in current]
    for size in range(min(len(previous), len(current)), 0, -1):
        if old_content[-size:] == new_content[:size]:
            # 编号仍有交集时，不用文字覆盖真正新增的同文消息。
            old_ids = {tuple(item[0]) for item in previous}
            new_ids = {tuple(item[0]) for item in current}
            if old_ids.isdisjoint(new_ids):
                return size
    return 0


def encode_rows(rows: list[dict]) -> str:
    def encode(value: Any) -> Any:
        if isinstance(value, bytes):
            return {"__image_bytes__": base64.b64encode(value).decode("ascii")}
        raise TypeError(type(value).__name__)
    return json.dumps(rows, ensure_ascii=False, default=encode)


def decode_rows(value: str) -> list[dict]:
    def decode(item: dict) -> Any:
        if set(item) == {"__image_bytes__"}:
            return base64.b64decode(item["__image_bytes__"])
        return item
    return json.loads(value, object_hook=decode)


class RuntimeStore:
    def __init__(self, path: str | Path) -> None:
        self.db = sqlite3.connect(str(path), timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS cursors(friend TEXT PRIMARY KEY, snapshot TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs(
                id TEXT PRIMARY KEY, friend TEXT NOT NULL, rows TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', prompt TEXT, reply TEXT,
                anchor TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                next_try REAL NOT NULL, created REAL NOT NULL, error TEXT
            );
            CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status, next_try, created);
            CREATE TABLE IF NOT EXISTS history(
                job_id TEXT PRIMARY KEY, friend TEXT NOT NULL, prompt TEXT NOT NULL,
                reply TEXT NOT NULL, completed REAL NOT NULL
            );
        """)
        # 上次在生成阶段退出，重新生成即可；已生成和发送待确认的草稿保留。
        self.db.execute("UPDATE jobs SET status='pending' WHERE status='working'")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def cursor(self, friend: str) -> list | None:
        row = self.db.execute("SELECT snapshot FROM cursors WHERE friend=?", (friend,)).fetchone()
        return json.loads(row[0]) if row else None

    def capture(self, friend: str, snapshot: list, rows: list[dict], now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self.db:
            if rows:
                recent = self.db.execute(
                    "SELECT * FROM jobs WHERE friend=? AND status='pending' AND attempts=0 "
                    "AND created>? AND next_try>? ORDER BY created DESC LIMIT 1",
                    (friend, now - 2, now),
                ).fetchone()
                if recent:
                    combined = decode_rows(recent["rows"]) + rows
                    self.db.execute("UPDATE jobs SET rows=?,next_try=? WHERE id=?", (
                        encode_rows(combined), min(recent["created"] + 2, now + 0.6), recent["id"],
                    ))
                else:
                    self.db.execute("INSERT INTO jobs(id,friend,rows,next_try,created) VALUES(?,?,?,?,?)", (
                        uuid.uuid4().hex, friend, encode_rows(rows), now + 0.6, now,
                    ))
            self.db.execute("INSERT OR REPLACE INTO cursors(friend,snapshot) VALUES(?,?)", (
                friend, json.dumps(snapshot, ensure_ascii=False),
            ))

    def job(self, job_id: str) -> dict:
        return dict(self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def _next(self, statuses: tuple[str, ...], friends: tuple[str, ...], now: float) -> dict | None:
        if not friends:
            return None
        rows = self.db.execute(
            "SELECT * FROM jobs WHERE status IN (" + ",".join("?" for _ in statuses) + ") "
            "AND friend IN (" + ",".join("?" for _ in friends) + ") AND next_try<=? "
            "ORDER BY created,rowid", (*statuses, *friends, now),
        ).fetchall()
        for row in rows:
            earlier = self.db.execute(
                "SELECT 1 FROM jobs WHERE friend=? AND status!='done' AND created<? LIMIT 1",
                (row["friend"], row["created"]),
            ).fetchone()
            if not earlier:
                return dict(row)
        return None

    def claim(self, friends: tuple[str, ...], now: float) -> dict | None:
        job = self._next(("pending",), friends, now)
        if job:
            with self.db:
                self.db.execute("UPDATE jobs SET status='working' WHERE id=?", (job["id"],))
        return job

    def ready(self, job_id: str, prompt: str, reply: str) -> None:
        with self.db:
            self.db.execute("UPDATE jobs SET status='ready',prompt=?,reply=?,next_try=0,error=NULL WHERE id=?", (
                prompt, reply, job_id,
            ))

    def next_send(self, friends: tuple[str, ...], now: float) -> dict | None:
        return self._next(("ready", "sending"), friends, now)

    def sending(self, job_id: str, anchor: list) -> None:
        with self.db:
            self.db.execute("UPDATE jobs SET status='sending',anchor=? WHERE id=?", (
                json.dumps(anchor, ensure_ascii=False), job_id,
            ))

    def complete(self, job_id: str) -> None:
        job = self.job(job_id)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO history VALUES(?,?,?,?,?)", (
                job_id, job["friend"], job["prompt"], job["reply"], time.time(),
            ))
            # 已发送图片不再占用队列存储。
            self.db.execute("UPDATE jobs SET status='done',rows='[]',error=NULL WHERE id=?", (job_id,))

    def failed(self, job_id: str, stage: str, error: Exception, now: float) -> None:
        job = self.job(job_id)
        attempts = job["attempts"] + 1
        delay = min(2 ** min(attempts, 5), 30)
        with self.db:
            self.db.execute("UPDATE jobs SET status=?,attempts=?,next_try=?,error=? WHERE id=?", (
                stage, attempts, now + delay, f"{type(error).__name__}: {error}"[:500], job_id,
            ))
        print(f"[{job['friend']}] 暂时失败，消息已保存，{delay} 秒后重试：{type(error).__name__}", flush=True)

    def history(self, friend: str) -> list[dict[str, str]]:
        rows = self.db.execute("SELECT prompt,reply FROM history WHERE friend=? ORDER BY completed DESC LIMIT 4", (friend,)).fetchall()
        return [part for row in reversed(rows) for part in (
            {"role": "user", "content": row["prompt"]}, {"role": "assistant", "content": row["reply"]},
        )]

    def counts(self, friends: tuple[str, ...] | None = None) -> dict[str, int]:
        if friends is None:
            rows = self.db.execute("SELECT status,COUNT(*) FROM jobs GROUP BY status")
        elif not friends:
            return {}
        else:
            rows = self.db.execute("SELECT status,COUNT(*) FROM jobs WHERE friend IN (" +
                                   ",".join("?" for _ in friends) + ") GROUP BY status", friends)
        return {row[0]: row[1] for row in rows}


class ModelStalled(RuntimeError):
    pass


class AutoReplyRunner:
    """主线程只操作微信和队列；后台线程只访问模型。"""
    def __init__(self, assistant: Any, ui: Any, store: RuntimeStore, clock=time.time) -> None:
        self.assistant, self.ui, self.store, self.clock = assistant, ui, store, clock
        self.friends = assistant.config.allowed_friends
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="reply-model")
        self.active: tuple[str, Future] | None = None
        self.active_started = 0.0
        self.active_limit = 45.0

    def tick(self) -> None:
        self.ui.poll()
        now = self.clock()
        if self.active and not self.active[1].done() and now - self.active_started > self.active_limit:
            raise ModelStalled("model deadline exceeded")
        if self.active and self.active[1].done():
            job_id, future = self.active
            self.active = None
            try:
                prompt, reply = future.result()
                self.store.ready(job_id, prompt, reply)
            except Exception as exc:
                self.store.failed(job_id, "pending", exc, now)
        job = self.store.next_send(self.friends, now)
        if job:
            try:
                if self.assistant.auto_send:
                    if job["status"] == "ready":
                        anchor = self.ui.snapshot(job["friend"])
                        self.store.sending(job["id"], anchor)
                    else:
                        anchor = json.loads(job["anchor"])
                    self.ui.send(job["friend"], job["reply"], anchor=anchor)
                    print(f"[{job['friend']}] 已发送：{job['reply']}", flush=True)
                else:
                    print(f"[{job['friend']}] 预览：{job['reply']}", flush=True)
                self.store.complete(job["id"])
            except Exception as exc:
                stage = "sending" if self.store.job(job["id"])["anchor"] else "ready"
                self.store.failed(job["id"], stage, exc, now)
        if self.active is None:
            job = self.store.claim(self.friends, now)
            if job:
                print(f"[{job['friend']}] 正在生成回复，继续接收新消息。", flush=True)
                rows = decode_rows(job["rows"])
                future = self.executor.submit(
                    self.assistant.prepare_reply, job["friend"], rows, self.store.history(job["friend"]),
                )
                self.active = (job["id"], future)
                self.active_started = now
                self.active_limit = 45 + 30 * sum(row.get("消息类型") == "图片" for row in rows)

    def close(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)
