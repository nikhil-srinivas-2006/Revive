# Revive

A local Windows photo-processing engine under development. The **normalize_png**
adapter inspects photographs, applies EXIF orientation once, converts valid embedded
profiles to sRGB, and publishes lossless 8-bit PNG derivatives. Originals remain
unchanged. Optional conventional cleanup is disabled by default. An optional local
**DDColor Tiny** stage now colorizes eligible grayscale inputs. Super-resolution
and face restoration are not implemented yet.

The **mock_copy** adapter remains available for byte-identical automation tests.
Output names distinguish `_normalized_` derivatives from `_mockcopy_` copies.

The design reference is [the implementation plan](docs/IMPLEMENTATION_PLAN.md).
Current scope and remaining work are tracked in [implementation status](docs/IMPLEMENTATION_STATUS.md).

## Setup

Python 3.11 and uv are recommended. From the repository root:

```powershell
uv sync --locked --extra dev --python 3.11
Copy-Item configs/example.toml config.local.toml
```

The application targets a broad range of Windows laptops, with CPU-only operation
as the baseline and optional qualified GPU acceleration later. A specific development
PC's hardware is not a requirement for other installations. A lightweight OS query
now selects a conservative worker memory budget automatically. GPU discovery,
acceleration qualification, and measured minimum system requirements remain pending.

Edit the three absolute paths in `config.local.toml`. The template also supports
`${REVIVE_INPUT_DIR}`, `${REVIVE_OUTPUT_DIR}`, and `${LOCALAPPDATA}` environment
references; set them or replace them with absolute paths. Input, output, and application
data must be separate, non-overlapping directories. Create the input directory
yourself; Revive creates only output and application-data directories. Configuration is strict:
misspelled keys and unimplemented adapters produce an error.

The current workspace's ignored `config.local.toml` uses `test_photos/` as input,
`.runtime/png_output/` for PNG derivatives and `.runtime/png_state/` for processing
state. The earlier mock-copy outputs/state are preserved separately.
The local configuration and test photographs are excluded from version control.

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
- Automatic physical-RAM admission and pre-decode checks protect responsiveness;
  the Windows worker also has a separate enforced committed-memory ceiling.
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

When trying a new adapter on previously completed photos, use a separate output and
application-data directory. The current development configuration already does this;
changing an adapter alone intentionally does not rerun successful jobs.

The pipeline emits one output per source mapping, preserving subdirectories. The
name includes source-content, pipeline and path digests. A hash conflict or foreign
file at the intended destination is reported, never overwritten.

## PNG normalization and optional cleanup

The template selects `normalize_png`. Default processing preserves decoded pixels
apart from orientation and color conversion. Untagged RGB/grayscale inputs assume
sRGB and record `ASSUMED_SRGB` in the manifest. CMYK/Lab inputs require a valid
profile. Invalid profiles and high-bit-depth scans fail explicitly rather than
silently discarding color interpretation or reducing precision.

Alpha is preserved; no background is flattened. PNG encoding is lossless, but the
derivative is 8-bit sRGB and is not an archival replacement for a higher-precision
original. No resize, crop, sharpening, scratch reconstruction, or generative editing
is performed. Every encoded PNG is reopened and compared against the planned pixels,
dimensions, mode, orientation, and color profile before publication.

Descriptive EXIF and PNG text are carried forward where supported. Structural EXIF
tags are removed or corrected. Source EXIF, ICC, XMP, PNG text, and JPEG APP13 data
are retained in central manifest records when available. Original XMP stays in the
manifest rather than being copied with potentially stale orientation/color-space
claims. This is not an assertion that every proprietary metadata field is supported.
Manifests may contain private descriptive metadata, including GPS/date information.

Inspection reports grayscale/sepia candidates, tonal span, clipping, edge energy,
and median residuals from a view of at most 256 pixels on its long edge. These are
heuristics, not quality scores, face detection, or historical color verification.
Residuals can represent grain, text or detail as well as noise.

Optional settings in `[pipeline]`:

