"""Read-only, bounded disk inventory; never follow links or cross filesystems."""
from __future__ import annotations

import heapq
import os
import shutil
import stat
import time
from pathlib import Path


def format_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:,.1f} {unit}"
        amount /= 1024
    raise AssertionError("unreachable")


def inventory(root: Path = Path("/"), *, seconds: float = 30) -> dict:
    """Rank allocated bytes, including nested directory totals (which overlap).

    Permission failures and a deadline produce explicitly partial results. File contents
    are never opened. Hard links count once, as with du; symlinks and devices are ignored.
    """
    root = root.resolve()
    usage = shutil.disk_usage(root)
    device = root.stat().st_dev
    deadline = time.monotonic() + seconds
    files: list[tuple[int, str]] = []
    folders: list[tuple[int, str]] = []
    seen: set[tuple[int, int]] = set()
    skipped = 0
    timed_out = False

    def keep(heap, size, path):
        heapq.heappush(heap, (size, str(path)))
        if len(heap) > 3:
            heapq.heappop(heap)

    def walk(path):
        nonlocal skipped, timed_out
        total = 0
        try:
            with os.scandir(path) as entries:
                for entry in entries:
                    if time.monotonic() >= deadline:
                        timed_out = True
                        break
                    try:
                        info = entry.stat(follow_symlinks=False)
                        if info.st_dev != device or entry.is_symlink():
                            continue
                        if stat.S_ISDIR(info.st_mode):
                            size = walk(Path(entry.path))
                            keep(folders, size, entry.path)
                            total += size
                        elif stat.S_ISREG(info.st_mode):
                            identity = (info.st_dev, info.st_ino)
                            if identity in seen:
                                continue
                            if info.st_nlink > 1:
                                seen.add(identity)
                            size = getattr(info, "st_blocks", None)
                            size = info.st_size if size is None else size * 512
                            total += size
                            keep(files, size, entry.path)
                    except OSError:
                        skipped += 1
        except (OSError, RecursionError):
            skipped += 1
        return total

    walk(root)
    return {"root": str(root), "total": usage.total, "used": usage.used,
            "free": usage.free, "files": sorted(files, reverse=True),
            "folders": sorted(folders, reverse=True), "skipped": skipped,
            "timed_out": timed_out}
