import os
import subprocess
import sys
import time

import pytest

from revive.errors import ReviveError
from revive.processes import WorkerJob


def test_timeout_closes_job_before_draining_inherited_pipes(
    engine, monkeypatch, tmp_path
):
    import revive.engine

    events = []

    class Job:
        def __init__(self, memory):
            pass

        def popen(self, *args, **kwargs):
            return Process()

        def close(self):
            events.append("close")

    class Process:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def communicate(self, data=None, timeout=None):
            if data:
                raise subprocess.TimeoutExpired("worker", timeout)
            assert events[-1] == "kill" and "close" in events
            events.append("drain")
            return "", ""

        def kill(self):
            assert events == ["close"]
            events.append("kill")

    monkeypatch.setattr(revive.engine, "WorkerJob", Job)
    with pytest.raises(ReviveError) as error:
        engine._worker(tmp_path / "snapshot", tmp_path / "output")
    assert error.value.code == "WORKER_TIMEOUT" and error.value.retryable
    assert events == ["close", "kill", "drain", "close"]


@pytest.mark.skipif(
    os.name != "nt", reason="Windows suspended startup and Job Object inheritance"
)
def test_suspended_start_contains_real_python_descendants_and_closes_pipes(tmp_path):
    import ctypes
    from ctypes import wintypes

    marker = tmp_path / "descendant-pid"

    class DelayedJob(WorkerJob):
        def assign(self, pid):
            # Even with a delayed assignment, no launcher/interpreter can run yet.
            time.sleep(0.25)
            assert not marker.exists()
            super().assign(pid)

    child = "import os,sys,time; from pathlib import Path; Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)"
    parent = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]]); time.sleep(60)"
    job = DelayedJob(256)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    descendant = None
    try:
        with job.popen(
            [sys.executable, "-c", parent, child, str(marker)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW,
        ) as process:
            try:
                deadline = time.monotonic() + 10
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                assert marker.exists(), "Contained descendant did not start"
                descendant = kernel.OpenProcess(
                    0x100000, False, int(marker.read_text())
                )
                assert descendant
                job.close()
                process.communicate(timeout=10)
                assert process.returncode is not None
                assert kernel.WaitForSingleObject(descendant, 10000) == 0
            finally:
                job.close()
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=10)
    finally:
        job.close()
        if descendant:
            kernel.CloseHandle(descendant)
