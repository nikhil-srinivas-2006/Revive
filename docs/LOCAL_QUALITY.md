# Larger local colorization and restoration

The free local quality candidate uses DDColor-L ModelScope for colorization and
Real-ESRGAN x2plus for tiled enlargement. It runs on CPU with the existing optional
extras; it does not require BasicSR, torchvision, Hugging Face Hub, an API key or
a cloud service at runtime. Face reconstruction and scratch inpainting remain off.

## Setup and configuration

```powershell
uv sync --locked --extra dev --extra colorization --extra super-resolution --python 3.11
.venv\Scripts\python.exe -m revive --config config.local.toml models install --model ddcolor_modelscope
.venv\Scripts\python.exe -m revive --config config.local.toml models install --model realesrgan_x2plus
.venv\Scripts\python.exe -m revive --config config.local.toml doctor
.venv\Scripts\python.exe -m revive --config config.local.toml start
```

Downloads are explicit setup operations. The DDColor checkpoint is 911,914,869
bytes; Real-ESRGAN is 67,061,725 bytes. Their immutable revisions/release URLs,
sizes, and SHA-256 digests are pinned. Every load verifies a read handle before
using `torch.load(weights_only=True)` and strict architecture loading. Neither
checkpoint is bundled in the wheel. Allow disk space for staging downloads.

```toml
[colorization]
enabled = true
model = "ddcolor_modelscope"
input_size = 512
strength = 0.85
include_sepia = false

[super_resolution]
enabled = true
model = "realesrgan_x2plus"
target_long_edge = 4096
tile_size = 128

[resources]
worker_memory_mib = 0
worker_timeout_seconds = 1800
cpu_threads = 2
```

Omitting `model` retains old behavior (`ddcolor_tiny`, `swinir_lightweight_x2`).
Both old selections remain supported. New setup templates explicitly select the
larger models. Models are chosen independently; unknown names are rejected.
`models status/install` without `--model` addresses the configured colorizer.
Doctor reports the selected models and verifies their checkpoints.

Enabling stages still means automatic eligibility checks, not forced processing.
Full-resolution color protection, conservative sepia/transparency bypasses,
original Lab luminance retention, at-most-2x resizing, output pixel/disk limits,
PNG validation, atomic no-overwrite publication, and provenance remain active.
Selecting a different model changes the pipeline fingerprint. It does not
implicitly reprocess completed jobs in an existing database.

## Memory and tiling

For the larger colorizer, automatic budgets reserve half the currently available
physical RAM/system commit and cap each worker at 12 GiB and half total RAM.
Missing memory readings use the conservative 512 MiB fallback. Existing lightweight
profiles retain their earlier 2 GiB automatic ceiling. Explicit quality budgets
above 12 GiB are rejected; a positive budget sets a ceiling, not a reservation or
a guarantee that this RAM is free. Windows enforces a committed-memory Job Object
limit; the physical budget is an admission estimate, not an OS working-set limit.

The 256-view larger-colorizer estimates are 2048 MiB physical / 4096 MiB commit,
plus image buffers. The 512-view estimates are 3072 / 5120 MiB plus buffers.
These are provisional admission heuristics; measured qualification is recorded
separately. Low-memory admission waits without exhausting attempts. Actual memory
allocation failures and timeouts keep their bounded failure/retry semantics.

DDColor's encoder hook cycles are collected after inference before loading the
upscaler. The two models are not intentionally retained together. Real-ESRGAN
uses the author's EMA weights, 23 RRDB blocks, CPU float32, native 2x output and
10 reflected right/bottom pre-padding pixels. Tiles have 32-pixel context and even
coordinates for pixel-unshuffle alignment. As with upstream tiled inference,
finite context is approximate: seams/edge differences need visual review.
Unchanged exact grayscale inputs remain neutral if colorization was bypassed.

## Trial and comparison

