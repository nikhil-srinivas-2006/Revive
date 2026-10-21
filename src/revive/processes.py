"""Worker lifetime containment without a heavyweight Windows dependency."""

import os
import subprocess

from .errors import ReviveError


class WorkerJob:
    def popen(self, args, **kwargs):
        """Assign before execution, including before a Windows venv launcher forks.

        Popen closes the primary thread handle. Toolhelp finds that still-suspended
        thread so documented ResumeThread can start it after job assignment.
        """
        if os.name == "nt":
            if self.handle is None:
                raise ReviveError("WORKER_CONTAINMENT", "Worker job is already closed.")
            kwargs["creationflags"] = (
                kwargs.get("creationflags", 0) | 0x4
            )  # CREATE_SUSPENDED
        process = subprocess.Popen(args, **kwargs)
        try:
            self.assign(process.pid)
            if os.name == "nt":
                self._resume(process.pid)
            return process
        except BaseException:
            self.close()
            process.kill()
            process.communicate()
            raise

    def _resume(self, pid):
        import ctypes
        from ctypes import wintypes

        class ThreadEntry(ctypes.Structure):
            _fields_ = [
                ("size", wintypes.DWORD),
                ("usage", wintypes.DWORD),
                ("tid", wintypes.DWORD),
                ("pid", wintypes.DWORD),
                ("priority", wintypes.LONG),
                ("delta", wintypes.LONG),
                ("flags", wintypes.DWORD),
            ]

        kernel = self.kernel
        kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
        kernel.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
        kernel.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenThread.restype = wintypes.HANDLE
        kernel.ResumeThread.argtypes = [wintypes.HANDLE]
        kernel.ResumeThread.restype = wintypes.DWORD
        snapshot = kernel.CreateToolhelp32Snapshot(0x4, 0)  # TH32CS_SNAPTHREAD
        if snapshot == wintypes.HANDLE(-1).value:
            raise ReviveError(
                "WORKER_CONTAINMENT", "Cannot locate suspended worker thread."
            )
        try:
            entry = ThreadEntry()
            entry.size = ctypes.sizeof(entry)
            available = kernel.Thread32First(snapshot, ctypes.byref(entry))
            while available:
                if entry.pid == pid:
                    thread = kernel.OpenThread(
                        0x2, False, entry.tid
                    )  # THREAD_SUSPEND_RESUME
                    if not thread:
                        break
                    try:
                        if kernel.ResumeThread(thread) == 1:
                            return
                    finally:
                        kernel.CloseHandle(thread)
                    break
                available = kernel.Thread32Next(snapshot, ctypes.byref(entry))
            raise ReviveError(
                "WORKER_CONTAINMENT", "Cannot start the contained worker thread."
            )
        finally:
            kernel.CloseHandle(snapshot)

    def __init__(self, memory_mib=0):
        self.handle = None
        if os.name != "nt":
            return
        import ctypes
        from ctypes import wintypes

        class BasicLimit(ctypes.Structure):
            _fields_ = [
                ("process_time", ctypes.c_int64),
                ("job_time", ctypes.c_int64),
                ("flags", wintypes.DWORD),
                ("min_ws", ctypes.c_size_t),
                ("max_ws", ctypes.c_size_t),
                ("active_processes", wintypes.DWORD),
                ("affinity", ctypes.c_size_t),
                ("priority", wintypes.DWORD),
                ("scheduling", wintypes.DWORD),
            ]

        class IO(ctypes.Structure):
            _fields_ = [("counts", ctypes.c_uint64 * 6)]

        class ExtendedLimit(ctypes.Structure):
            _fields_ = [
                ("basic", BasicLimit),
                ("io", IO),
                ("process_memory", ctypes.c_size_t),
                ("job_memory", ctypes.c_size_t),
                ("peak_process", ctypes.c_size_t),
                ("peak_job", ctypes.c_size_t),
            ]

        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        self.kernel.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.AssignProcessToJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
        ]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        limits = ExtendedLimit()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if memory_mib:
            limits.basic.flags |= 0x100  # JOB_OBJECT_LIMIT_PROCESS_MEMORY
            limits.process_memory = memory_mib * 1024**2
        if not self.handle or not self.kernel.SetInformationJobObject(
            self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            self.close()
            raise ReviveError(
                "WORKER_CONTAINMENT", "Cannot initialize Windows worker containment."
            )

    def assign(self, pid):
        if self.handle is None:
            return
        # Assign requires PROCESS_SET_QUOTA | PROCESS_TERMINATE.
        process = self.kernel.OpenProcess(0x0101, False, pid)
        try:
            if not process or not self.kernel.AssignProcessToJobObject(
                self.handle, process
            ):
                raise ReviveError(
                    "WORKER_CONTAINMENT", "Cannot attach worker to its Windows job."
                )
        finally:
            if process:
                self.kernel.CloseHandle(process)

    def close(self):
        if self.handle is not None:
            self.kernel.CloseHandle(self.handle)
            self.handle = None
