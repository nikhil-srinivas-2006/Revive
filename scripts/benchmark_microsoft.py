"""Opt-in, CPU-only global restoration comparison; never part of the monitor.

Run prepare_microsoft_benchmark.py first. This experiment uses 256-long-edge
previews, not full-resolution restoration. No face processing or colorization.
"""

import argparse
import gc
import hashlib
import html
import importlib
import importlib.util
import io
import json
import os
import platform
import subprocess
import sys
import time
import types
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from revive.paths import check_plain_path, signature, source_reader
from revive.processes import WorkerJob
from revive.resources import (
    memory_mib,
    process_memory_mib,
    worker_budget_mib,
    worker_commit_budget_mib,
)

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / ".runtime" / "microsoft_benchmark"
REVISION = "33875eccf4ebcd3665cf38cc56f3a0ce563d3a9c"
MODES = ("detect", "quality", "scratch")
MIN_PHYSICAL_MIB = {"detect": 896, "quality": 512, "scratch": 768}
REQUIRED_WEIGHTS = (
    "checkpoints/restoration/VAE_A_quality/latest_net_G.pth",
    "checkpoints/restoration/VAE_B_quality/latest_net_G.pth",
    "checkpoints/restoration/VAE_B_scratch/latest_net_G.pth",
    "checkpoints/restoration/mapping_quality/latest_net_mapping_net.pth",
    "checkpoints/restoration/mapping_scratch/latest_net_mapping_net.pth",
    "checkpoints/detection/FT_Epoch_latest.pt",
)
# Only remove unused optional imports; inference architecture mathematics unchanged.
PATCHES = {
    "models/networks.py": [
        (
            "from torchvision import models",
            "# Training VGG import omitted for inference",
        )
    ],
    "models/mapping_model.py": [
        (
            "from util.image_pool import ImagePool",
            "from ..util.image_pool import ImagePool",
        )
    ],
    "models/NonLocal_feature_mapping_model.py": [
        (
            "from util.image_pool import ImagePool",
            "from ..util.image_pool import ImagePool",
        )
    ],
    "detection_models/networks.py": [
        (
            "from detection_models.sync_batchnorm import DataParallelWithCallback",
            "# GPU training wrapper omitted; sync_bn=False",
        ),
        (
            "from detection_models.antialiasing import Downsample",
            "from .antialiasing import Downsample",
        ),
    ],
}


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def load_manifest():
    manifest = json.loads((ASSETS / "acquisition.json").read_text())
    if manifest["revision"] != REVISION:
        raise ValueError("Unexpected source revision")
    return manifest


def upstream(manifest):
    """Import pinned local source in its own namespace, with audited import edits."""
    prefix = "revive_microsoft_experiment"
    source_root = ASSETS / "source" / "Global"
    package = types.ModuleType(prefix)
    package.__path__ = [str(source_root)]
    sys.modules[prefix] = package
    # Check every downloaded file before importing any upstream code.
    for name, expected in manifest["sources"].items():
        path = ASSETS / "source" / name
        check_plain_path(path)
        if sha256(path) != expected:
            raise ValueError(f"Source digest mismatch: {name}")
    order = (
        "models/networks.py",
        "models/NonLocal_feature_mapping_model.py",
        "models/mapping_model.py",
        "detection_models/networks.py",
    )
    for relative in order:
        replacements = PATCHES[relative]
        module_name = prefix + "." + relative.removesuffix(".py").replace("/", ".")
        importlib.import_module(module_name.rsplit(".", 1)[0])
        path = source_root / relative
        source = path.read_text(encoding="utf-8")
        for old, new in replacements:
            if source.count(old) != 1:
                raise ValueError(f"Compatibility edit does not match: {relative}")
            source = source.replace(old, new)
        module = types.ModuleType(module_name)
        module.__file__ = str(path)
        module.__package__ = module_name.rsplit(".", 1)[0]
        sys.modules[module_name] = module
        # Executable upstream code is explicitly acquired and hash checked above.
        exec(compile(source, str(path), "exec"), module.__dict__)  # noqa: S102
    return prefix


