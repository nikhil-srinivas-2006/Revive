"""Durable single-worker scheduler and no-overwrite publisher."""

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from .config import Config
from .errors import ReviveError
from .paths import check_plain_path, signature, source_reader, within
from .processes import WorkerJob
from .state import State

LOG = logging.getLogger("revive")
EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}


def file_hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def publish(temp: Path, target: Path):
    """Create a final directory entry atomically, without replacing an existing one.

    Both paths must be on one local filesystem. Hard links are supported by NTFS;
    unsupported filesystems fail explicitly rather than using an unsafe overwrite.
    """
    os.link(temp, target)
    temp.unlink()


class Engine:
    def __init__(self, config: Config):
        config.validate()
        self.config = config
        for path in (config.app_data, config.output):
            check_plain_path(path)
            path.mkdir(parents=True, exist_ok=True)
            check_plain_path(path)
        self.work = within(config.app_data, config.app_data / "work")
        self.work.mkdir(exist_ok=True)
        database = within(config.app_data, config.app_data / "state.sqlite3")
        for suffix in ("", "-wal", "-shm"):
            check_plain_path(Path(str(database) + suffix))
        self.state = State(database)
        scope = json.dumps([str(config.input), str(config.output)])
        saved_scope = self.state.setting("scope")
        if saved_scope is not None and saved_scope != scope:
            self.state.close()
            raise ReviveError(
                "CONFIG_SCOPE_CHANGED",
                "Use a new application directory when changing input/output roots.",
            )
        self.state.set_setting("scope", scope)

    def close(self):
        self.state.close()

    def incident(self, code, message):
        if self.state.incident(code, message):
            # Path-free, rate-limited logs; native toasts are a later milestone.
            LOG.warning("%s: %s", code, message)

    def recover(self):
        for row in self.state.db.execute(
            "SELECT * FROM jobs WHERE state='processing'"
        ).fetchall():
            try:
                self._cleanup_stage(row)
                if row["output_path"] and row["output_hash"]:
                    target = within(
                        self.config.output, self.config.output / row["output_path"]
                    )
                    if target.exists():
                        if file_hash(target) != row["output_hash"]:
                            raise ReviveError(
                                "PUBLISH_CONFLICT",
                                "An existing output has unexpected content.",
                            )
                        self.state.update(
                            row["id"],
                            state="completed",
                            completed_at=time.time(),
                            error_code=None,
                        )
                        continue
                self.state.update(
                    row["id"],
                    state="retry_pending",
                    next_attempt=0,
                    error_code="INTERRUPTED",
                )
            except (ReviveError, OSError):
                self.state.update(
                    row["id"], state="failed", error_code="PUBLISH_CONFLICT"
                )
                self.incident(
                    "PUBLISH_CONFLICT",
                    "Interrupted publication requires inspection; run status.",
                )
        # Only application-created work directories; do not follow links.
        for child in self.work.iterdir():
            check_plain_path(child)
            if child.is_dir() and child.name.startswith("job-"):
                shutil.rmtree(child)

    def _cleanup_stage(self, row):
        if row["staging_path"]:
            stage = within(self.config.output, self.config.output / row["staging_path"])
            if stage.name != f".revive-job-{row['id']}-{row['attempts']}.tmp":
                raise ReviveError(
                    "PATH_UNSAFE", "Unexpected staging filename in state."
                )
            stage.unlink(missing_ok=True)
            self.state.update(row["id"], staging_path=None)

    def scan(self, baseline=None, now=None):
        now = time.time() if now is None else now
        if baseline is None:
            baseline = not self.config.process_existing and not self.state.setting(
                "initial_scan_done"
            )
        try:
            check_plain_path(self.config.input)
            if not self.config.input.is_dir():
                raise ReviveError(
                    "INPUT_UNAVAILABLE", "Input directory is unavailable.", True
                )
            self._walk(self.config.input, baseline, now)
            self.state.set_setting("initial_scan_done", "yes")
            # Missing arrivals must not occupy bounded readiness slots forever.
            waiting = self.state.db.execute(
                "SELECT id,relative_path FROM jobs WHERE state='waiting_stability'"
            ).fetchall()
            for row in waiting:
                source = within(
                    self.config.input, self.config.input / row["relative_path"]
                )
                if not source.exists():
                    self.state.update(
                        row["id"], state="skipped", error_code="SOURCE_MISSING"
                    )
        except ReviveError as exc:
            self.incident(exc.code, str(exc))
        except OSError:
            self.incident(
                "INPUT_UNAVAILABLE",
                "Input directory cannot be read; discovery will retry.",
            )

    def _walk(self, directory, baseline, now):
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path)
                try:
                    within(self.config.input, path)
                    if entry.is_dir(follow_symlinks=False):
                        if self.config.recursive:
                            self._walk(path, baseline, now)
                    elif (
                        entry.is_file(follow_symlinks=False)
                        and path.suffix.lower() in EXTENSIONS
                    ):
                        relative = path.relative_to(self.config.input).as_posix()
                        # Windows DirEntry.stat() omits file identity fields; use Path.stat().
                        admitted = self.state.observe(
                            relative,
                            signature(path.stat(follow_symlinks=False)),
                            self.config.fingerprint,
                            now,
                            self.config,
                            baseline,
                        )
                        if not admitted:
                            self.incident(
                                "QUEUE_FULL",
                                "Queue capacity reached; deferred files remain in input.",
                            )
                            return
                except ReviveError as exc:
                    self.incident(exc.code, str(exc))
                except OSError:
                    self.incident(
                        "ACCESS_DENIED",
                        "An input entry could not be inspected; discovery will retry.",
                    )

    def _space(self, path, needed=0):
        if (
            shutil.disk_usage(path).free
            < self.config.min_free_disk_mib * 1024**2 + needed
        ):
            raise ReviveError(
                "DISK_LOW", "Insufficient free disk space; work will retry.", True
            )

    def _snapshot(self, job, dest):
        source = within(self.config.input, self.config.input / job["relative_path"])
        limit = self.config.max_source_mib * 1024**2
        with source_reader(self.config.input, source) as stream:
            before = os.fstat(stream.fileno())
            if signature(before) != job["signature"]:
                raise ReviveError(
                    "SOURCE_CHANGED",
                    "Source revision changed; discovery will reconcile.",
                )
            if before.st_size > limit:
                raise ReviveError(
                    "IMAGE_LIMIT", "Source exceeds the configured byte limit."
                )
            self._space(self.work, before.st_size * 2)
            digest = hashlib.sha256()
            size = 0
            with dest.open("xb") as output:
                while block := stream.read(1024 * 1024):
                    size += len(block)
                    if size > limit:
                        raise ReviveError(
                            "IMAGE_LIMIT", "Source exceeds the configured byte limit."
                        )
                    output.write(block)
                    digest.update(block)
            if (
                signature(os.fstat(stream.fileno())) != job["signature"]
                or signature(source.stat()) != job["signature"]
            ):
                raise ReviveError(
                    "SOURCE_CHANGED", "Source changed during snapshotting."
                )
            if os.name != "nt":
                # POSIX has no mandatory deny-write sharing: verify a second full read.
                time.sleep(self.config.quiet_seconds)
                stream.seek(0)
                if (
                    hashlib.file_digest(stream, "sha256").hexdigest()
                    != digest.hexdigest()
                ):
                    raise ReviveError(
                        "SOURCE_CHANGED", "Source changed during snapshot verification."
                    )
                if signature(source.stat()) != job["signature"]:
                    raise ReviveError(
                        "SOURCE_CHANGED", "Source changed during snapshot verification."
                    )
        return digest.hexdigest()

    def _worker(self, snapshot, output):
        request = {
            "snapshot": str(snapshot),
            "output": str(output),
            "settings": self.config.worker_settings(),
        }
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        containment = WorkerJob(request["settings"]["worker_commit_mib"])
        try:
            with containment.popen(
                [sys.executable, "-m", "revive.worker"],
                stdin=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=flags,
            ) as process:
                try:
                    stdout, _ = process.communicate(
                        json.dumps(request), timeout=self.config.worker_timeout_seconds
                    )
                    if process.returncode:
                        raise ReviveError(
                            "WORKER_CRASH",
                            "Worker exited unexpectedly; work will retry.",
                            True,
                        )
                except BaseException:
                    # Kill descendants before waiting for pipes inherited through
                    # the Windows Python launcher; killing only Popen.pid can hang.
                    containment.close()
                    process.kill()
                    process.communicate()
                    raise
            response = json.loads(stdout)
            if not response["ok"]:
                raise ReviveError(
                    response["code"],
                    response["message"],
                    response.get("retryable", False),
                )
            return response
        except subprocess.TimeoutExpired as exc:
            raise ReviveError(
                "WORKER_TIMEOUT", "Worker exceeded its deadline; work will retry.", True
            ) from exc
        except (ValueError, KeyError) as exc:
            raise ReviveError(
                "WORKER_PROTOCOL", "Worker returned an invalid response."
            ) from exc
        finally:
            containment.close()

    def process_one(self):
        job = self.state.next_job()
        if job is None:
            return False
        attempts = job["attempts"] + 1
        self.state.update(
            job["id"], state="processing", attempts=attempts, error_code=None
        )
        stage = None
        stage_owned = False
        try:
            check_plain_path(self.work)
            with tempfile.TemporaryDirectory(prefix="job-", dir=self.work) as directory:
                snapshot = Path(directory) / "source"
                encoded = Path(directory) / "encoded"
                digest = self._snapshot(job, snapshot)
                response = self._worker(snapshot, encoded)
                if file_hash(encoded) != response["hash"]:
                    raise ReviveError(
                        "OUTPUT_INVALID",
                        "Output checksum does not match the worker result.",
                    )
                if self.config.adapter == "mock_copy" and response["hash"] != digest:
                    raise ReviveError(
                        "OUTPUT_INVALID",
                        "Mock output is not identical to its snapshot.",
                    )
                relative = Path(job["relative_path"])
                stem = relative.stem[:100]
                # Include a path digest so case/extension collisions retain their source mapping.
                mapping = hashlib.sha256(job["relative_path"].encode()).hexdigest()[:12]
                suffix = (
                    "mockcopy" if self.config.adapter == "mock_copy" else "normalized"
                )
                if response["metadata"].get("colors_inferred"):
                    suffix = "colorized"
                if response["metadata"].get("details_inferred"):
                    suffix = (
                        "colorized_upscaled"
                        if response["metadata"].get("colors_inferred")
                        else "upscaled"
                    )
                name = f"{stem}_{suffix}_{digest}_{self.config.fingerprint[:12]}_{mapping}{response['extension']}"
                target = within(
                    self.config.output, self.config.output / relative.parent / name
                )
                target.parent.mkdir(parents=True, exist_ok=True)
                within(self.config.output, target)
                self._space(target.parent, encoded.stat().st_size)
                metadata = {
                    **response["metadata"],
                    "source_hash": digest,
                    "pipeline": self.config.fingerprint,
                    "processed_at": time.time(),
                }
                self.state.update(
                    job["id"],
                    source_hash=digest,
                    output_path=target.relative_to(self.config.output).as_posix(),
                    output_hash=response["hash"],
                    metadata=json.dumps(metadata),
                )
                if target.exists():
                    if file_hash(target) != response["hash"]:
                        raise ReviveError(
                            "PUBLISH_CONFLICT",
                            "An existing output has unexpected content.",
                        )
                else:
                    # Stage beside target, making atomic publication same-volume.
                    stage = within(
                        self.config.output,
                        target.parent / f".revive-job-{job['id']}-{attempts}.tmp",
                    )
                    self.state.update(
                        job["id"],
                        staging_path=stage.relative_to(self.config.output).as_posix(),
                    )
                    with stage.open("xb") as output, encoded.open("rb") as stream:
                        stage_owned = True
                        shutil.copyfileobj(stream, output, 1024 * 1024)
                        output.flush()
                        os.fsync(output.fileno())
                    if file_hash(stage) != response["hash"]:
                        raise ReviveError(
                            "OUTPUT_INVALID", "Staged output checksum mismatch."
                        )
                    within(self.config.output, target)
                    publish(stage, target)
                self.state.update(
                    job["id"],
                    state="completed",
                    completed_at=time.time(),
                    error_code=None,
                    staging_path=None,
                )
        except ReviveError as exc:
            self._fail(job, attempts, exc)
        except FileExistsError:
            self._fail(
                job,
                attempts,
                ReviveError(
                    "PUBLISH_CONFLICT", "An output or staging file already exists."
                ),
            )
        except OSError as exc:
            code = "DISK_LOW" if getattr(exc, "errno", None) == 28 else "IO_ERROR"
            self._fail(
                job,
                attempts,
                ReviveError(
                    code, "Filesystem operation failed; work will retry.", True
                ),
            )
        finally:
            if stage_owned and stage is not None and stage.exists():
                stage.unlink()
        return True

    def _fail(self, job, attempts, exc):
        retry = exc.retryable and attempts < self.config.max_attempts
        delay = self.config.delays_seconds[
            min(attempts - 1, len(self.config.delays_seconds) - 1)
        ]
        state = "retry_pending" if retry else "failed"
        if exc.code == "SOURCE_CHANGED":
            state = "skipped"
        self.state.update(
            job["id"],
            state=state,
            error_code=exc.code,
            next_attempt=time.time() + delay,
        )
        if not retry and state != "skipped":
            self.incident(exc.code, f"Job {job['id']}: {exc}. Run status for details.")

    def run(self, once=False, stop=None):
        stop = stop or threading.Event()
        self.recover()
        self.scan()
        dirty = threading.Event()
        observer = None
        if not once:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer

            class Handler(FileSystemEventHandler):
                def on_any_event(self, event):
                    if event.event_type in {"created", "modified", "moved", "deleted"}:
                        dirty.set()

            try:
                observer = Observer()
                observer.schedule(
                    Handler(), str(self.config.input), recursive=self.config.recursive
                )
                observer.start()
            except OSError:
                observer = None
                self.incident(
                    "WATCH_UNAVAILABLE",
                    "Event monitoring unavailable; reconciliation remains active.",
                )
        next_scan = time.monotonic() + self.config.sample_seconds
        try:
            while not stop.is_set():
                now = time.monotonic()
                if now >= next_scan or dirty.is_set():
                    dirty.clear()
                    self.scan()
                    waiting = self.state.db.execute(
                        "SELECT 1 FROM jobs WHERE state='waiting_stability' LIMIT 1"
                    ).fetchone()
                    interval = (
                        self.config.sample_seconds
                        if waiting
                        else self.config.reconcile_seconds
                    )
                    next_scan = time.monotonic() + interval
                if self.process_one():
                    continue
                waiting = self.state.db.execute(
                    "SELECT 1 FROM jobs WHERE state='waiting_stability' LIMIT 1"
                ).fetchone()
                if once and not waiting:
                    return
                # Event.wait avoids busy polling and can be interrupted on shutdown.
                stop.wait(min(1, max(0.01, next_scan - time.monotonic())))
        finally:
            if observer is not None:
                observer.stop()
                observer.join(timeout=10)
