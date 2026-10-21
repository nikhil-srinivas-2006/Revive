# DDColor Tiny qualification

Date: 2026-10-21. Status: local CPU integration and functional qualification;
unattended visual-quality acceptance and a representative laptop matrix remain open.

## Provenance and licenses

| Asset | Pinned source |
|---|---|
| Architecture | [Official DDColor repository](https://github.com/piddnad/DDColor), revision `2adb63f2656ac41cbdf7b894cddd94121a3faf13` |
| Checkpoint | [Author's DDColor Tiny model card](https://huggingface.co/piddnad/ddcolor_paper_tiny), revision `cf9fd99c1d7472689ec7413441c1b799a51866a3` |
| Checkpoint size | 220,372,845 bytes |
| Published LFS SHA-256 | `8a1277bc90a1bfbb6d2d83933a9a6bc821931879ca93e26e4fcec12165d41fce` |
| Model configuration | ConvNeXt Tiny; MultiScaleColorDecoder; 100 queries; 3 scales; 9 decoder layers; two output chroma channels |
| Upstream reference view | 512 × 512; Revive initially qualifies a 256 × 256 CPU view |

DDColor code uses Apache-2.0. The author-hosted checkpoint's model card explicitly
declares Apache-2.0. This is evidence for that exact checkpoint, not an assumption
that all related models or downstream conversions have the same terms. The official
[model zoo](https://github.com/piddnad/DDColor/blob/master/MODEL_ZOO.md) links the
Tiny variant. Do not substitute an unverified mirror.

Only five architecture files are vendored. The four BasicSR import prefixes in
`model.py` are changed to local relative imports; helper mathematics is retained.
`upstream.json` records source/packaged hashes. DDColor's Apache license, original
Meta ConvNeXt MIT license, and copyright/DETR attribution are packaged. No upstream
training package, torchvision, timm, remote-code loader, or Hugging Face SDK is
required. Optional dependency license/security review should be refreshed before
distribution; this document is not a legal opinion.

## Runtime design

- Optional extra: CPU-only PyTorch, NumPy, headless OpenCV. Versions are locked in
  `uv.lock`; base normalization does not require or import them.
- Explicit `models install` downloads the one pinned checkpoint, with a streaming
  size limit and SHA-256 check before atomic no-replace publication. Runtime never
  downloads missing assets. Status and inference validate existing weights offline.
- A read handle remains open across hashing and loading. PyTorch uses
  `weights_only=True`, CPU mapping, strict state matching and parameter assignment;
  there is no unsafe full-model pickle fallback.
- CPU initialization uses a 32-pixel dummy encoder pass to discover channel widths;
  all checkpoint parameter shapes are checked. Actual inference uses the configured
  view size. The tested implementation uses ordinary CPU construction: meta-device
  construction was not retained after qualification failures.
- Two CPU threads by default; no GPU auto-selection. One model worker processes
  one image and exits, releasing model/runtime allocations.

## Fidelity policy

Automatic eligibility requires a grayscale candidate plus a full-resolution check:
no pixel has a channel difference above four 8-bit levels, and at least 99.9% have
channel differences at most two. This conservative gate can skip scans with chroma
noise. It prevents a tiny colored object hidden by the inspection thumbnail from
being silently recolorized.

Transparent inputs, color images, uncertain inputs and sepia candidates bypass
inference. Sepia requires explicit `include_sepia = true` until broader calibration.
These classifications are heuristic and cannot establish photographic history.

The model predicts only Lab a/b. Full-resolution source L is combined with inferred
chroma in 64-row strips, retaining source dimensions and detail structure. The
luminance reference is the image after profile/orientation normalization and any
explicitly enabled conventional cleanup. Chroma
strength is bounded to 0–1 and a/b values to ±110. Where RGB gamut conversion changes
L too much, inferred chroma is progressively reduced. The encoded result must stay
within 1.0 Lab L unit of source L per pixel or fail `QUALITY_REGRESSION`. This is a
numerical guard, not a guarantee of identity, plausible skin tone, artifact-free
color, or visual improvement.

Both PNG provenance and the central manifest explicitly mark inferred colors and
record the checkpoint digest, source revision, backend, strength, view size and
observed maximum L error. The general template keeps colorization disabled pending
review. Enabling it does not silently rerun previously completed jobs.

## Memory findings and measured probe

Windows job limits account for committed memory, which differs substantially from
resident physical memory. Deriving a committed-memory cap directly from available
physical RAM caused safe but unnecessary refusals. Automatic admission now uses a
physical-RAM estimate; the independently bounded job ceiling uses available system
commit, total physical RAM and a 2 GiB maximum. An explicit positive
`worker_memory_mib` applies to both. Neither setting promises a hard resident-RAM cap.

The 256-view admission estimate reserves 896 MiB plus image buffers in physical RAM
and 1792 MiB plus buffers in committed memory. The experimental 512-view estimates
are 1792/3072 MiB respectively, so a larger explicit ceiling may be required. These
are provisional heuristics, not measured minimum system requirements.

One successful synthetic 128 × 96 grayscale-ramp probe, with a 256 × 256 model view:

| Measurement | Observed value |
|---|---|
| Automatic physical-RAM budget | 1163 MiB |
| Enforced committed-memory ceiling | 2048 MiB |
| Child load + inference + reconstruction | 5.449 s |
| Peak working set | 767.99 MiB |
| Peak process commit | 1566.04 MiB |
| Maximum reconstructed Lab L error | 0.3479 |

After imports alone, peak working set was 197.10 MiB while peak commit was
791.40 MiB. The probe ran after earlier attempts, so filesystem caches may have
been warm. These are actual observations from one Windows machine, not published
benchmarks, cold-start claims, cross-laptop results, or quality validation.

Reproduce after explicit setup:

```powershell
.venv\Scripts\python.exe scripts/qualify_ddcolor.py --config config.local.toml
```

The probe uses a synthetic image and the configured checkpoint. It never downloads
weights or increases the configured automatic commit ceiling. `--debug` includes
exception causes for this developer probe only; normal incidents remain path-free.

The development photo trial completed ten images: six colorized and four bypassed
to normalization (one uncertain sepia, two color/uncertain, one declined by the
full-resolution neutrality check). All source hashes were unchanged. The maximum
reported Lab L error across colorized samples was 0.994873. One output was visually
inspected for obvious artifacts; this is not the representative human review gate.
The complete suite passed 110 tests with one Windows symlink-privilege skip.

A wheel build succeeded and included the architecture, source manifest and required
license/notice files without bundling model weights or runtime binaries. CPU index
selection is configured by `uv.lock`/`pyproject.toml`; standalone pip users must
select CPU PyTorch wheels explicitly rather than assume wheel metadata chooses an
index. Fresh offline installation remains a release test.

## Remaining release gates

- Human review on portraits, groups, text, landscapes, sepia and intentionally
  limited-palette color photos; no accuracy claim from numerical tests alone.
- Representative 8/16 GiB laptop measurements and low-memory/error recovery checks.
- Qualification of the 512 view, backend/dependency upgrades, and eventual ONNX or
  other acceleration only if measured benefit warrants it.
- Fresh offline installation from the lockfile and separately acquired verified
  assets, followed by a longer unattended soak.
