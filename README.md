# Revive

A local Windows photo-processing engine under development. The first milestone
implements folder automation with a **mock copy adapter**. It validates photographs
and publishes byte-identical copies. It does not restore, colorize, resize, rotate,
or modify metadata. Outputs include `_mockcopy_` in their names, and manifests record
`restoration_performed: false`.

The design reference is [the implementation plan](docs/IMPLEMENTATION_PLAN.md).
Current scope and remaining work are tracked in [implementation status](docs/IMPLEMENTATION_STATUS.md).

## Setup

Python 3.11 and uv are recommended. From the repository root:

```powershell
uv sync --locked --extra dev --python 3.11
Copy-Item configs/example.toml config.local.toml
```

Edit the three absolute paths in `config.local.toml`. Input, output, and application
data must be separate, non-overlapping directories. Create the input directory
yourself; Revive creates only output and application-data directories. The example
uses disposable directories under `E:/Revive/.runtime/`. Configuration is strict:
misspelled keys and unimplemented adapters produce an error.

```powershell
.venv\Scripts\python.exe -m revive --config config.local.toml validate-config
.venv\Scripts\python.exe -m revive --config config.local.toml doctor
.venv\Scripts\python.exe -m revive --config config.local.toml run --once
```

`--once` waits for discovered images to settle, processes immediately runnable jobs,
then exits. Future retry attempts remain durable for the next invocation. Continuous
operation uses `run` without `--once`; Ctrl+C requests shutdown after the active
bounded worker attempt. No autostart task is installed automatically.

```powershell
.venv\Scripts\python.exe -m revive --config config.local.toml run
```

To run without a console during development, use `.venv\Scripts\pythonw.exe` instead
of `python.exe`. Validate configuration first: startup errors cannot be shown in a
hidden console. Native failure notifications and supported autostart installation
are not implemented yet; this milestone is not ready for unattended production use.

## Inspect and recover

These read-only commands work while the monitor is running:

```powershell
.venv\Scripts\python.exe -m revive --config config.local.toml status
.venv\Scripts\python.exe -m revive --config config.local.toml export-manifest
```

`export-manifest` writes JSON Lines to stdout and includes private relative filenames
and processing metadata. Store exports accordingly. `status` shows counts and
path-free incidents. Logs rotate locally, with repeated incidents logged at most
once per code per day; the database still counts every occurrence.

After fixing a failed job, stop the monitor and explicitly queue a retry:

```powershell
.venv\Scripts\python.exe -m revive --config config.local.toml retry 1
.venv\Scripts\python.exe -m revive --config config.local.toml run --once
```

One mutating command owns each application-data directory at a time. Changing the
input/output roots requires a separate application-data directory, preventing old
relative queue entries from being interpreted against a different collection.

## Implemented behavior

- Native filesystem events and periodic reconciliation, with one processing job.
- Size/timestamp/file-identity stability sampling, followed by a private snapshot.
- On Windows, a read handle denying concurrent writes/deletes while snapshotting;
  final handle path verification. POSIX additionally compares two full reads.
- JPEG, PNG, TIFF, BMP and WebP detection; actual content format verification and
  full decoding in a separate process. Temporary/other filename extensions ignored.
- Byte and decoded-pixel limits; animated/multipage inputs explicitly rejected.
- Durable SQLite WAL queue, revision tracking, retries, startup recovery, provenance.
- Separate worker per job; no Pillow or AI framework imported by the monitor.
- Windows Job Objects terminate the worker if its monitor dies.
- Source files opened for reading only; originals and metadata remain byte-identical.
- Same-volume, atomic no-replace publication using hard links; NTFS is the initial
  target. Unsupported filesystems fail explicitly rather than overwrite a result.
- Publication intent persisted before final output creation; recovery adopts a
  matching output or reports a conflict. Only recorded staging files are removed.

`process_existing = false` skips existing eligible files on the first successful
scan only. Files arriving while the application is subsequently stopped are
discovered when it restarts. Changed source revisions get new output names; repeated
events do not duplicate work. Successful results are not automatically reprocessed
when the pipeline fingerprint changes.

The pipeline emits one output per source mapping, preserving subdirectories. The
name includes source-content, pipeline and path digests. A hash conflict or foreign
file at the intended destination is reported, never overwritten.

## Current boundaries

Application path checks are **not OS-enforced isolation**. The normal user account
retains its other permissions. This milestone rejects links, junctions, reparse
points, network/device paths and cloud placeholders; use fully resident ordinary
local files. Drive Desktop integration needs separate qualification. A malicious
process with the same account can still race filesystem operations; the application
does not claim to sandbox hostile local actors or compromised image decoders.

A paused copy can appear stable. A producer's temporary file followed by final rename
is preferable where available. Notifications currently use durable incidents and
logs only. Large-image memory measurements, notification delivery, suspend-aware
timeouts, disk-history retention, and sustained archive-scale event handling remain
release gates. No GPU backend, AI models, model downloads, conventional restoration,
PNG conversion, face enhancement, scratch reconstruction or Drive API exists yet.

## Development checks

```powershell
uv sync --locked --extra dev --python 3.11
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\ruff.exe check src tests
.venv\Scripts\ruff.exe format --check src tests
```

Tests use synthetic local images and temporary folders, including real subprocess
workers and Windows handle behavior. Symlink tests skip when Windows does not permit
unprivileged symlink creation. No personal photo collection is used by tests.

