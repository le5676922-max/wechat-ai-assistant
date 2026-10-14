"""Windows 进程互斥锁；不读取其他进程已锁住的字节。"""
import os
from pathlib import Path
from typing import BinaryIO


class LockBusyError(RuntimeError):
    pass


def acquire_file_lock(path: Path) -> BinaryIO:
    import msvcrt

    try:
        handle = path.open("a+b")
    except OSError as exc:
        raise RuntimeError(f"无法打开锁文件 {path.name}：{exc}") from exc
    try:
        # 文件大小属于元数据，可在另一进程持锁时查询；read(1) 会被 Windows 拒绝。
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError as exc:
        handle.close()
        raise LockBusyError(f"锁已被其他进程占用：{path.name}") from exc
    return handle
