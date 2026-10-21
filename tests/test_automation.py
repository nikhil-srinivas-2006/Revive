import hashlib
import json
import os
import subprocess
import sys
import threading
import time

import pytest
from PIL import Image

from revive.cli import main
from revive.config import load_config
from revive.engine import Engine, file_hash, publish
from revive.errors import ReviveError
from revive.paths import InstanceLock, within


def photo(path, color="gray", fmt="PNG"):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (20, 30), color).save(path, format=fmt)
    return path.read_bytes()


def settle(engine):
    now = time.time()
    engine.scan(now=now)
    engine.scan(now=now + 1)


def test_empty_run_has_no_jobs_or_worker(engine):
    engine.run(once=True)
    assert engine.state.counts() == {}


def test_end_to_end_unicode_duplicate_events_and_identical_source(engine, config):
    source = config.input / "Family photos" / "été 家族.png"
    original = photo(source)
    engine.run(once=True)
    assert engine.state.counts() == {"completed": 1}
    row = engine.state.db.execute("SELECT * FROM jobs").fetchone()
    output = config.output / row["output_path"]
    assert "Family photos" in str(output)
    assert "_mockcopy_" in output.name
    assert output.read_bytes() == original == source.read_bytes()
    metadata = json.loads(row["metadata"])
    assert metadata["restoration_performed"] is False
    for _ in range(4):
        engine.scan()
    assert not engine.process_one()
    assert engine.state.counts() == {"completed": 1}


def test_slow_copy_restarts_stability(engine, config):
    source = config.input / "slow.png"
    source.write_bytes(b"partial")
    engine.scan(now=10)
    assert not engine.process_one()
    source.write_bytes(b"longer partial")
    engine.scan(now=11)
    assert not engine.process_one()
    assert engine.state.counts() == {"skipped": 1, "waiting_stability": 1}
    photo(source)
    engine.scan(now=12)
    assert not engine.process_one()
    engine.scan(now=13)
    assert engine.process_one()
    assert engine.state.counts()["completed"] == 1


def test_changed_source_creates_revision_without_overwrite(engine, config):
    source = config.input / "photo.png"
    first = photo(source, "red")
    settle(engine)
    engine.process_one()
    second = photo(source, "blue")
    settle(engine)
    engine.process_one()
    outputs = list(config.output.rglob("*mockcopy*"))
    assert {p.read_bytes() for p in outputs} == {first, second}
    assert engine.state.counts() == {"completed": 2}


def test_duplicate_bytes_have_two_source_mappings(engine, config):
    data = photo(config.input / "a.png")
    (config.input / "b.png").write_bytes(data)
    settle(engine)
    assert engine.process_one()
    assert engine.process_one()
    assert engine.state.counts() == {"completed": 2}
    assert len(list(config.output.glob("*mockcopy*"))) == 2


def test_corrupt_file_fails_without_output(engine, config):
    (config.input / "bad.jpg").write_bytes(b"not an image")
    settle(engine)
    engine.process_one()
    row = engine.state.db.execute("SELECT * FROM jobs").fetchone()
    assert row["state"] == "failed"
    assert row["error_code"] == "SOURCE_CORRUPT"
    assert list(config.output.iterdir()) == []


def test_actual_format_controls_extension(engine, config):
    photo(config.input / "misleading.jpg", fmt="PNG")
    settle(engine)
    engine.process_one()
    row = engine.state.db.execute("SELECT * FROM jobs").fetchone()
    assert row["state"] == "completed"
    assert row["output_path"].endswith(".png")


def test_multipage_rejected(engine, config):
    frame = Image.new("RGB", (10, 10))
    frame.save(config.input / "multi.tif", save_all=True, append_images=[frame])
    settle(engine)
    engine.process_one()
    assert (
        engine.state.db.execute("SELECT error_code FROM jobs").fetchone()[0]
        == "FORMAT_UNSUPPORTED"
    )


