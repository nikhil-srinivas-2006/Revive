"""Local monitor lifecycle control. No image, network, or ML imports."""

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from .config import describe
from .errors import ReviveError
from .paths import InstanceLock, check_plain_path, within

_launches = []


def identity(config):
    return hashlib.sha256(
        json.dumps(describe(config), sort_keys=True).encode()
    ).hexdigest()


def read_json(path):
    check_plain_path(path)
    try:
        with path.open("rb") as stream:
            data = stream.read(16_385)
        if len(data) > 16_384:
            return {}
        value = json.loads(data)
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def write_json(root, path, value):
    within(root, path)
    temporary = within(root, path.with_name(f".{path.name}-{uuid.uuid4().hex}.tmp"))
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.write("\n")
        within(root, path)
        # A Windows status reader can briefly deny delete sharing. Let its tiny
        # bounded read finish before replacing the heartbeat/control file.
        for attempt in range(6):
            try:
                os.replace(temporary, path)
                break
            except PermissionError as exc:
                if getattr(exc, "winerror", None) not in (5, 32, 33) or attempt == 5:
                    raise
                time.sleep(0.02)
    finally:
        temporary.unlink(missing_ok=True)


def locked(path):
    """Probe an existing run lock, without creating files or changing contents."""
    check_plain_path(path)
    try:
        stream = path.open("r+b")
    except FileNotFoundError:
        return False
    with stream:
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        except (BlockingIOError, PermissionError):
            return True
    return False


def monitor_status(app_data):
    busy = locked(within(app_data, app_data / "monitor-run.lock"))
    info = read_json(within(app_data, app_data / "monitor.json"))
    phase = info.get("state", "starting" if busy else "stopped")
    heartbeat = info.get("heartbeat", 0)
    responsive = type(heartbeat) in (int, float) and 0 <= time.time() - heartbeat <= 15
    return {
        **info,
        "running": busy,
        "state": phase if busy else ("failed" if phase == "failed" else "stopped"),
        "responsive": bool(busy and responsive),
    }


def event_name(app_data, token):
    if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32}", token):
        raise ReviveError("CONTROL_INVALID", "Monitor control token is invalid.")
    scope = hashlib.sha256(os.path.normcase(str(app_data)).encode()).hexdigest()[:20]
    return f"Local\\ReviveStop-{scope}-{token}"


class StopSignal:
    def __init__(self, app_data, token):
        self.handle = None
        if os.name != "nt":
            return
        import ctypes
        from ctypes import wintypes

        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateEventW.argtypes = [
            wintypes.LPVOID,
            wintypes.BOOL,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        ]
        self.kernel.CreateEventW.restype = wintypes.HANDLE
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.SetEvent.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.CreateEventW(
            None, True, False, event_name(app_data, token)
        )
        if not self.handle:
            raise ReviveError(
                "CONTROL_UNAVAILABLE", "Cannot create the monitor stop event."
            )

    def wait(self, seconds):
        if self.handle is None:
            time.sleep(seconds)
            return False
        result = self.kernel.WaitForSingleObject(self.handle, round(seconds * 1000))
        if result not in (0, 258):
            raise ReviveError("CONTROL_UNAVAILABLE", "Monitor stop event failed.")
        return result == 0

    def wake(self):
        if self.handle is not None:
            self.kernel.SetEvent(self.handle)

    def close(self):
        if self.handle is not None:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def signal_stop(app_data, token):
    # A token-specific request also works across logon sessions and provides a
    # fallback if native event delivery is unavailable. It never targets a PID.
    event_name(app_data, token)
    write_json(
        app_data,
        app_data / "monitor-request.json",
        {"token": token, "operation": "stop"},
    )
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.OpenEventW.restype = wintypes.HANDLE
        kernel.SetEvent.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenEventW(0x2, False, event_name(app_data, token))
        if handle:
            try:
                kernel.SetEvent(handle)
            finally:
                kernel.CloseHandle(handle)