def checkpoint(manifest, name):
    """Verify the same handle consumed by the restricted PyTorch deserializer."""
    import torch

    record = manifest["weights"][name]
    path = ASSETS / "weights" / record["file"]
    check_plain_path(path)
    with path.open("rb") as stream:
        if os.fstat(stream.fileno()).st_size != record["bytes"]:
            raise ValueError("Checkpoint size mismatch")
        if hashlib.file_digest(stream, "sha256").hexdigest() != record["sha256"]:
            raise ValueError("Checkpoint digest mismatch")
        stream.seek(0)
        return torch.load(stream, weights_only=True, map_location="cpu")


def options():
    # Defaults from pinned BaseOptions, plus test.py parameter_set overrides.
    return SimpleNamespace(
        mc=64,
        start_r=1,
        spatio_size=64,
        feat_dim=-1,
        use_segmentation_model=False,
        mapping_net_dilation=1,
        NL_res=True,
        NL_fusion_method="combine",
        correlation_renormalize=True,
        softmax_temperature=1.0,
        use_self=False,
        cosin_similarity=False,
    )


def to_tensor(image):
    import numpy as np
    import torch

    array = np.array(image, dtype=np.float32, copy=True) / 127.5 - 1
    if array.ndim == 2:
        array = array[:, :, None]
    return torch.from_numpy(array.transpose(2, 0, 1).copy()).unsqueeze(0)


def make_preview(data):
    """Validate decode, normalize orientation/profile, fit without crop/stretch."""
    from PIL import Image, ImageOps

    from revive.pipeline import SUPPORTED_MODES, to_srgb

    with Image.open(io.BytesIO(data)) as probe:
        if (
            probe.width * probe.height > 24_000_000
            or getattr(probe, "n_frames", 1) != 1
        ):
            raise ValueError("Benchmark supports single-frame photos up to 24 MP")
        probe.verify()
    with Image.open(io.BytesIO(data)) as decoded:
        decoded.load()
        if decoded.mode not in SUPPORTED_MODES:
            raise ValueError("Unsupported benchmark precision/mode")
        oriented = ImageOps.exif_transpose(decoded)
        normalized, profile = to_srgb(oriented, [], [])
        oriented.close()
    if normalized.mode != "RGB":
        normalized.close()
        raise ValueError("Transparent benchmark inputs require separate qualification")
    original_size = normalized.size
    normalized.thumbnail((256, 256), Image.Resampling.LANCZOS)
    if min(normalized.size) < 64:
        normalized.close()
        raise ValueError("Preview short edge below 64 is outside this experiment")
    return normalized, original_size, profile


def padded(image):
    import numpy as np
    from PIL import Image

    array = np.array(image)
    bottom, right = (-image.height) % 16, (-image.width) % 16
    return Image.fromarray(
        np.pad(array, ((0, bottom), (0, right), (0, 0)), mode="reflect")
    )