def test_pixel_limit_before_decode(config, change_config):
    instance = Engine(change_config(config, max_input_megapixels=1))
    try:
        Image.new("L", (1100, 1000)).save(config.input / "large.png")
        settle(instance)
        instance.process_one()
        assert (
            instance.state.db.execute("SELECT error_code FROM jobs").fetchone()[0]
            == "IMAGE_LIMIT"
        )
    finally:
        instance.close()


def test_initial_existing_disabled_but_new_files_admitted(config, change_config):
    photo(config.input / "old.png")
    instance = Engine(change_config(config, process_existing=False))
    try:
        instance.scan(baseline=True)
        assert instance.state.counts() == {"skipped": 1}
        photo(config.input / "new.png")
        settle(instance)
        instance.process_one()
        assert instance.state.counts() == {"skipped": 1, "completed": 1}
    finally:
        instance.close()


def test_capacity_defers_and_later_discovers(config, change_config):
    instance = Engine(change_config(config, max_pending_jobs=1))
    try:
        photo(config.input / "a.png")
        photo(config.input / "b.png")
        settle(instance)
        assert instance.state.counts().get("queued") == 1
        instance.process_one()
        settle(instance)
        instance.process_one()
        assert instance.state.counts() == {"completed": 2}
    finally:
        instance.close()


def test_crash_after_publication_adopts_artifact(engine, config):
    photo(config.input / "a.png")
    settle(engine)
    engine.process_one()
    engine.state.update(1, state="processing", completed_at=None)
    engine.recover()
    assert engine.state.counts() == {"completed": 1}
    assert len(list(config.output.rglob("*mockcopy*"))) == 1


def test_crash_before_publication_requeues(engine, config):
    photo(config.input / "a.png")
    settle(engine)
    engine.state.update(1, state="processing", attempts=1)
    engine.recover()
    assert engine.state.counts() == {"retry_pending": 1}
    engine.process_one()
    assert engine.state.counts() == {"completed": 1}


def test_publication_conflict_preserves_existing(engine, config):
    photo(config.input / "a.png")
    settle(engine)
    engine.process_one()
    row = engine.state.db.execute("SELECT * FROM jobs").fetchone()
    target = config.output / row["output_path"]
    target.write_bytes(b"other content")
    engine.state.update(1, state="processing")
    engine.recover()
    assert target.read_bytes() == b"other content"
    assert engine.state.counts() == {"failed": 1}


def test_disk_failure_retries_without_partial_final(engine, config, monkeypatch):
    photo(config.input / "a.png")
    settle(engine)

    def no_space(*args):
        raise ReviveError("DISK_LOW", "Low disk.", True)

    monkeypatch.setattr(engine, "_space", no_space)
    engine.process_one()
    assert engine.state.counts() == {"retry_pending": 1}
    assert list(config.output.iterdir()) == []


def test_worker_crash_bounded_retry(engine, config, monkeypatch):
    photo(config.input / "a.png")
    settle(engine)

    def crash(*args):
        raise ReviveError("WORKER_CRASH", "Worker crashed.", True)

    monkeypatch.setattr(engine, "_worker", crash)
    for _ in range(config.max_attempts):
        engine.state.db.execute("UPDATE jobs SET next_attempt=0")
        engine.state.db.commit()
        engine.process_one()
    assert engine.state.counts() == {"failed": 1}
    assert (
        engine.state.db.execute("SELECT attempts FROM jobs").fetchone()[0]
        == config.max_attempts
    )


def test_source_changed_between_queue_and_snapshot_is_not_processed(engine, config):
    source = config.input / "a.png"
    photo(source)
    settle(engine)
    photo(source, "red")
    engine.process_one()
    assert engine.state.counts() == {"skipped": 1}
    assert list(config.output.iterdir()) == []


def test_atomic_publish_never_replaces_existing(tmp_path):
    source, target = tmp_path / "temp", tmp_path / "final"
    source.write_bytes(b"new")
    target.write_bytes(b"old")
    with pytest.raises(FileExistsError):
        publish(source, target)
    assert target.read_bytes() == b"old"


