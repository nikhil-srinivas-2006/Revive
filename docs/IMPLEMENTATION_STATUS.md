# Implementation status

Updated: 2026-10-21

The user authorized starting implementation after the plan was saved. This document
records progress; the [design plan](IMPLEMENTATION_PLAN.md) remains the target
architecture rather than a claim that all features are already available.

## Current milestone: optional 2x super-resolution

Implemented the offline CPU SwinIR lightweight x2 adapter, explicit model setup,
strict configuration, bounded tiles, at-most-2x output geometry toward 4096 pixels,
RAM/commit/output-disk admission, source neutrality preservation, final EXIF updates,
inferred-detail provenance and upscaled filenames. The shared template remains
disabled; `.runtime/super_resolution.toml` uses separate qualification data/output.

The real-checkpoint synthetic comparison passed: maximum full-vs-tiled difference
was one 8-bit code value. Tiled peak working set/commit were 448.44/1,118.66 MiB.
The photo trial produced three correctly sized upscales; one case timed out and six
failed RAM admission when available memory fell. All ten original hashes remained
unchanged. These incomplete cases were not published as successful results. See
[super-resolution qualification](SUPER_RESOLUTION_QUALIFICATION.md) for exact
measurements, source/weight provenance, local comparison paths and remaining gates.

The timeout trial also exposed an existing Windows launcher containment gap.
Workers now start suspended, enter their Job Object before execution, then resume.
Failure cleanup terminates the whole job before draining inherited pipes. Real
descendant tests cover both behavior and delayed assignment. The suite passes
**142 tests, with 1 skipped**. Ruff and the built wheel's vendor/license/hash and
no-weight checks pass. `.gitattributes` preserves vendor manifest byte hashes.

## Previous experiment: Microsoft global restoration

The optional colorization milestone is implemented. Following the user's request
to assess Microsoft's old-photo restoration project, a separate opt-in benchmark
now acquires pinned source/selected release assets explicitly and runs bounded CPU
comparisons. Quality repair completed all 10 `test_photos/` previews with original
hashes unchanged. Median worker wall time was 6.620 seconds; maximum peak working
set was 506.56 MiB and committed memory was 1,155.07 MiB. Scratch detection and
repair remain unqualified: the automatic RAM gate deferred every detector case.
The offline review page and reports live in ignored benchmark data. Two visual
spot checks show reduced grain accompanied by softer fine details/text, so there
is no automatic promotion into the product pipeline. See
[benchmark evidence and reproduction](MICROSOFT_RESTORATION_BENCHMARK.md).

That experiment reported **119 tests, with 1 skipped**. `.gitattributes` pins
vendored DDColor source/licenses to LF after Windows Git conversion exposed
byte-integrity failures. Architecture contents and recorded hashes are unchanged.

## Implemented product pipeline

Implemented configuration, application path boundaries, native-event discovery,
reconciliation, stability observation, private snapshots, persistent SQLite state,
single-instance ownership, short-lived adapter workers, validated byte-copy output,
atomic publication, crash recovery, bounded retries, incident logging, and CLI
validation/status/export/retry operations. Added a `normalize_png` adapter with
EXIF orientation, ICC/sRGB conversion, transparency preservation, source metadata
retention, heuristic inspection, opt-in bounded denoising/tonal adjustment, and
exact encoded-pixel validation. Cleanup remains disabled in the development config.

The dependency environment uses Python 3.11.16, Pillow 13.0.0 and watchdog 6.0.0.
`uv.lock` captures resolved packages and artifact hashes. Pillow performs validation
and conventional processing. The optional colorization extra is now installed for
development: CPU PyTorch 2.14.1+cpu, NumPy 2.4.6 and headless OpenCV 4.14.0.94.
DDColor Tiny architecture/licensing and the pinned author's checkpoint are recorded
in [colorization qualification](COLORIZATION_QUALIFICATION.md). Weights are stored
only in ignored application data, with size/SHA-256 verification.

The model is loaded only for eligible images inside an isolated worker. Automatic
selection has a full-resolution neutrality gate; color/uncertain/transparent inputs
and sepia by default bypass inference. Chroma is reconstructed with preserved
normalized luminance in 64-row strips. Inferred colors and the maximum numerical
luminance error are recorded in both PNG provenance and the central manifest.

Deliberate initial differences from the target plan:

- Each job gets its own worker instead of a reusable batch worker. This is simpler
  for crash isolation; revisit before AI model loading costs become significant.
- The mock worker preserves encoded bytes; `normalize_png` produces 8-bit sRGB PNG
  derivatives. Higher-bit-depth inputs require later precision-preserving support.
- All reparse points, including cloud placeholders, are rejected until a tested
  cloud-provider policy exists.