```toml
adapter = "normalize_png"
denoise_strength = 0
contrast_strength = 0
```

Each cleanup strength accepts 0–0.25. A nonzero setting enables mild median-filter
blending or tonal adjustment only when inspection evidence passes the current
conservative gate. Transparency bypasses cleanup. Keep strengths at zero for
faithful normalization; review representative results before enabling cleanup.
Applied stages and skip reasons are recorded; inferred colors are always false.

`[resources].worker_memory_mib = 0` selects a physical-RAM admission budget from
the smaller of 2 GiB, one quarter of physical RAM, and half the currently available
RAM; unavailable OS data falls back to 512 MiB. Windows additionally computes a
committed-memory ceiling from the smaller of 2 GiB, one quarter of physical RAM,
and half the available system commit. Resident and committed memory are different
quantities; neither setting is a hard resident-working-set cap. A positive value
overrides both budgets. Admission uses a conservative allocation estimate, not an
exact predictor. Memory pressure can defer a photo; Windows can terminate a worker
that breaches its commit ceiling. Other operating
systems currently have admission checks only and are not production-qualified.
Disk preflight reserves room for the uncompressed PNG estimate and configured
free-space reserve; actual disk errors remain recoverable.

## Optional local colorization

Use a separate output/application-data directory when trying colorization on photos
already completed by another pipeline. Choose those directories before installing
weights: models are stored under the selected application-data root.

```powershell
uv sync --locked --extra dev --extra colorization --python 3.11
.venv\Scripts\python.exe -m revive --config config.local.toml models status
.venv\Scripts\python.exe -m revive --config config.local.toml models install
```

`models install` explicitly downloads the author's pinned 220 MB Apache-2.0
checkpoint and verifies its published SHA-256 before publication. It never
overwrites a conflicting asset. Runtime, doctor and model status operate offline;
missing/corrupt weights produce actionable errors. For offline setup, copy the exact
checkpoint to `<app_data>/models/ddcolor_tiny/pytorch_model.bin` and verify it with
`models status`. No training dependencies, torchvision, remote-code loader or cloud
inference are used. Keep the optional extra in subsequent `uv sync` commands if
colorization is enabled; base-only sync removes optional packages.

```toml
[colorization]
enabled = true
strength = 0.85
include_sepia = false
input_size = 256
```

Enable only after setup and representative visual review. Automatic grayscale
selection checks every pixel to catch small colored details hidden by the inspection
thumbnail. Color/uncertain images and transparency bypass the model. Sepia remains
opt-in because its history cannot be reliably inferred. Zero strength also bypasses
inference. A configured colorization failure does not silently become successful
uncolorized output for an eligible photo.

Only inferred Lab chroma is used, combined with the normalized/optionally cleaned
image's luminance in memory-bounded strips. Chroma is reduced where gamut conversion
would exceed the 1.0 Lab L error guard. Output size/composition are retained. This
does not guarantee correct colors, identity, or visual improvement. PNG provenance
and the central manifest explicitly mark inferred colors and model versions.
Outputs are labeled `_colorized_` when inference was applied; bypassed photos use
`_normalized_`.

DDColor needs more RAM than normalization. Conservative model admission estimates
can defer large photos even when the decoder's pixel limit permits them. The 512
view is experimental and may require a larger explicitly configured commit budget;
the automatic cap is not raised to force a job through. CPU threads default to two
and can be configured from 1–8 in `[resources].cpu_threads`. No GPU is allocated.

The workspace's separate qualification configuration is `.runtime/colorization.toml`.
Its outputs are in `.runtime/colorized_output/`; `config.local.toml` still uses the
normalization-only path. The author checkpoint has been installed for qualification.
See [qualification evidence and limits](docs/COLORIZATION_QUALIFICATION.md) for
source licenses, asset digests, measured memory, and outstanding review gates.

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
release gates. No GPU backend, face enhancement, super-resolution, scratch
reconstruction or Drive API exists yet. Colorization remains disabled in the shared
template until users explicitly complete setup and review representative results.

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