def run_child(args):
    # Parent attaches the Windows job BEFORE ML imports or checkpoint allocation.
    if sys.stdin.readline().strip() != "GO":
        raise ValueError("Parent containment handshake missing")
    start = time.perf_counter()
    import numpy as np
    import torch
    from PIL import Image

    torch.set_num_threads(2)
    torch.set_num_interop_threads(1)
    torch.manual_seed(0)
    manifest = load_manifest()
    prefix = upstream(manifest)
    source = args.case / "baseline.png"
    with Image.open(source) as im:
        image = padded(im.convert("RGB"))
        size = im.size
    weights = "checkpoints/restoration/"
    with torch.inference_mode():
        if args.child == "detect":
            cls = importlib.import_module(prefix + ".detection_models.networks").UNet
            model = cls(
                in_channels=1,
                out_channels=1,
                depth=4,
                conv_num=2,
                wf=6,
                padding=True,
                batch_norm=True,
                up_mode="upsample",
                with_tanh=False,
                sync_bn=False,
                antialiasing=True,
            )
            state = checkpoint(manifest, "checkpoints/detection/FT_Epoch_latest.pt")
            model.load_state_dict(state["model_state"], strict=True, assign=True)
            del state
            gc.collect()
            model.eval()
            mask = torch.sigmoid(model(to_tensor(image.convert("L")))) >= 0.4
            values = mask[0, 0].to(torch.uint8).numpy()[: size[1], : size[0]] * 255
            result_image = Image.fromarray(values)
            result_image.save(args.case / "mask.png")
            details = {"scratch_fraction": float(np.mean(values > 0))}
        else:
            networks = importlib.import_module(prefix + ".models.networks")
            opt = options()
            generator_a = networks.GlobalGenerator_DCDCv2(
                3, 3, 64, 4, 3, networks.get_norm_layer("instance"), opt=opt
            )
            generator_b = networks.GlobalGenerator_DCDCv2(
                3, 3, 64, 4, 3, networks.get_norm_layer("instance"), opt=opt
            )
            generator_a.load_state_dict(
                checkpoint(manifest, weights + "VAE_A_quality/latest_net_G.pth"),
                strict=True,
                assign=True,
            )
            b_name = "VAE_B_scratch" if args.child == "scratch" else "VAE_B_quality"
            generator_b.load_state_dict(
                checkpoint(manifest, weights + b_name + "/latest_net_G.pth"),
                strict=True,
                assign=True,
            )
            if args.child == "scratch":
                cls = importlib.import_module(
                    prefix + ".models.NonLocal_feature_mapping_model"
                ).Mapping_Model_with_mask
            else:
                cls = importlib.import_module(
                    prefix + ".models.mapping_model"
                ).Mapping_Model
            mapping = cls(64, 512, n_blocks=6, opt=opt)
            mapping.load_state_dict(
                checkpoint(
                    manifest,
                    weights + f"mapping_{args.child}/latest_net_mapping_net.pth",
                ),
                strict=True,
                assign=True,
            )
            for model in (generator_a, generator_b, mapping):
                model.eval()
            tensor = to_tensor(image)
            if args.child == "scratch":
                with Image.open(args.case / "mask.png") as mask_image:
                    # Match upstream hole synthesis, default mask_dilation=0.
                    array = np.array(mask_image)
                array = np.pad(
                    array,
                    ((0, image.height - size[1]), (0, image.width - size[0])),
                    mode="reflect",
                )
                mask = torch.from_numpy((array > 0).astype(np.float32))[None, None]
                tensor = tensor * (1 - mask) + mask
            features = generator_a(tensor, flow="enc")
            mapped = (
                mapping(features, mask)
                if args.child == "scratch"
                else mapping(features)
            )
            generated = generator_b(mapped, flow="dec")
            if not torch.isfinite(generated).all():
                raise ValueError("Non-finite model output")
            # Standard fixed [-1,1] conversion; no per-image min/max contrast boost.
            values = (
                ((generated[0].permute(1, 2, 0).clamp(-1, 1) + 1) * 127.5)
                .round()
                .to(torch.uint8)
                .numpy()
            )
            result_image = Image.fromarray(values[: size[1], : size[0]])
            result_image.save(args.case / f"{args.child}.png")
            with Image.open(source) as baseline:
                change = np.abs(
                    np.asarray(result_image).astype(np.float32)
                    - np.asarray(baseline).astype(np.float32)
                )
            details = {
                "mean_absolute_change_255": float(change.mean()),
                "changed_fraction_over_16": float(np.mean(np.max(change, axis=2) > 16)),
            }
    print(
        json.dumps(
            {
                "ok": True,
                "mode": args.child,
                "seconds": round(time.perf_counter() - start, 3),
                "memory_mib": process_memory_mib(),
                **details,
            }
        ),
        flush=True,
    )