- Mutating CLI commands require the monitor to be stopped. Read-only status and
  manifest export operate concurrently.
- Worker physical-RAM admission and Windows committed-memory ceilings are now
  computed separately. The estimates still need a representative laptop matrix.
- Pending jobs are bounded, but history retention and watchdog's internal event
  backlog still need production qualification.
- Native toasts, service/autostart management, stop/reprocess CLI commands,
  face adapters, calibrated visual-quality gates and broader
  performance benchmarks remain unimplemented.

## Validation

Automated cases cover synthetic format decoding, full-process execution, live
arrival, duplicate events/content, slow writes, revisions, queue admission, missing
files, corruption, multipage/pixel limits, input/output boundaries, publication
conflicts, restart recovery, disk-pressure and worker-crash injection, concurrent
read-only status, first-run policy, manifest provenance, Windows sharing modes and
Windows worker lifetime containment. New cases cover all eight EXIF orientations,
TIFF decoder orientation, EXIF sub-IFDs and density, ICC conversion/failure,
transparency, metadata retention, high-bit-depth rejection, deterministic PNG
encoding, exact pixel validation, conservative cleanup, memory/disk admission,
and disk-full errors. The monitor still imports neither Pillow nor PyTorch.

The automation milestone previously passed **39 tests, with 1 skipped**.
The previous combined suite passed **82 tests, with 1 skipped** on Windows. The
colorization milestone now passes **110 tests, with 1 skipped**, including the
optional runtime tests. The skip requires symlink privileges. Ruff lint/formatting
checks pass. Download tests use synthetic assets rather than network access.
Ruff lint and formatting checks passed. CLI configuration validation and doctor
also passed. These original checks preceded configuration of the development photo
folder. A subsequent mock-copy trial completed all 10 files in `test_photos/`;
source and output SHA-256 hashes matched for every file. The first sandboxed trial
encountered filesystem restrictions; retries outside the sandbox completed cleanly.
The conventional-processing milestone also produced PNG derivatives for all 10
development photos, with all original hashes unchanged. These outputs contain
orientation/profile normalization only. Inspection reported seven grayscale
candidates, one sepia candidate, and two color-or-uncertain images. These are
heuristic classifications. No image-quality,
historical-fidelity, peak-memory or idle-resource benchmark result is implied by
functional test success. A Windows symlink test requires Developer Mode or elevated
symlink privileges and otherwise skips.

The subsequent DDColor qualification completed all 10 development photos: six
colorized PNGs and four normalization-only outputs. The full-resolution gate
declined one thumbnail grayscale candidate; sepia and the two other color/uncertain
inputs also bypassed inference. Original hashes remained unchanged. Maximum reported
Lab L error across the six inferred outputs was 0.994873. No facial restoration,
upscaling, or claim of historical color accuracy was made. One output received a
basic developer visual inspection; a representative human quality review remains
an acceptance gate.

A real-checkpoint synthetic probe also passed under automatic limits; exact measured
time and resident/commit peaks are in COLORIZATION_QUALIFICATION.md. Initial bounded
probes exposed a meta-initialization incompatibility and the need to distinguish
Windows commit from resident RAM. The final adapter uses ordinary CPU construction
with a 32-pixel channel-discovery pass, strict loading and the unchanged source
architecture mathematics.

## Next milestone

The product targets a broad range of laptops, not a specific user's PC. CPU-only
compatibility is the baseline; RAM/commit detection is implemented. Optional acceleration
and a representative laptop validation matrix remain planned. Exact hardware from
the user is not required to continue development.

Development input is now `test_photos/` through the ignored `config.local.toml`.
PNG outputs go to `.runtime/png_output/`; state goes to `.runtime/png_state/`.
The earlier `.runtime/mock_output/` and `.runtime/dev_state/` remain intact.
The checked-in template uses environment references instead of the development
machine's drive paths. Private test photographs are excluded from Git.

DDColor Tiny licensing, dependencies, CPU inference and checkpoint integrity have
been qualified for the pinned 256-view CPU path. Optional SwinIR x2 is implemented,
with partial real-photo qualification under the resource/deadline gates. Continue
native failure alerts and operator controls while extending the colorization/SR
review corpus and laptop benchmarks. Microsoft scratch-model qualification and a
Real-ESRGAN runtime comparison remain separate experiments. Automatic color
classification and inferred-detail quality still need broader calibration before
unattended production use.

The separate `.runtime/colorization.toml` uses `.runtime/colorized_output/` and
`.runtime/colorization_state/`. The usual `config.local.toml` remains normalization
only. Explicit model setup was authorized and completed for this qualification;
no runtime weight download, GPU backend or autostart registration was introduced.