def test_path_escape_rejected(config):
    with pytest.raises(ReviveError):
        within(config.input, config.input / ".." / "outside.png")


def test_overlap_rejected(config, change_config):
    with pytest.raises(ReviveError, match="overlap"):
        change_config(config, output=config.input / "output").validate()


def test_symlink_rejected(config, tmp_path):
    source = config.input / "link.png"
    target = tmp_path / "private.png"
    photo(target)
    try:
        source.symlink_to(target)
    except OSError:
        pytest.skip("Symlink creation requires Developer Mode or privileges on Windows")
    with pytest.raises(ReviveError):
        within(config.input, source)


def test_single_instance_lock(config):
    config.app_data.mkdir()
    path = config.app_data / "monitor.lock"
    with (
        InstanceLock(path),
        pytest.raises(ReviveError, match="Another monitor"),
        InstanceLock(path),
    ):
        pass


def config_file(config, tmp_path, extra=""):
    path = tmp_path / "config.toml"
    path.write_text(
        f"""schema_version = 1
[paths]
input = {json.dumps(config.input.as_posix())}
output = {json.dumps(config.output.as_posix())}
app_data = {json.dumps(config.app_data.as_posix())}
{extra}
""",
        encoding="utf-8",
    )
    return path


def test_config_strict_keys_and_adapter(config, tmp_path):
    path = config_file(config, tmp_path)
    assert load_config(path).input == config.input
    path = config_file(config, tmp_path, '[pipeline]\nadapter = "ddcolor"')
    with pytest.raises(ReviveError, match="mock_copy"):
        load_config(path)
    path = config_file(config, tmp_path, "[pipeline]\ncolourization = true")
    with pytest.raises(ReviveError):
        load_config(path)


def test_cli_readiness_and_manifest(config, tmp_path, capsys):
    path = config_file(config, tmp_path)
    assert main(["--config", str(path), "validate-config"]) == 0
    assert main(["--config", str(path), "doctor"]) == 0
    assert not config.app_data.exists()
    assert '"ai_available": false' in capsys.readouterr().out


def test_watch_live_arrival_and_shutdown(engine, config):
    stop = threading.Event()
    errors = []

    def arrival():
        try:
            time.sleep(0.1)
            photo(config.input / "new.png")
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if list(config.output.rglob("*mockcopy*")):
                    return
                time.sleep(0.05)
            errors.append("No output")
        finally:
            stop.set()

    thread = threading.Thread(target=arrival)
    thread.start()
    engine.run(stop=stop)
    thread.join()
    assert not errors
    assert engine.state.counts() == {"completed": 1}


def test_monitor_does_not_import_heavy_libraries():
    code = 'import revive.engine, sys; assert "PIL" not in sys.modules; assert "torch" not in sys.modules'
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, check=False
    )
    assert result.returncode == 0


def test_source_hash_is_snapshot_hash(engine, config):
    data = photo(config.input / "a.png")
    settle(engine)
    engine.process_one()
    row = engine.state.db.execute("SELECT * FROM jobs").fetchone()
    assert row["source_hash"] == hashlib.sha256(data).hexdigest()
    assert row["output_hash"] == file_hash(config.output / row["output_path"])


def test_database_survives_reopen(config):
    photo(config.input / "a.png")
    first = Engine(config)
    settle(first)
    first.close()
    second = Engine(config)
    try:
        second.recover()
        second.process_one()
        assert second.state.counts() == {"completed": 1}
        assert second.state.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        second.close()


def test_readonly_status_works_while_monitor_lock_is_held(
    engine, config, tmp_path, capsys
):
    path = config_file(config, tmp_path)
    with InstanceLock(config.app_data / "monitor.lock"):
        assert main(["--config", str(path), "status"]) == 0
    assert '"counts"' in capsys.readouterr().out


def test_status_before_first_run_does_not_create_directories(config, tmp_path):
    path = config_file(config, tmp_path)
    assert main(["--config", str(path), "status"]) == 0
    assert not config.app_data.exists()
    assert not config.output.exists()


