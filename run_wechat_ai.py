"""自动发送启动器：监督主循环心跳，卡住或退出后恢复持久队列。"""
from __future__ import annotations

import logging
import argparse
from logging.handlers import RotatingFileHandler
import subprocess
import sys
import threading
import time
from pathlib import Path

from wechat_lock import LockBusyError, acquire_file_lock

ROOT = Path(__file__).resolve().parent
WATCHDOG_SECONDS = 30
_LOCK = None
STOP_PATH = ROOT / ".wechat_ai_stop"


def lock_supervisor() -> None:
    global _LOCK
    try:
        _LOCK = acquire_file_lock(ROOT / '.wechat_ai_supervisor.lock')
    except LockBusyError as exc:
        raise LockBusyError('自动回复启动器已经在运行，无需重复启动。') from exc


def stop_worker(worker: subprocess.Popen[str]) -> None:
    if worker.poll() is not None:
        return
    # 虚拟环境的 python.exe 还会启动真正的解释器，必须结束整个子进程树。
    subprocess.run(['taskkill', '/PID', str(worker.pid), '/T', '/F'],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   check=False, timeout=10)
    try:
        worker.wait(timeout=5)
    except subprocess.TimeoutExpired:
        worker.kill()
        worker.wait(timeout=5)


def run_worker(command: list[str], watchdog_seconds: float = WATCHDOG_SECONDS,
               poll_seconds: float = 0.2, emit=print, stop_requested=lambda: False) -> int:
    last_progress = time.monotonic()
    worker = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True, encoding='utf-8',
                              errors='replace', bufsize=1,
                              creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)

    def relay() -> None:
        nonlocal last_progress
        assert worker.stdout is not None
        for line in worker.stdout:
            if line.strip() == '::heartbeat::':
                last_progress = time.monotonic()
            else:
                emit(line.rstrip())

    reader = threading.Thread(target=relay, daemon=True)
    reader.start()
    try:
        while worker.poll() is None:
            if stop_requested():
                stop_worker(worker)
                return 0
            if time.monotonic() - last_progress > watchdog_seconds:
                emit(f'主循环 {watchdog_seconds:g} 秒没有心跳，自动结束卡住的进程并恢复队列。')
                stop_worker(worker)
                return 75
            time.sleep(poll_seconds)
        return worker.returncode
    finally:
        stop_worker(worker)
        reader.join(timeout=2)
        if worker.stdout:
            worker.stdout.close()


def main() -> int:
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    if sys.platform != 'win32':
        print('此启动器只支持 Windows。', flush=True)
        return 2
    parser = argparse.ArgumentParser(description='微信自动回复后台启动器')
    parser.add_argument('--stop', action='store_true', help='停止本项目的自动回复进程')
    args = parser.parse_args()
    if args.stop:
        STOP_PATH.touch()
        for _ in range(100):
            try:
                lock_supervisor()
            except LockBusyError:
                time.sleep(0.1)
                continue
            except RuntimeError as exc:
                print(str(exc), flush=True)
                return 2
            STOP_PATH.unlink(missing_ok=True)
            print('自动回复已停止。', flush=True)
            return 0
        print('停止请求已提交，工作进程正在退出。', flush=True)
        return 1
    try:
        lock_supervisor()
    except RuntimeError as exc:
        print(str(exc), flush=True)
        return 2
    STOP_PATH.unlink(missing_ok=True)
    log_dir = ROOT / 'logs'
    log_dir.mkdir(exist_ok=True)
    logger = logging.getLogger('wechat-supervisor')
    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(log_dir / 'wechat_ai.log', maxBytes=2_000_000,
                                  backupCount=3, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
    logger.addHandler(handler)

    def emit(message: str) -> None:
        print(message, flush=True)
        logger.info(message)

    try:
        while True:
            code = run_worker([sys.executable, '-u', str(ROOT / 'wechat_ai.py'), '--send'],
                              emit=emit, stop_requested=STOP_PATH.exists)
            if code in (0, 2):
                if code == 2:
                    emit('配置错误或已有监听实例，请按提示修正后启动。')
                return code
            emit('3 秒后自动重新连接微信，接着处理待回复消息。')
            time.sleep(3)
    except KeyboardInterrupt:
        emit('已停止自动回复。')
        return 0
    finally:
        handler.close()
        STOP_PATH.unlink(missing_ok=True)


if __name__ == '__main__':
    raise SystemExit(main())
