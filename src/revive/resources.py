"""Lightweight memory discovery; no WMI, GPU probes, or ML imports."""

import os


def memory_mib():
    """Return total/available physical memory where the OS exposes it."""
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            class Status(ctypes.Structure):
                _fields_ = [
                    ("length", wintypes.DWORD),
                    ("load", wintypes.DWORD),
                    ("total", ctypes.c_uint64),
                    ("available", ctypes.c_uint64),
                    ("total_page", ctypes.c_uint64),
                    ("available_page", ctypes.c_uint64),
                    ("total_virtual", ctypes.c_uint64),
                    ("available_virtual", ctypes.c_uint64),
                    ("extended", ctypes.c_uint64),
                ]

            status = Status()
            status.length = ctypes.sizeof(status)
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(Status)]
            if kernel.GlobalMemoryStatusEx(ctypes.byref(status)):
                return status.total // 1024**2, status.available // 1024**2
        else:
            page = os.sysconf("SC_PAGE_SIZE")
            return (
                os.sysconf("SC_PHYS_PAGES") * page // 1024**2,
                os.sysconf("SC_AVPHYS_PAGES") * page // 1024**2,
            )
    except (OSError, ValueError, AttributeError):
        pass
    return None, None


def worker_budget_mib(configured=0):
    if configured:
        return configured
    total, available = memory_mib()
    # Never increase the budget merely to fit an image. Small-memory systems may
    # defer large inputs rather than jeopardize interactive desktop responsiveness.
    return max(1, min(2048, total // 4, available // 2)) if total and available else 512
