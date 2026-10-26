# Revive

A local Windows photo-processing engine under development. The **normalize_png**
adapter inspects photographs, applies EXIF orientation once, converts valid embedded
profiles to sRGB, and publishes lossless 8-bit PNG derivatives. Originals remain
unchanged. Optional conventional cleanup is disabled by default. An optional local
**DDColor-L ModelScope** stage colorizes eligible grayscale inputs, followed by
tiled **Real-ESRGAN x2plus** upscaling for images below the resolution target.
DDColor Tiny and SwinIR remain available as lightweight compatibility choices.
The setup template enables automatic selection of both stages. Face restoration
remains unimplemented. See [local quality setup and limits](docs/LOCAL_QUALITY.md).

The **mock_copy** adapter remains available for byte-identical automation tests.
Output names distinguish `_normalized_` derivatives from `_mockcopy_` copies.

The design reference is [the implementation plan](docs/IMPLEMENTATION_PLAN.md).
Current scope and remaining work are tracked in [implementation status](docs/IMPLEMENTATION_STATUS.md).

## Setup

Python 3.11 and uv are recommended. From the repository root:

```powershell
uv sync --locked --extra dev --extra colorization --extra super-resolution --python 3.11
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
`.runtime/quality_output/` for PNG derivatives and `.runtime/quality_state/`
for processing state. Earlier qualification outputs/state are preserved separately.
The local configuration and test photographs are excluded from version control.

```powershell
.venv\Scripts\python.exe -m revive --config config.local.toml validate-config
.venv\Scripts\python.exe -m revive --config config.local.toml models install --model ddcolor_modelscope
.venv\Scripts\python.exe -m revive --config config.local.toml models install --model realesrgan_x2plus
.venv\Scripts\python.exe -m revive --config config.local.toml doctor
.venv\Scripts\python.exe -m revive --config config.local.toml start
```

Model installation is a one-time setup step. Once the monitor is started,
copy photographs into the input folder. No per-photo command or profile selection
is needed. The same settings handle these cases:

| Original | Automatic action |
| --- | --- |
| Confidently grayscale, below target | Colorize, then upscale |
| Already colored, below target | Upscale only |
| Confidently grayscale, at/above target | Colorize only |
| Already colored, at/above target | Normalize orientation/profile only |

The resolution decision uses pixel dimensions, with a default target of 4096 on
the long edge and a maximum 2x enlargement. It does not diagnose blur or guarantee
recovered detail. Sepia/uncertain colors and transparency retain their conservative
bypass rules. RAM, disk and timeout limits still apply; a resource failure is
reported for retry rather than silently declared a successful enhancement.

`--once` waits for discovered images to settle, processes immediately runnable jobs,
then exits. Future retry attempts remain durable for the next invocation. Continuous
operation uses `run` without `--once`; Ctrl+C cooperatively interrupts an active
worker and preserves its job for resumption. No autostart task is installed automatically.

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
original. With both AI stages disabled, normalization does not resize or infer
colors/details. Every encoded PNG is reopened and compared against the planned pixels,
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
Applied stages and skip reasons are recorded; inferred colors are marked only when
the separate colorization stage actually runs.

For lightweight models, `[resources].worker_memory_mib = 0` selects a physical-RAM admission budget from
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
The larger DDColor-L profile uses separate automatic ceilings up to 12 GiB while
reserving half available memory. See [memory policy](docs/LOCAL_QUALITY.md).

## Lightweight colorization compatibility profile

Use a separate output/application-data directory when trying colorization on photos
already completed by another pipeline. Choose those directories before installing
weights: models are stored under the selected application-data root.

```powershell
uv sync --locked --extra dev --extra colorization --python 3.11
.venv\Scripts\python.exe -m revive --config config.local.toml models status --model ddcolor_tiny
.venv\Scripts\python.exe -m revive --config config.local.toml models install --model ddcolor_tiny
```

The Tiny install explicitly downloads the author's pinned 220 MB Apache-2.0
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
model = "ddcolor_tiny"
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

The earlier Tiny qualification configuration is `.runtime/colorization.toml`.
Its outputs remain in `.runtime/colorized_output/`; the everyday `config.local.toml`
now selects the larger-model quality profile.
See [qualification evidence and limits](docs/COLORIZATION_QUALIFICATION.md) for
source licenses, asset digests, measured memory, and outstanding review gates.

## Lightweight upscaling compatibility profile

SwinIR lightweight x2 is available as an offline CPU stage after normalization,
cleanup and optional colorization. Install the extra and its selected checkpoint
explicitly before enabling it:

```powershell
uv sync --locked --extra dev --extra colorization --extra super-resolution --python 3.11
revive --config config.local.toml models install --model swinir_lightweight_x2
revive --config config.local.toml models status --model swinir_lightweight_x2
```

The `colorization` extra can be omitted when only upscaling is needed. Keep the
extras you use in subsequent `uv sync` commands. The default `models install`
command selects the configured colorizer. Both checkpoints belong to the configuration's
application directory; a separate development configuration uses separate assets.

```toml
[super_resolution]
enabled = true
model = "swinir_lightweight_x2"
target_long_edge = 4096
tile_size = 128
```

Dimensions grow by at most 2x toward the target long edge. Larger originals retain
their dimensions. Transparency bypasses inference and remains at its original size;
exactly neutral RGB images remain neutral after reconstruction. Metadata records
whether enlargement occurred and marks reconstructed details as inferred. Applied
outputs use `_upscaled_` or `_colorized_upscaled_` filenames.

Inference uses 8-pixel-aligned tiles with 32 pixels of context and no full-image
float tensor. Finite context approximates a full-frame pass. The 64-pixel option
reduces tile workspace at the cost of more repeated computation. RAM, Windows
commit and output-disk admission account for larger derivatives; an enabled model
failure does not silently fall back to interpolation. CPU processing can take
minutes and still hit the configured deadline. The shared template remains
disabled pending visual review and broader laptop qualification.

Development configuration: `.runtime/super_resolution.toml`; outputs:
`.runtime/upscaled_output/`. See [qualification evidence](docs/SUPER_RESOLUTION_QUALIFICATION.md)
for source/weight digests, measurements, resource limits and remaining gates.

## Background monitor controls

```powershell
revive --config config.local.toml start
revive --config config.local.toml status
revive --config config.local.toml stop
revive --config config.local.toml restart
```

`start` launches a windowless monitor on Windows and waits for readiness. Repeating
it with the same settings returns the current monitor; changed settings require
`restart`. Stop interrupts an active worker safely, retaining its job for resumption
without consuming a retry attempt. Publication already underway may finish. A
restart continues the durable queue. Stop the monitor before editing failed jobs
with `retry JOB_ID` or installing model checkpoints.

`status` includes running/readiness state, heartbeat, native-event versus
reconciliation mode, and the latest 20 failed/retry-pending jobs with recovery
guidance. Control targets a unique run token rather than terminating a saved PID.
Native watches are reattached after input-directory replacement, and long
scheduling gaps trigger reconciliation.

The development alias `.runtime/background.toml` now uses the same automatic
settings, output and state as `config.local.toml`. Both address the same monitor.
The new state includes existing `test_photos/` on its first run; earlier outputs
remain untouched. Use `start` when you want monitoring to continue.

```powershell
revive --config .runtime/background.toml startup export
```

This exports a current-user, least-privilege login-task definition for review.
Export does not register a task. See [background operation](docs/BACKGROUND_OPERATION.md)
for lifecycle behavior, the validated XML, and deployment/testing work remaining.

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
release gates. No GPU backend, face enhancement, scratch
reconstruction or Drive API exists yet. The automatic template requires both
local model checkpoints to be installed during setup. Broader laptop and visual
quality qualification remain pending.

## Development checks

```powershell
uv sync --locked --extra dev --extra colorization --extra super-resolution --python 3.11
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\ruff.exe check src tests
.venv\Scripts\ruff.exe format --check src tests
```

Tests use synthetic local images and temporary folders, including real subprocess
workers and Windows handle behavior. Symlink tests skip when Windows does not permit
unprivileged symlink creation. No personal photo collection is used by tests.

