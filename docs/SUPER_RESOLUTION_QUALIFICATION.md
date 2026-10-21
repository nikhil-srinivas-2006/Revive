# Optional SwinIR lightweight x2 qualification

Updated: 2026-10-21. The CPU adapter is implemented and disabled in the shared
configuration. Geometry, bounded tiling, metadata, resource admission and offline
checkpoint setup are integrated into the existing isolated worker pipeline.

## Assets and dependency scope

- Upstream: https://github.com/JingyunLiang/SwinIR
- Architecture revision: `6545850fbf8df298df73d81f3e8cba638787c8bd`.
- Checkpoint: `002_lightweightSR_DIV2K_s64w8_SwinIR-S_x2.pth`, author's v0.0 release.
- Bytes: 17,147,989.
- SHA-256: `193b229909ca89cd8b55de9c9e7fce146ae759d59dfcd78d8feb9dd1d6fa0fd7`.

The repository declares Apache-2.0 and acknowledges Swin-Transformer and KAIR.
Their MIT license notices are also packaged. `src/revive/_vendor/swinir/upstream.json`
records exact upstream/packaged hashes and original license Git blob identifiers.
The checkpoint release has no independently published digest or separate weight
license statement. The digest above was pinned from the author's HTTPS release;
it authenticates subsequent bytes against this acquisition, not against an
independent signature. Weights remain local setup assets and are not in the wheel.

The only architecture edit redirects timm's helper import to local helpers:
native PyTorch truncated-normal initialization, integer/tuple conversion and an
inference-only DropPath identity. Stochastic-depth training is rejected. Model
construction matches upstream lightweight SR settings: RGB, scale 2, image size 64,
window size 8, depths [6,6,6,6], embed dimension 60, heads [6,6,6,6], MLP ratio 2,
pixelshuffledirect upsampler, 1conv residual connection. Parameters load strictly
through `torch.load(weights_only=True)` from the same verified read-only handle.

The `super-resolution` extra uses CPU PyTorch and NumPy; it requires neither timm,
torchvision nor OpenCV. The existing colorization extra remains available. Both
extras resolve torch through the explicit CPU index; `uv.lock` records versions
and hashes. The monitor imports neither ML libraries nor the pixel pipeline.

## Processing and resource behavior

The stage runs after colorization. Target geometry is at most 2x toward a configured
long edge of 64..4096 pixels, default 4096. It never shrinks an already larger image.
Opaque input is reconstructed at native 2x and, if needed, resized down to the exact
planned dimensions. This temporary native image is included in memory admission.
Transparent input bypasses enlargement. Exactly neutral RGB input is tested across
all pixels and reconstructed output is made neutral; even one colored source pixel
prevents that conversion.

Default cores are 128x128, optionally 64x64. Each tile includes 32 pixels of context,
aligned to the global 8-pixel attention-window grid. The final edge is symmetrically
padded and the result is cropped. Only the core is pasted into an 8-bit canvas;
there is no whole-image floating tensor or overlap-weight accumulator. Finite
context is approximate, not a claim of global inference equivalence.

Physical admission reserves 512 MiB plus conservative source/native/final-image
allowances; commit admission adds another 768 MiB. These are estimates, not laptop
minimum specifications. The Windows Job Object applies a hard per-process commit
ceiling, computed separately from physical availability. Disk admission includes
the enlarged PNG and configured free-space reserve. Output pixels are capped by
`resources.max_output_megapixels` (default 64). Requested inference failures report
failure/retry; there is no silent interpolation success.

The milestone also corrected Windows launcher containment. Workers are created
suspended, assigned to their Job Object, then resumed through documented Windows
thread APIs. Launchers and future descendants inherit containment. Error/timeout
cleanup closes the job before draining inherited pipes, avoiding a hang when only
the launcher PID was killed. Tests check real interpreter descendants, including a
deliberate delay before assignment, and verify pipe closure and process termination.

Output EXIF dimensions describe the final derivative, orientation/profile handling
is retained, and the normal PNG pixel/dimension/profile validation still runs.
Applied outputs are labelled `_upscaled_` or `_colorized_upscaled_`; provenance
contains model/source hashes, tile/context settings, actual dimensions and
`details_inferred`. Timing and peak-memory observations are stored only in the
central manifest so they do not make PNG bytes nondeterministic. DDColor's recorded
luminance bound describes its colorization stage before SR reconstructs pixels.