class RuntimeSession:
    def __init__(self, config, config_file, stop, token=None, once=False):
        self.config, self.stop = config, stop
        self.token = token or uuid.uuid4().hex
        event_name(config.app_data, self.token)
        self.path = within(config.app_data, config.app_data / "monitor.json")
        self.run_lock = InstanceLock(
            within(config.app_data, config.app_data / "monitor-run.lock")
        )
        self.done = threading.Event()
        self.failure = None
        self.mutex = threading.RLock()
        self.info = {
            "token": self.token,
            "pid": os.getpid(),
            "state": "starting",
            "started": time.time(),
            "config_file": str(config_file.resolve()),
            "config_identity": identity(config),
            "mode": "once" if once else "continuous",
        }

    def publish(self, **fields):
        with self.mutex:
            self.info.update(fields, heartbeat=time.time())
            write_json(self.config.app_data, self.path, self.info)

    def requested(self):
        request = read_json(self.config.app_data / "monitor-request.json")
        return request.get("token") == self.token and request.get("operation") == "stop"

    def listen(self):
        try:
            while not self.done.is_set():
                signaled = self.signal.wait(1)
                if self.done.is_set():
                    break
                if signaled or self.requested():
                    self.stop.set()
                    self.publish(state="stopping")
                    return
                if time.monotonic() >= self.next_heartbeat:
                    self.publish()
                    self.next_heartbeat = time.monotonic() + 5
        except (OSError, ReviveError):
            # Losing control/heartbeat storage fails closed; do not leave an
            # unmanageable hidden monitor processing more photos.
            self.failure = ReviveError(
                "CONTROL_UNAVAILABLE",
                "Monitor control or heartbeat storage failed; processing stopped.",
            )
            self.stop.set()

    def __enter__(self):
        self.run_lock.__enter__()
        try:
            self.signal = StopSignal(self.config.app_data, self.token)
            self.publish()
            if self.requested():
                self.stop.set()
            self.next_heartbeat = time.monotonic() + 5
            self.thread = threading.Thread(
                target=self.listen, name="revive-control", daemon=True
            )
            self.thread.start()
            return self
        except BaseException:
            if hasattr(self, "signal"):
                self.signal.close()
            self.run_lock.__exit__()
            raise

    def ready(self, watching):
        with self.mutex:
            if not self.stop.is_set():
                self.publish(
                    state="running",
                    watch_mode="events" if watching else "reconciliation",
                )

    def __exit__(self, exc_type=None, exc=None, traceback=None):
        self.done.set()
        self.signal.wake()
        self.thread.join()
        failure = exc or self.failure
        try:
            self.publish(
                state="failed" if exc_type or self.failure else "stopped",
                error_code=getattr(failure, "code", None),
            )
        finally:
            self.signal.close()
            self.run_lock.__exit__()
        if exc_type is None and self.failure is not None:
            raise self.failure


def _stop(config, timeout):
    info = monitor_status(config.app_data)
    if not info["running"]:
        return info
    signal_stop(config.app_data, info.get("token"))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = monitor_status(config.app_data)
        if not current["running"] and not locked(
            within(config.app_data, config.app_data / "monitor.lock")
        ):
            return current
        if current.get("token") != info.get("token"):
            raise ReviveError(
                "CONTROL_CHANGED", "A different monitor started while waiting for stop."
            )
        time.sleep(0.1)
    raise ReviveError(
        "STOP_TIMEOUT",
        "Stop was requested but shutdown has not completed; inspect status and logs.",
    )


def _start(config, config_file, timeout):
    current = monitor_status(config.app_data)
    if current["running"]:
        if current.get("config_identity") != identity(config):
            raise ReviveError(
                "CONFIG_ACTIVE",
                "Monitor uses different settings; restart with the intended configuration.",
            )
        return {**current, "already_running": True}
    token = uuid.uuid4().hex
    log = within(config.app_data, config.app_data / "startup.log")
    if log.exists() and log.stat().st_size > 1024**2:
        backup = within(config.app_data, config.app_data / "startup.log.1")
        os.replace(log, backup)
    options = (
        {
            "creationflags": subprocess.CREATE_NO_WINDOW
            | subprocess.CREATE_NEW_PROCESS_GROUP
        }
        if os.name == "nt"
        else {"start_new_session": True}
    )
    with log.open("ab") as errors:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "revive",
                "--config",
                str(config_file.resolve()),
                "run",
                "--launch-token",
                token,
                "--expected-config-identity",
                identity(config),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=errors,
            cwd=str(Path(sys.executable).parent),
            **options,
        )
    _launches[:] = [child for child in _launches if child.poll() is None]
    _launches.append(process)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = monitor_status(config.app_data)
        if (
            info.get("token") == token
            and info["running"]
            and info["state"] == "running"
        ):
            return info
        if process.poll() is not None:
            raise ReviveError(
                "START_FAILED", "Monitor exited during startup; inspect startup.log."
            )
        time.sleep(0.1)
    signal_stop(config.app_data, token)
    raise ReviveError(
        "START_TIMEOUT",
        "Startup did not confirm readiness; stop requested for this launch. Inspect startup.log.",
    )


def control(config, config_file, action, timeout=20):
    if action not in {"start", "stop", "restart"}:
        raise ReviveError("CONTROL_INVALID", "Unknown monitor control operation.")
    if action == "stop" and not config.app_data.exists():
        return {"running": False, "state": "stopped", "responsive": False}
    config.app_data.mkdir(parents=True, exist_ok=True)
    with InstanceLock(
        within(config.app_data, config.app_data / "monitor-manager.lock")
    ):
        if action in ("stop", "restart"):
            stopped = _stop(config, timeout)
            if action == "stop":
                return stopped
        return _start(config, config_file, timeout)
