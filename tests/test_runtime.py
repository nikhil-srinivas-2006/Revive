import hashlib
import json
import os
import sqlite3
import sys
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import replace

import pytest
from PIL import Image

from revive.engine import Engine
from revive.errors import ReviveError
from revive.runtime import RuntimeSession, control, monitor_status, signal_stop
from revive.startup import NAMESPACE, task_xml


def config_file(config, tmp_path):
    path = tmp_path / "config with spaces.toml"
    path.write_text(
        "schema_version=1\n[paths]\n"
        + "\n".join(
            f"{name}={json.dumps(str(getattr(config, name)))}"
            for name in ("input", "output", "app_data")
        )
        + "\n[watch]\nquiet_seconds=0.02\nstable_samples=2\nsample_seconds=0.02\nreconcile_seconds=0.2\n[resources]\nworker_memory_mib=256\n",
        encoding="utf-8",
    )
    from revive.config import load_config

    return path, load_config(path)


def test_status_is_read_only_and_stale_pid_is_not_running(config):
    assert not monitor_status(config.app_data)["running"]
    assert not config.app_data.exists()
    config.app_data.mkdir()
    (config.app_data / "monitor.json").write_text(
        json.dumps(
            {
                "state": "running",
                "pid": os.getpid(),
                "token": uuid.uuid4().hex,
                "heartbeat": time.time(),
            }
        )
    )
    status = monitor_status(config.app_data)
    assert not status["running"] and status["state"] == "stopped"
    assert not control(config, None, "stop")["running"]


def test_token_scoped_stop_and_session_status(config, tmp_path):
    config.app_data.mkdir()
    stop = threading.Event()
    with RuntimeSession(config, tmp_path / "config.toml", stop) as session:
        session.ready(True)
        status = monitor_status(config.app_data)
        assert (
            status["running"]
            and status["responsive"]
            and status["watch_mode"] == "events"
        )
        signal_stop(config.app_data, uuid.uuid4().hex)
        assert not session.requested() and not stop.wait(0.1)
        signal_stop(config.app_data, session.token)
        assert stop.wait(3)
    assert not monitor_status(config.app_data)["running"]


def test_invalid_token_never_writes_a_control_request(config):
    config.app_data.mkdir()
    with pytest.raises(ReviveError):
        signal_stop(config.app_data, "../../another-event")
    assert not (config.app_data / "monitor-request.json").exists()


def test_background_live_arrival_duplicate_start_restart_and_stop(config, tmp_path):
    path, current = config_file(config, tmp_path)
    try:
        first = control(current, path, "start", 20)
        duplicate = control(current, path, "start", 20)
        assert first["state"] == "running" and duplicate["already_running"]
        assert first["token"] == duplicate["token"]
        source = current.input / "new photo.png"
        Image.new("RGB", (20, 30), "gray").save(source)
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        deadline = time.monotonic() + 15
        while (
            not list(current.output.rglob("*mockcopy*")) and time.monotonic() < deadline
        ):
            time.sleep(0.05)
        assert list(current.output.rglob("*mockcopy*"))
        with pytest.raises(ReviveError) as error:
            control(replace(current, cpu_threads=3), path, "start", 20)
        assert error.value.code == "CONFIG_ACTIVE"
        second = control(current, path, "restart", 20)
        assert second["running"] and second["token"] != first["token"]
        assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
        connection = sqlite3.connect(current.app_data / "state.sqlite3")
        assert (
            connection.execute(
                "SELECT count(*) FROM jobs WHERE state='completed'"
            ).fetchone()[0]
            == 1
        )
        connection.close()
    finally:
        control(current, path, "stop", 20)
    assert not monitor_status(current.app_data)["running"]