## Real-checkpoint synthetic evidence

`scripts/qualify_swinir.py` runs in the same suspended-start Windows containment and
uses only generated pixels. It checks tiled inference against a full-frame
reference. The reference intentionally requires more memory than production's
bounded tile path and has its own admission estimate.

264x264 synthetic RGB, default 128 cores, nine tiles:

| Observation | Measured result |
| --- | --- |
| Whole probe wall time inside child | 55.776 seconds |
| Tiled phase peak working set / committed memory | 448.44 / 1,118.66 MiB |
| Whole probe peak working set / committed memory | 757.09 / 1,592.49 MiB |
| Automatic physical budget / hard commit ceiling | 905 / 2,048 MiB |
| Full vs tiled mean absolute difference, 8-bit values | 0.000062175 |
| Mean absolute difference near tile boundaries | 0.000278308 |
| Maximum absolute difference | 1 code value |

The initial numerical regression limits are mean <=1, boundary mean <=2 and
maximum <=16. They passed on this control; they are not calibrated perceptual
quality or historical-fidelity scores. Earlier 64-core testing on a smaller
136x120 control also passed. Real photographs and other hardware can differ.

## Development photo qualification

Input remains `test_photos/`. `.runtime/super_resolution.toml` enables SR separately,
with `.runtime/upscaled_output/` and `.runtime/upscaling_state/`. Other normalization
and colorization configurations/outputs remain intact. The development run uses
one attempt per failure to avoid repeating long qualification jobs. Standard
configuration retry behavior remains unchanged.

The bounded run finished with **3 completed upscales, 1 timeout and 6 RAM-admission
failures**. The failed cases are explicitly recorded, not normalization-only
successes. Available memory fell during the run; no automatic budget or deadline
was raised to force completion. A previous 64-core attempt also timed out on the
1.08 MP input. The development state retains that history as skipped old-policy
jobs; the figures here describe the ten current 128-core jobs.

| Input size | Output size | Worker seconds | Peak working set / commit, MiB |
| --- | --- | --- | --- |
| 447x612 | 894x1224 | 173.900 | 485.20 / 1,145.20 |
| 479x640 | 958x1280 | 58.084 | 507.55 / 1,180.97 |
| 450x450 | 900x900 | 49.425 | 485.01 / 1,151.15 |

All ten original hashes match the pre-existing normalization qualification
manifest. The verification record is
`.runtime/upscaling_state/original_hash_verification.json`. Every completed output
was fully decoded, matched its planned pixels/dimensions/profile and was published
without overwriting an existing derivative.

The local same-size comparison is
`.runtime/upscaling_state/review/index.html`, with Lanczos baselines, full-size AI
outputs and a JSON timing/error/hash report. Regenerate it with
`scripts/review_super_resolution.py --config .runtime/super_resolution.toml`.
A developer portrait spot check shows sharper edges and texture, including some
emphasized grain. This is not evidence of recovered authentic detail or a calibrated
identity/fidelity assessment. The incomplete cases and wider hardware corpus remain
qualification work.

The complete regression suite passes **142 tests, with 1 skipped** (Windows
symlink privilege). Thirty SR tests cover geometry, configuration/fingerprints,
odd-edge tile coverage, invalid model output, neutral-color preservation,
source/asset integrity, resource admission, final EXIF/provenance, combined-stage
order and publication naming. Two additional tests cover inherited-pipe timeout
cleanup and real Windows descendant containment. Ruff and wheel checks pass; the
wheel includes the pinned architecture and license notices and excludes weights.

## Reproduce and release gates

```powershell
uv sync --locked --extra dev --extra super-resolution --python 3.11
revive --config config.local.toml models install --model swinir_lightweight_x2
revive --config config.local.toml models status --model swinir_lightweight_x2
```

Enable `[super_resolution]` only in the intended configuration. To repeat the
synthetic probe in this workspace:

```powershell
.venv\Scripts\python.exe scripts/qualify_swinir.py --config .runtime/super_resolution.toml
```

Setup is explicit network acquisition; run/doctor/status never fetch weights.
Human review of texture, text, color, identity and seams is still required, along
with representative laptops, larger scans and real combined DDColor/SwinIR peak
measurements. Synthetic checks do not establish that AI SR improves old photos.
Real-ESRGAN comparison, face processing and Microsoft scratch-model qualification
remain separate work; none is enabled by this milestone.