def bounded_worker(case, mode, timeout):
    physical = worker_budget_mib()
    commit = worker_commit_budget_mib()
    record = {"mode": mode, "physical_budget_mib": physical, "commit_limit_mib": commit}
    if physical < MIN_PHYSICAL_MIB[mode]:
        return {
            **record,
            "ok": False,
            "error": "RAM_ADMISSION",
            "required_mib": MIN_PHYSICAL_MIB[mode],
        }
    containment = WorkerJob(commit)
    start = time.perf_counter()
    try:
        with subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--child",
                mode,
                "--case",
                str(case),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        ) as process:
            try:
                containment.assign(process.pid)
                output, error = process.communicate("GO\n", timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                output, error = process.communicate()
                return {
                    **record,
                    "ok": False,
                    "error": "TIMEOUT",
                    "wall_seconds": round(time.perf_counter() - start, 3),
                }
            except BaseException:
                process.kill()
                process.communicate()
                raise
            record.update(
                exit_code=process.returncode,
                wall_seconds=round(time.perf_counter() - start, 3),
            )
            if process.returncode:
                return {
                    **record,
                    "ok": False,
                    "error": error[-4000:],
                    "stdout": output[-1000:],
                }
            details = json.loads(output.strip().splitlines()[-1])
            target = case / ("mask.png" if mode == "detect" else f"{mode}.png")
            if not details.get("ok") or not target.exists():
                raise ValueError("Worker did not produce a successful result")
            from PIL import Image

            with (
                Image.open(target) as produced,
                Image.open(case / "baseline.png") as base,
            ):
                produced.load()
                if produced.size != base.size or produced.mode != (
                    "L" if mode == "detect" else "RGB"
                ):
                    raise ValueError("Worker output shape/mode mismatch")
            return {**record, **details, "output_sha256": sha256(target)}
    finally:
        containment.close()


def gallery(run, report):
    """Offline review sheet with escaped names and relative, private image URLs."""
    rows = []
    for case in report["cases"]:
        cells = []
        for name in ("baseline", "quality", "mask", "scratch"):
            path = run / case["id"] / f"{name}.png"
            cells.append(
                f'<td><img src="{case["id"]}/{name}.png" alt="{name}"></td>'
                if path.exists()
                else "<td>Unavailable</td>"
            )
        rows.append(f"<tr><th>{html.escape(case['source'])}</th>{''.join(cells)}</tr>")
    page = (
        '<!doctype html><meta charset="utf-8"><title>Microsoft restoration review</title>'
        "<style>body{font:16px system-ui;background:#202124;color:#eee;padding:20px}table{border-collapse:collapse}th,td{padding:10px;border:1px solid #666}img{max-width:256px;max-height:256px}th{max-width:240px;overflow-wrap:anywhere}</style>"
        "<h1>Microsoft restoration: development preview comparison</h1>"
        "<p>256-pixel long edge, CPU, no face enhancement. Judge text, faces, texture and false scratch masks. "
        "Pixel-change measurements do not measure quality. These are experimental previews, not full-resolution restored photos.</p>"
        "<table><tr><th>Source</th><th>Normalized baseline</th><th>Quality repair</th><th>Predicted scratches</th><th>Scratch + quality repair</th></tr>"
        + "".join(rows)
        + '</table><p>Detailed timings, memory, errors and hashes: <a href="report.json">report.json</a></p>'
    )
    (run / "review.html").write_text(page, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "test_photos")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--child", choices=MODES, help=argparse.SUPPRESS)
    parser.add_argument("--case", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        run_child(args)
        return 0
    if not 1 <= args.limit <= 100 or not 1 <= args.timeout <= 600:
        parser.error("Limit must be 1..100, timeout 1..600 seconds")
    input_root = args.input.absolute()
    check_plain_path(input_root)
    if (
        not input_root.is_dir()
        or input_root == ASSETS
        or ASSETS.is_relative_to(input_root)
    ):
        parser.error("Choose a photo directory separate from benchmark assets")
    manifest = load_manifest()
    # Validate all assets before snapshotting private photos.
    for name in REQUIRED_WEIGHTS:
        if name not in manifest["weights"]:
            raise ValueError(
                f"Missing checkpoint; run explicit asset preparation: {name}"
            )
        entry = manifest["weights"][name]
        check_plain_path(ASSETS / "weights" / entry["file"])
        if sha256(ASSETS / "weights" / entry["file"]) != entry["sha256"]:
            raise ValueError("Checkpoint differs from acquired digest")
    run = ASSETS / "runs" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    run.mkdir(parents=True)
    report = {
        "source_revision": REVISION,
        "asset_manifest": manifest,
        "compatibility_import_edits": PATCHES,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cpu_threads": 2,
        "view_long_edge": 256,
        "padding": "reflect to next multiple of 16; cropped after inference",
        "output_conversion": "fixed [-1,1] to [0,255]; upstream per-image contrast normalization omitted",
        "detector": "same padded preview, sigmoid >=0.4; no short-edge 256 enlargement",
        "face_processing": False,
        "colorization": False,
        "initial_memory_mib": memory_mib(),
        "cases": [],
    }
    paths = sorted(
        path
        for path in input_root.rglob("*")
        if path.is_file()
        and path.suffix.lower()
        in (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp")
    )[: args.limit]
    print("RUN", run, flush=True)
    for index, path in enumerate(paths):
        case = {
            "id": f"{index:03d}",
            "source": path.relative_to(input_root).as_posix(),
            "results": [],
        }
        report["cases"].append(case)
        try:
            with source_reader(input_root, path) as stream:
                before = signature(os.fstat(stream.fileno()))
                data = stream.read(40_000_001)
                if (
                    len(data) > 40_000_000
                    or signature(os.fstat(stream.fileno())) != before
                ):
                    raise ValueError(
                        "Input exceeds benchmark limit or changed during snapshot"
                    )
            case["source_sha256"] = hashlib.sha256(data).hexdigest()
            preview, dimensions, profile = make_preview(data)
            del data
            folder = run / case["id"]
            folder.mkdir()
            preview.save(folder / "baseline.png", icc_profile=profile)
            case.update(original_size=dimensions, preview_size=preview.size)
            preview.close()
            for mode in MODES:
                if mode == "scratch" and not any(
                    result["mode"] == "detect" and result["ok"]
                    for result in case["results"]
                ):
                    result = {
                        "mode": mode,
                        "ok": False,
                        "error": "DETECTOR_UNAVAILABLE",
                    }
                else:
                    result = bounded_worker(folder, mode, args.timeout)
                case["results"].append(result)
                print(
                    case["id"],
                    mode,
                    "OK" if result["ok"] else "FAILED",
                    result.get("wall_seconds", result.get("error")),
                    flush=True,
                )
            with source_reader(input_root, path) as stream:
                case["original_unchanged"] = (
                    hashlib.file_digest(stream, "sha256").hexdigest()
                    == case["source_sha256"]
                )
            if not case["original_unchanged"]:
                raise ValueError("Original changed during benchmark")
        except Exception as exc:  # noqa: BLE001 -- keep per-photo failures in report
            case["error"] = str(exc)
            print(case["id"], type(exc).__name__, str(exc), flush=True)
        (run / "report.json").write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8"
        )
        gallery(run, report)
    return (
        0
        if report["cases"]
        and all(
            case.get("original_unchanged")
            and all(result["ok"] for result in case["results"])
            for case in report["cases"]
        )
        else 2
    )


if __name__ == "__main__":
    raise SystemExit(main())
