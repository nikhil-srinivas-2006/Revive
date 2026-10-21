"""Bounded, opt-in real SwinIR seam/memory probe using synthetic pixels only."""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from revive.config import load_config
from revive.processes import WorkerJob
from revive.super_resolution import requirements

TEST_SIZE = (264, 264)


def child(config):
    if sys.stdin.readline().strip() != "GO":
        raise ValueError("Containment handshake missing")
    import numpy as np
    from PIL import Image

    from revive.resources import process_memory_mib
    from revive.swinir import SwinIRAdapter, tiled_upscale

    y, x = np.mgrid[: TEST_SIZE[1], : TEST_SIZE[0]]
    values = np.stack(
        ((x * 7 + y * 3) % 256, (x * 3 + y * 11) % 256, (x * 13 + y * 5) % 256), axis=2
    ).astype(np.uint8)
    image = Image.fromarray(values)
    start = time.perf_counter()
    adapter = SwinIRAdapter(config.worker_settings())
    model = adapter.load()
    tiled, count = tiled_upscale(
        image, lambda tile: adapter.predict(model, tile), config.sr_tile_size
    )
    tiled_peak = process_memory_mib()
    full = np.rint(np.clip(adapter.predict(model, image), 0, 1) * 255).astype(np.uint8)
    error = np.abs(np.asarray(tiled).astype(np.float32) - full.astype(np.float32))
    # Compare regions next to internal tile boundaries separately from all pixels.
    seam = np.zeros(error.shape[:2], dtype=bool)
    for axis, length in enumerate((image.height, image.width)):
        for boundary in range(
            config.sr_tile_size * 2, length * 2, config.sr_tile_size * 2
        ):
            if axis == 0:
                seam[max(0, boundary - 2) : boundary + 2, :] = True
            else:
                seam[:, max(0, boundary - 2) : boundary + 2] = True
    mean_error = float(error.mean())
    seam_mean = float(error[seam].mean()) if seam.any() else 0.0
    maximum = float(error.max())
    # Initial regression gate in 8-bit code values, not a perceptual/fidelity score.
    ok = mean_error <= 1 and seam_mean <= 2 and maximum <= 16
    print(
        json.dumps(
            {
                "ok": ok,
                "seconds": round(time.perf_counter() - start, 3),
                "memory_mib": process_memory_mib(),
                "tiled_memory_mib": tiled_peak,
                "tiles": count,
                "full_vs_tiled_mean_absolute_255": mean_error,
                "seam_mean_absolute_255": seam_mean,
                "max_absolute_255": maximum,
                "regression_gate": {"mean": 1, "seam_mean": 2, "max": 16},
            }
        ),
        flush=True,
    )
    return 0 if ok else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    config = load_config(args.config)
    if args.child:
        return child(config)
    settings = config.worker_settings()
    _, physical, commit, _ = requirements(TEST_SIZE, settings)
    # The reference full-frame pass intentionally allocates beyond production's
    # bounded tile view. Admit it separately from ordinary tiled inference.
    physical = max(896, physical)
    commit = max(1792, commit)
    if (
        settings["worker_memory_mib"] < physical
        or settings["worker_commit_mib"] < commit
    ):
        print(json.dumps({"ok": False, "error": "RAM_ADMISSION", "settings": settings}))
        return 2
    containment = WorkerJob(settings["worker_commit_mib"])
    try:
        with containment.popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--config",
                str(args.config.resolve()),
                "--child",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        ) as process:
            try:
                out, err = process.communicate("GO\n", timeout=120)
            except BaseException:
                containment.close()
                process.kill()
                process.communicate()
                raise
            print(
                json.dumps(
                    {
                        "physical_budget_mib": settings["worker_memory_mib"],
                        "commit_limit_mib": settings["worker_commit_mib"],
                        "exit_code": process.returncode,
                        "stdout": out.strip(),
                        "stderr": err.strip(),
                    },
                    indent=2,
                )
            )
            return 0 if process.returncode == 0 else 2
    finally:
        containment.close()


if __name__ == "__main__":
    raise SystemExit(main())
