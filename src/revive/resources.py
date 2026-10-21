"""Lightweight memory discovery; no WMI, GPU probes, or ML imports."""

import os


def _memory_snapshot():
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
                return (
                    status.total // 1024**2,
                    status.available // 1024**2,
                    status.available_page // 1024**2,
                )
        else:
            page = os.sysconf("SC_PAGE_SIZE")
            return (
                os.sysconf("SC_PHYS_PAGES") * page // 1024**2,
                os.sysconf("SC_AVPHYS_PAGES") * page // 1024**2,
                None,
            )
    except (OSError, ValueError, AttributeError):
        pass
    return None, None, None


def memory_mib():
    total, available, _ = _memory_snapshot()
    return total, available


def available_commit_mib():
    return _memory_snapshot()[2]


def worker_budget_mib(configured=0):
    if configured:
        return configured
    total, available = memory_mib()
    # Never increase the budget merely to fit an image. Small-memory systems may
    # defer large inputs rather than jeopardize interactive desktop responsiveness.
    return max(1, min(2048, total // 4, available // 2)) if total and available else 512


def worker_commit_budget_mib(configured=0):
    if configured:
        return configured
    total, _ = memory_mib()
    available = available_commit_mib()
    if total and available:
        return max(1, min(2048, total // 4, available // 2))
    return worker_budget_mib()


def process_memory_mib():
    """Windows process high-water measurements for explicit qualification only."""
    if os.name != "nt":
        return {"peak_working_set": None, "peak_commit": None}
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("faults", wintypes.DWORD),
            *[
                (name, ctypes.c_size_t)
                for name in (
                    "peak_ws",
                    "ws",
                    "peak_paged",
                    "paged",
                    "peak_nonpaged",
                    "nonpaged",
                    "pagefile",
                    "peak_pagefile",
                    "private",
                )
            ],
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(Counters),
        wintypes.DWORD,
    ]
    info = Counters()
    info.cb = ctypes.sizeof(info)
    if not psapi.GetProcessMemoryInfo(
        kernel.GetCurrentProcess(), ctypes.byref(info), info.cb
    ):
        return {"peak_working_set": None, "peak_commit": None}
    return {
        "peak_working_set": round(info.peak_ws / 1024**2, 2),
        "peak_commit": round(info.peak_pagefile / 1024**2, 2),
    }