def test_stop_interrupts_worker_without_using_retry_attempt(
    config, tmp_path, monkeypatch
):
    from revive.processes import WorkerJob

    current = replace(
        config, worker_memory_mib=256, max_attempts=1, worker_timeout_seconds=30
    )
    engine = Engine(current)
    source = current.input / "photo.png"
    Image.new("RGB", (20, 30), "gray").save(source)
    original = source.read_bytes()
    marker = tmp_path / "worker-ready"
    stop = threading.Event()
    failures = []
    original_popen = WorkerJob.popen

    def blocking_worker(job, args, **kwargs):
        code = "import sys,time; from pathlib import Path; Path(sys.argv[1]).write_text('ready'); time.sleep(60)"
        return original_popen(job, [sys.executable, "-c", code, str(marker)], **kwargs)

    with RuntimeSession(current, tmp_path / "config.toml", stop) as session:

        def stop_when_ready():
            try:
                deadline = time.monotonic() + 10
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                if not marker.exists():
                    failures.append("Worker never reached the interrupt point")
                signal_stop(current.app_data, session.token)
            finally:
                if failures:
                    stop.set()

        sender = threading.Thread(target=stop_when_ready)
        sender.start()
        try:
            with monkeypatch.context() as patch:
                patch.setattr(WorkerJob, "popen", blocking_worker)
                started = time.monotonic()
                engine.run(stop=stop, on_ready=session.ready)
                assert time.monotonic() - started < 10
        finally:
            stop.set()
            sender.join(timeout=12)
    try:
        assert not failures
        row = engine.state.db.execute(
            "SELECT state,attempts,error_code FROM jobs"
        ).fetchone()
        assert tuple(row) == ("retry_pending", 0, "INTERRUPTED")
        assert not list(current.output.rglob("*mockcopy*"))
        engine.run(once=True)
        assert engine.state.counts() == {"completed": 1}
        assert source.read_bytes() == original
    finally:
        engine.close()


def test_stop_before_start_does_not_scan_or_spawn(engine, monkeypatch):
    stop = threading.Event()
    stop.set()
    monkeypatch.setattr(
        engine, "scan", lambda: pytest.fail("Stopped engine must not scan")
    )
    engine.run(stop=stop)


def test_startup_definition_is_least_privilege_and_quotes_config(tmp_path):
    executable = tmp_path / "Python & environment" / "pythonw.exe"
    config = tmp_path / "Photos & family" / "config.toml"
    data = task_xml(config, executable, "S-1-5-21-123-456-789-1001")
    root = ET.fromstring(data)
    ns = {"t": NAMESPACE}
    assert root.find("t:Principals/t:Principal/t:RunLevel", ns).text == "LeastPrivilege"
    assert (
        root.find("t:Principals/t:Principal/t:LogonType", ns).text == "InteractiveToken"
    )
    assert root.find("t:Actions/t:Exec/t:Command", ns).text == str(executable)
    assert str(config) in root.find("t:Actions/t:Exec/t:Arguments", ns).text
    assert root.find("t:Settings/t:ExecutionTimeLimit", ns).text == "PT0S"
    assert root.find("t:Settings/t:StopIfGoingOnBatteries", ns).text == "false"
    assert "&amp;" in data.decode("utf-16")


def test_failed_session_keeps_observable_error_code(config, tmp_path):
    config.app_data.mkdir()
    with (
        pytest.raises(ReviveError),
        RuntimeSession(config, tmp_path / "config.toml", threading.Event()),
    ):
        raise ReviveError("WATCH_UNAVAILABLE", "Test failure")
    status = monitor_status(config.app_data)
    assert not status["running"] and status["state"] == "failed"
    assert status["error_code"] == "WATCH_UNAVAILABLE"


def test_worker_protocol_survives_multiple_communication_timeouts(config, monkeypatch):
    from revive.processes import WorkerJob

    current = replace(config, worker_memory_mib=256, worker_timeout_seconds=10)
    instance = Engine(current)
    source = current.input / "slow-start.png"
    Image.new("RGB", (20, 30), "gray").save(source)
    original_popen = WorkerJob.popen

    def slow_start(job, args, **kwargs):
        code = "import time,runpy; time.sleep(0.7); runpy.run_module('revive.worker',run_name='__main__')"
        return original_popen(job, [sys.executable, "-c", code], **kwargs)

    monkeypatch.setattr(WorkerJob, "popen", slow_start)
    try:
        instance.run(once=True)
        assert instance.state.counts() == {"completed": 1}
        output = next(current.output.rglob("*mockcopy*"))
        assert output.read_bytes() == source.read_bytes()
    finally:
        instance.close()


def test_folder_replacement_rearms_watch_and_processes_new_arrival(engine, config):
    engine.config = replace(config, worker_memory_mib=256, reconcile_seconds=0.05)
    stop, ready = threading.Event(), threading.Event()
    failures = []

    def replace_folder():
        try:
            assert ready.wait(5)
            config.input.rename(config.input.with_name("retired-input"))
            config.input.mkdir()
            Image.new("RGB", (20, 30), "gray").save(
                config.input / "after replacement.png"
            )
            deadline = time.monotonic() + 10
            while (
                not list(config.output.rglob("*mockcopy*"))
                and time.monotonic() < deadline
            ):
                time.sleep(0.03)
            if not list(config.output.rglob("*mockcopy*")):
                failures.append("Replacement root did not process its new photo")
        finally:
            stop.set()

    sender = threading.Thread(target=replace_folder)
    sender.start()
    try:
        engine.run(stop=stop, on_ready=lambda _: ready.set())
    finally:
        stop.set()
        sender.join(timeout=12)
    assert not failures
    assert engine.state.counts() == {"completed": 1}


