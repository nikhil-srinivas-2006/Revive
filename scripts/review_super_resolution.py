"""Build an offline same-size Lanczos/SwinIR review from a qualification manifest."""

import argparse
import hashlib
import html
import json
import os
from pathlib import Path
from urllib.parse import quote

from PIL import Image, ImageOps

from revive.config import load_config
from revive.paths import source_reader, within
from revive.pipeline import to_srgb
from revive.state import State


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    config = load_config(args.config)
    if (
        config.colorization_enabled
        or config.denoise_strength
        or config.contrast_strength
    ):
        parser.error(
            "This comparison isolates SR; use a configuration without colorization/cleanup"
        )
    review = within(config.app_data, config.app_data / "review")
    review.mkdir(exist_ok=True)
    state = State.readonly(config.app_data / "state.sqlite3")
    try:
        latest = {
            row["relative_path"]: row
            for row in state.export()
            if row["pipeline"] == config.fingerprint
        }
    finally:
        state.close()
    rows, records = [], []
    for name, job in sorted(latest.items()):
        label = html.escape(name)
        record = {
            key: job[key]
            for key in (
                "id",
                "relative_path",
                "state",
                "error_code",
                "source_hash",
                "output_path",
                "output_hash",
            )
        }
        records.append(record)
        metadata = job.get("metadata") or {}
        if job["state"] != "completed" or not metadata.get("details_inferred"):
            rows.append(
                f'<tr><th>{label}</th><td colspan="2">{html.escape(job["state"])}: {html.escape(job["error_code"] or "")}</td></tr>'
            )
            continue
        output = within(config.output, config.output / job["output_path"])
        with source_reader(config.output, output) as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != job["output_hash"]:
                raise ValueError("Published derivative differs from manifest")
        source = within(config.input, config.input / name)
        with source_reader(config.input, source) as stream:
            record["original_unchanged"] = (
                hashlib.file_digest(stream, "sha256").hexdigest() == job["source_hash"]
            )
            if not record["original_unchanged"]:
                raise ValueError("Source changed since the qualified snapshot")
            stream.seek(0)
            with Image.open(stream) as decoded:
                decoded.load()
                oriented = ImageOps.exif_transpose(decoded)
                baseline, icc = to_srgb(oriented, [], [])
                oriented.close()
        filename = f"baseline_{job['id']}_{job['source_hash'][:12]}.png"
        target = within(config.app_data, review / filename)
        enlarged = baseline.resize(tuple(metadata["size"]), Image.Resampling.LANCZOS)
        baseline.close()
        try:
            if not target.exists():
                with target.open("xb") as stream:
                    enlarged.save(stream, format="PNG", icc_profile=icc)
        finally:
            enlarged.close()
        record["measurements"] = metadata.get("worker_measurements")
        record["size"] = metadata["size"]
        sr_url = quote(os.path.relpath(output, review).replace(os.sep, "/"), safe="/")
        base_url = quote(filename)
        rows.append(
            f'<tr><th>{label}<br>{metadata["size"][0]} × {metadata["size"][1]}</th><td><a href="{base_url}"><img src="{base_url}" alt="Lanczos interpolation"></a></td><td><a href="{sr_url}"><img src="{sr_url}" alt="SwinIR AI enlargement"></a></td></tr>'
        )
    page = (
        '<!doctype html><meta charset="utf-8"><title>Revive SR qualification</title>'
        "<style>body{background:#202124;color:#eee;font:16px system-ui;padding:20px}table{border-collapse:collapse}th,td{padding:12px;border:1px solid #666}th{max-width:240px;overflow-wrap:anywhere}img{max-width:420px;max-height:500px}a{color:#9cf}</style>"
        "<h1>Same-size interpolation versus AI super-resolution</h1>"
        "<p>Click an image to inspect full-size pixels. Judge texture, text, faces, color, noise amplification and seams. AI-generated details are inferred.</p>"
        "<table><tr><th>Source and output size</th><th>Lanczos baseline</th><th>SwinIR lightweight x2</th></tr>"
        + "".join(rows)
        + '</table><p><a href="report.json">Timings, memory, hashes and incomplete cases</a></p>'
    )
    (review / "index.html").write_text(page, encoding="utf-8")
    (review / "report.json").write_text(
        json.dumps(records, indent=2) + "\n", encoding="utf-8"
    )
    print(review / "index.html")


if __name__ == "__main__":
    main()
