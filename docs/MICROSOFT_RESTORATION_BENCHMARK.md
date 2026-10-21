# Microsoft global restoration experiment

Updated: 2026-10-21. This is a development benchmark, separate from Revive's
default pipeline. The quality-only path ran successfully; scratch detection and
scratch repair remain unqualified because automatic RAM admission deferred them.

## What is implemented

`scripts/prepare_microsoft_benchmark.py` explicitly acquires the MIT-licensed
architecture at revision `33875eccf4ebcd3665cf38cc56f3a0ce563d3a9c` and selected
inference checkpoints from Microsoft's v1.0 release. HTTP byte ranges avoid
downloading the entire 2,036,400,762-byte archive. The six selected members occupy
812,102,772 bytes unpacked, about 746 MB compressed, excluding small range-read
overhead. Training checkpoints and the face archive are omitted. The detector's
single supplied checkpoint includes training state internally; no training runs.

Acquisition validates bounded ranges, member sizes and ZIP CRCs, then records
SHA-256 digests. Microsoft publishes no digest for this release: these are
**trust-on-first-acquisition hashes**, not independent publisher authentication.
Later inference verifies checkpoint bytes on the same handle passed to
`torch.load(weights_only=True)`, uses strict state loading, and downloads nothing.
Source, original license, weights, acquisition records and personal comparisons
stay under ignored `.runtime/microsoft_benchmark/`.

`scripts/benchmark_microsoft.py` reads `test_photos/` by default, creates an
orientation-corrected sRGB baseline, and runs separate CPU workers for detection,
quality repair and scratch repair. Each worker uses two CPU threads, a 120-second
timeout and Revive's automatic Windows committed-memory ceiling. A pipe handshake
holds ML imports until Windows Job Object attachment succeeds. Timeout/crash/RAM
failures appear in the report; a missing detector prevents scratch repair.

The parent stores source hashes, verifies originals after processing, validates
output dimensions/modes, and writes an offline `review.html` with a JSON report.
Pixel-change values describe how much the model altered; they are not quality
scores. Experiments return exit status 2 if any selected case is incomplete.

## Deliberate benchmark settings

- Fit previews inside 256 by 256 pixels without enlargement or cropping. Reflect
  padding to multiples of 16 is removed from the output. These are small preview
  derivatives, not full-resolution restored photographs.
- Use Microsoft's quality mapping, or its non-local scratch mapping with the
  predicted binary mask. Detector threshold is 0.4, mask dilation is 0, and the
  supplied encoder/decoder weights are selected as in upstream `Global/test.py`.
- Use the padded preview for detection. Upstream normally enlarges the detector's
  short edge to 256; this bounded trial does not. Detection quality at this setting
  has not yet been measured.
- Convert generated values with a fixed [-1,1] to [0,255] scale. Upstream's
  per-image display contrast normalization is omitted so it cannot inflate a
  visual comparison. Full-resolution/upstream-presentation equivalence is not
  claimed.
- No face enhancement, alignment, colorization, super-resolution, GPU use, runtime
  weight downloads, or automatic integration into the unattended engine.

The architecture code remains in its original downloaded form. The loader makes
four reviewed import adaptations in memory: remove the unused training-only
torchvision import; make ImagePool imports relative in two modules; remove the
unused multi-GPU wrapper import and make the detector's antialiasing import
relative. The detector is instantiated with `sync_bn=False`; its inference layers
already use ordinary BatchNorm2d. No layer or restoration mathematics is rewritten.
All import replacements are recorded in each report and require exact matches.

## Measured development result

Run: `.runtime/microsoft_benchmark/runs/20261021T120815950446Z/`.

| Observation | Result |
| --- | --- |
| Inputs / originals unchanged | 10 / 10 |
| Quality-repair outputs | 10 / 10 |
| Quality worker wall time, min / median / max | 5.790 / 6.620 / 8.165 seconds |
| Maximum quality worker peak working set | 506.56 MiB |
| Maximum quality worker peak committed memory | 1,155.07 MiB |
| Worker committed-memory ceiling | 2,048 MiB |
| Automatic physical budgets observed | 612–808 MiB |
| Scratch detectors / scratch repairs completed | 0 / 0 |

The experimental detector admission estimate is 896 MiB; scratch mapping is
768 MiB and quality mapping is 512 MiB. These conservative physical estimates
are provisional, not measured laptop minimums. The detector was deferred before
loading on every case; scratch repair then recorded `DETECTOR_UNAVAILABLE`.
No detector speed, memory peak, success, or scratch-removal-quality claim follows.

Two developer visual spot checks show reduced grain with softer facial features,
fabric detail and text. This is a reason to retain the model as an experiment.
No clean reference, calibrated fidelity metric, or representative human panel
exists for this set. A successful process run does not establish an improvement.
The current comparisons are suitable for reviewing smoothing tradeoffs; they
cannot decide whether scratch repair is effective.

## Reproduce

From the repository root in the optional CPU colorization environment:

```powershell
.venv\Scripts\python.exe scripts/prepare_microsoft_benchmark.py sources
.venv\Scripts\python.exe scripts/prepare_microsoft_benchmark.py weights --inference
.venv\Scripts\python.exe scripts/benchmark_microsoft.py --input test_photos
```

Preparation is explicit network setup. The benchmark itself runs offline and
creates a new timestamped run each time.

Remaining acceptance work: repeat detection/repair when automatic memory
admission permits it; measure their actual peaks; examine false scratch masks,
watermark/text damage, face identity, texture and composition; add clean/damaged
control pairs; and measure representative laptops. Do not promote this model to
the default pipeline until those gates pass. Product super-resolution remains a
separate unimplemented milestone.

## Verification

The complete suite passes **119 tests, with 1 skipped** on Windows. Nine offline
benchmark tests cover input orientation/composition, transparency and animation
rejection, low-memory admission, containment-before-start and timeout cleanup,
source/checkpoint tampering, escaped review filenames, range validation and range
caching. The skip elsewhere requires Windows symlink privileges. Ruff checks pass.
The source-integrity regression also exposed Git CRLF conversion of DDColor's
vendored files; `.gitattributes` now pins their original LF bytes to preserve the
existing digest manifest without changing model code.

Upstream references: [repository](https://github.com/microsoft/Bringing-Old-Photos-Back-to-Life),
[v1.0 release](https://github.com/microsoft/Bringing-Old-Photos-Back-to-Life/releases/tag/v1.0),
[pinned restoration entry point](https://github.com/microsoft/Bringing-Old-Photos-Back-to-Life/blob/33875eccf4ebcd3665cf38cc56f3a0ce563d3a9c/Global/test.py).
