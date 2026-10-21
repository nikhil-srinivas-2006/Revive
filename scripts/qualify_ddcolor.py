"""Bounded, opt-in real-checkpoint smoke test on a synthetic grayscale ramp.

No downloads or user-photo access. The parent caps the child at the configured
worker budget; this probe never increases that budget to make inference pass.
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from revive.config import load_config
from revive.errors import ReviveError
from revive.processes import WorkerJob


def child(config):
    from PIL import Image

    from revive.ddcolor import DDColorAdapter
    from revive.resources import process_memory_mib

    image = Image.new("RGB", (128, 96))
    image.putdata([((x * 2) % 256,) * 3 for y in range(96) for x in range(128)])
    start = time.perf_counter()
    adapter = DDColorAdapter(
        {**config.worker_settings(), "qualification_metrics": True}
    )
    try:
        output, details = adapter.colorize(image, 0.85)
    except ReviveError as exc:
        exc.measurements = adapter.measurements
        raise
    output.close()
    print(
        json.dumps(
            {
                "ok": True,
                "seconds": round(time.perf_counter() - start, 3),
                "memory_mib": process_memory_mib(),
                "result": details,
                "measurements": adapter.measurements,
            }
        )
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Include exception causes for this synthetic developer probe only.",
    )
    args = parser.parse_args()
    config = load_config(args.config)
    if args.child:
        try:
            child(config)
        except ReviveError as exc:
            result = {"ok": False, "code": exc.code, "message": str(exc)}
            if args.debug:
                result["cause"] = str(exc.__cause__)[:6000]
                result["measurements"] = getattr(exc, "measurements", [])
            print(json.dumps(result))
            return 2
        return 0
    budget = config.worker_settings()["worker_memory_mib"]
    commit = config.worker_settings()["worker_commit_mib"]
    containment = WorkerJob(commit)
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        with containment.popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--config",
                str(args.config.resolve()),
                "--child",
                *(["--debug"] if args.debug else []),
            ],
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=flags,
        ) as process:
            try:
                out, err = process.communicate(timeout=120)
            except BaseException:
                containment.close()
                process.kill()
                process.communicate()
                raise
            print(
                json.dumps(
                    {
                        "worker_budget_mib": budget,
                        "worker_commit_mib": commit,
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