def test_scope_change_requires_separate_state(engine, config, change_config):
    with pytest.raises(ReviveError, match="new application directory"):
        Engine(change_config(config, input=config.input.parent / "other"))


def test_initial_scan_disabled_applies_only_once_across_restarts(config, change_config):
    settings = change_config(config, process_existing=False)
    photo(config.input / "old.png")
    first = Engine(settings)
    first.scan()
    first.close()
    photo(config.input / "arrived_while_offline.png")
    second = Engine(settings)
    try:
        second.run(once=True)
        assert second.state.counts() == {"skipped": 1, "completed": 1}
    finally:
        second.close()


def test_changed_file_does_not_deadlock_full_queue(config, change_config):
    instance = Engine(change_config(config, max_pending_jobs=1))
    try:
        source = config.input / "a.png"
        photo(source)
        instance.scan(now=1)
        photo(source, "red")
        instance.scan(now=2)
        instance.scan(now=3)
        instance.process_one()
        assert instance.state.counts() == {"skipped": 1, "completed": 1}
    finally:
        instance.close()


def test_missing_arrival_releases_queue_slot(engine, config):
    source = config.input / "a.png"
    photo(source)
    engine.scan()
    source.unlink()
    engine.scan()
    assert engine.state.counts() == {"skipped": 1}


def test_reappearing_same_revision_restarts_readiness(engine, config):
    photo(config.input / "a.png")
    engine.scan()
    engine.state.update(1, state="skipped", error_code="SOURCE_MISSING")
    settle(engine)
    engine.process_one()
    assert engine.state.counts() == {"completed": 1}


def test_recovery_cleans_only_recorded_staging_file(engine, config):
    photo(config.input / "a.png")
    settle(engine)
    stage = config.output / ".revive-job-1-1.tmp"
    stage.write_bytes(b"partial")
    unrelated = config.output / "keep.tmp"
    unrelated.write_bytes(b"keep")
    engine.state.update(1, state="processing", attempts=1, staging_path=stage.name)
    engine.recover()
    assert not stage.exists()
    assert unrelated.read_bytes() == b"keep"
    assert engine.state.counts() == {"retry_pending": 1}


def test_configuration_change_does_not_reprocess_completed_revision(engine, config):
    photo(config.input / "a.png")
    settle(engine)
    engine.process_one()
    assert engine.state.observe(
        "a.png",
        engine.state.db.execute("SELECT signature FROM jobs").fetchone()[0],
        "new-pipeline",
        time.time(),
        config,
    )
    assert engine.state.counts() == {"completed": 1}


def test_incidents_are_counted_but_logs_rate_limited(engine, caplog):
    with caplog.at_level("WARNING", logger="revive"):
        for _ in range(3):
            engine.incident("TEST_INCIDENT", "Corrective action required.")
    assert (
        len([record for record in caplog.records if "TEST_INCIDENT" in record.message])
        == 1
    )
    assert engine.state.db.execute("SELECT count FROM incidents").fetchone()[0] == 3


@pytest.mark.skipif(os.name != "nt", reason="Windows share-mode behavior")
def test_source_handle_prevents_concurrent_writes(config):
    from revive.paths import source_reader

    source = config.input / "a.png"
    photo(source)
    with (
        source_reader(config.input, source),
        pytest.raises(PermissionError),
        source.open("wb"),
    ):
        pass


@pytest.mark.skipif(os.name != "nt", reason="Windows job containment")
def test_worker_terminated_when_containment_handle_closes():
    from revive.processes import WorkerJob

    container = WorkerJob()
    with subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"]
    ) as process:
        try:
            container.assign(process.pid)
            assert process.poll() is None
            container.close()
            process.wait(timeout=5)
            # Windows job-close termination may report exit code zero. Exiting
            # within five seconds, before the thirty-second sleep, proves teardown.
            assert process.poll() is not None
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