def test_resume_gap_reconciles_before_regular_scan_deadline(engine, monkeypatch):
    import revive.engine

    engine.config = replace(engine.config, sample_seconds=300, reconcile_seconds=300)
    clock = [0]
    scans = []
    stop = threading.Event()

    def process():
        if clock[0] == 0:
            clock[0] = 6
            return True
        stop.set()
        return False

    monkeypatch.setattr(revive.engine.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(engine, "scan", lambda: scans.append(clock[0]))
    monkeypatch.setattr(engine, "process_one", process)
    engine.run(once=True, stop=stop)
    assert scans == [0, 6]


def test_ready_failure_stops_already_started_observer(engine, monkeypatch):
    import watchdog.observers

    events = []

    class Observer:
        def schedule(self, *args, **kwargs):
            pass

        def start(self):
            events.append("start")

        def stop(self):
            events.append("stop")

        def join(self, timeout):
            events.append("join")

    monkeypatch.setattr(watchdog.observers, "Observer", Observer)

    def fail_ready(watching):
        raise ReviveError("CONTROL_UNAVAILABLE", "Control state unavailable")

    with pytest.raises(ReviveError):
        engine.run(on_ready=fail_ready)
    assert events == ["start", "stop", "join"]


def test_status_reports_recovery_action_for_failed_job(
    engine, config, tmp_path, capsys
):
    from revive.cli import main

    path, _ = config_file(config, tmp_path)
    (config.input / "broken.png").write_bytes(b"not an image")
    now = time.time()
    engine.scan(now=now)
    engine.scan(now=now + 1)
    engine.process_one()
    assert main(["--config", str(path), "status"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["needs_attention"][0]["error_code"] == "SOURCE_CORRUPT"
    assert "source format" in result["needs_attention"][0]["action"]
    assert not result["monitor"]["running"]
    assert "available_physical_mib" in result["resources"]
    assert "available_commit_mib" in result["resources"]
    assert result["resources"]["worker_memory_mib"] > 0


def test_interrupted_initial_scan_preserves_skip_existing_policy(config, monkeypatch):
    current = replace(config, process_existing=False)
    instance = Engine(current)
    for name in ("first.png", "second.png"):
        Image.new("RGB", (20, 30)).save(current.input / name)
    observe = instance.state.observe

    def stop_after_first(*args, **kwargs):
        result = observe(*args, **kwargs)
        instance.stop_event.set()
        return result

    try:
        with monkeypatch.context() as patch:
            patch.setattr(instance.state, "observe", stop_after_first)
            instance.scan()
        assert instance.state.setting("initial_scan_done") is None
        instance.stop_event = threading.Event()
        instance.scan()
        assert instance.state.setting("initial_scan_done") == "yes"
        assert instance.state.counts() == {"skipped": 2}
    finally:
        instance.close()


def test_configuration_change_during_launch_fails_before_monitor_starts(
    config, tmp_path, monkeypatch
):
    import revive.runtime

    path, current = config_file(config, tmp_path)
    popen = revive.runtime.subprocess.Popen

    def change_before_child(args, **kwargs):
        path.write_text(
            path.read_text(encoding="utf-8") + "cpu_threads=3\n", encoding="utf-8"
        )
        return popen(args, **kwargs)

    monkeypatch.setattr(revive.runtime.subprocess, "Popen", change_before_child)
    try:
        with pytest.raises(ReviveError) as error:
            control(current, path, "start", 10)
        assert error.value.code == "START_FAILED"
        assert not monitor_status(current.app_data)["running"]
        assert "CONFIG_CHANGED" in (current.app_data / "startup.log").read_text()
    finally:
        control(current, path, "stop", 10)


def test_control_failure_stops_monitor_and_reports_failure(
    config, tmp_path, monkeypatch
):
    from revive.runtime import StopSignal

    config.app_data.mkdir()
    stop = threading.Event()

    def fail_wait(self, seconds):
        raise OSError("injected control failure")

    monkeypatch.setattr(StopSignal, "wait", fail_wait)
    with (
        pytest.raises(ReviveError) as error,
        RuntimeSession(config, tmp_path / "config.toml", stop),
    ):
        assert stop.wait(3)
    assert error.value.code == "CONTROL_UNAVAILABLE"
    status = monitor_status(config.app_data)
    assert not status["running"] and status["state"] == "failed"
    assert status["error_code"] == "CONTROL_UNAVAILABLE"