The development input remains `test_photos/`. The new local profile uses
`.runtime/quality_output/` and `.runtime/quality_state/`; the old automatic profile
is preserved in `.runtime/legacy_automatic.toml`. Changing paths uses new state so
already completed development images can be compared without resetting history.
The current development profile explicitly sets a 6144 MiB worker ceiling and a
1800-second deadline, within the user's 12 GiB authorization. This is a ceiling,
not a claim that 6 GiB is currently free. New installations retain automatic
budgeting in the shared template.

```powershell
# Explicit bounded trial on the three smallest originals, under production worker limits:
.venv\Scripts\python.exe scripts/qualify_local_models.py --config config.local.toml --limit 3

# Process the full current folder, then stop:
.venv\Scripts\python.exe -m revive --config config.local.toml run --once

# Compare published results with the previous local pipeline offline:
.venv\Scripts\python.exe scripts/review_local_quality.py --config config.local.toml --previous-config .runtime/legacy_automatic.toml
```

Open `.runtime/quality_state/review/index.html` directly in Chrome or Edge. The
offline viewer offers a draggable before/after divider, side-by-side mode, photo
thumbnails, arrow-key navigation, fit/2x/3x zoom, and new/previous-result selection.
Both images share the same displayed geometry. Missing results are shown explicitly.
The page embeds a snapshot of the manifest; rerun the review command and reload
the page after subsequent test runs to include newly processed photos.

The trial stores results, hash verification, applied stages, time and Windows
peak memory in a unique subdirectory of `.runtime/quality_state/qualification/`.
It preserves originals and does not mark those samples completed in the normal
queue. The separate review command compares normal published results and records
failed/deferred jobs explicitly. Compare at equal displayed size and inspect faces,
color bleeding, grain and tile boundaries. More pixels or passing tests alone do
not establish better quality or historically accurate color/detail.

Sources: [DDColor model choices](https://github.com/piddnad/DDColor/blob/master/MODEL_ZOO.md),
[Real-ESRGAN](https://github.com/xinntao/Real-ESRGAN), and
[BasicSR RRDBNet](https://github.com/XPixelGroup/BasicSR).
DDColor/BasicSR source notices and Apache-2.0 licenses, and the Real-ESRGAN
BSD-3-Clause project license, are retained with the packaged architecture.

## Real-checkpoint trial, 2026-10-26

Both checkpoints passed pinned size/hash verification and strict architecture
loading. A three-photo trial used the production worker Job Object and the
6144 MiB ceiling. It completed all three, with source SHA-256 hashes unchanged:

| Input | Applied stages | Worker seconds | Peak working set / commit, MiB |
| --- | --- | --- | --- |
| 199x253 | Real-ESRGAN; colorization declined by full-resolution gate | 6.991 | 419.75 / 1040.41 |
| 184x274 | DDColor-L 512 view, then Real-ESRGAN | 16.574 | 1958.95 / 3210.32 |
| 480x304 | Real-ESRGAN; existing/uncertain color retained | 18.300 | 466.15 / 1150.15 |

The combined output's maximum pre-SR Lab L error was 0.988770, below the existing
1.0 numerical bound. All outputs were reopened and matched the planned decoded
pixels/geometry/profile. The report and original/new review are in
`.runtime/quality_state/qualification/9ab7aa1915cd47e9b83cba321b793345/`.
A previous cold-ish SR-only trial took 21.345 seconds for the 199x253 input;
cache/other workloads affect timing. These are observations from this development
machine, not guaranteed latency, calibrated memory minima, or proof of improved
quality. One resulting colorized portrait received a developer spot check; wider
human review, identity fidelity and seam/artifact assessment remain necessary.

The subsequent full-folder run completed all 12 development photos: seven were
colorized, five retained their existing/uncertain colors, and all twelve were
upscaled. All original hashes matched the pre-run inventory, and all published
PNGs passed checksum, decode, geometry and orientation checks. Worker times ranged
from 8.057 to 376.025 seconds; maximum peak working set was 2230.15 MiB and maximum
commit was 3324.99 MiB. This is CPU batch processing, not an instant enhancement
claim. Summary: `.runtime/quality_state/qualification_summary.json`; offline
original/previous/new comparison: `.runtime/quality_state/review/index.html`.
The continuous everyday monitor is running with native events after this batch.
