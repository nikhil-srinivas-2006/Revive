"""Offline original/previous/new comparison; never modifies source photographs."""

import argparse
import hashlib
import json
from pathlib import Path

from revive.config import load_config
from revive.paths import source_reader, within
from revive.state import State


def jobs(config):
    database = config.app_data / "state.sqlite3"
    if not database.exists():
        return {}
    state = State.readonly(database)
    try:
        return {row["relative_path"]: row for row in state.export()}
    finally:
        state.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--previous-config", type=Path)
    args = parser.parse_args()
    config = load_config(args.config)
    previous = load_config(args.previous_config) if args.previous_config else None
    old_jobs = jobs(previous) if previous else {}
    records = []
    for name, job in jobs(config).items():
        source = within(config.input, config.input / name)
        with source_reader(config.input, source) as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if job["source_hash"] and digest != job["source_hash"]:
            raise ValueError("Original changed since processing; comparison refused")
        previous_url = None
        after_url = None
        old = old_jobs.get(name)
        if old and old["state"] == "completed" and old["source_hash"] == digest:
            target = within(previous.output, previous.output / old["output_path"])
            with source_reader(previous.output, target) as stream:
                if (
                    hashlib.file_digest(stream, "sha256").hexdigest()
                    != old["output_hash"]
                ):
                    raise ValueError("Previous output no longer matches its checksum")
            previous_url = target.as_uri()
        metadata = job["metadata"] or {}
        if job["state"] == "completed":
            target = within(config.output, config.output / job["output_path"])
            with source_reader(config.output, target) as stream:
                if (
                    hashlib.file_digest(stream, "sha256").hexdigest()
                    != job["output_hash"]
                ):
                    raise ValueError("Published output no longer matches its checksum")
            after_url = target.as_uri()
        stages = metadata.get("stages", [])
        record = {
            "id": job["id"],
            "name": name,
            "before_url": source.as_uri(),
            "after_url": after_url,
            "previous_url": previous_url,
            "state": job["state"],
            "error_code": job["error_code"],
            "original_sha256": digest,
            "original_unchanged": job["source_hash"] is None
            or digest == job["source_hash"],
            "stages": stages,
            "worker_measurements": metadata.get("worker_measurements"),
            "colorized": metadata.get("colors_inferred", False),
            "upscaled": metadata.get("details_inferred", False),
        }
        records.append(record)
    review = within(config.app_data, config.app_data / "review")
    review.mkdir(exist_ok=True)
    (review / "report.json").write_text(
        json.dumps(records, indent=2) + "\n", encoding="utf-8"
    )
    template = (
        Path(__file__).with_name("review_viewer.html").read_text(encoding="utf-8")
    )
    # Filenames are user-controlled. Keep embedded JSON from closing its script
    # element; the viewer inserts names with textContent, never HTML markup.
    payload = (
        json.dumps(records)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )
    (review / "index.html").write_text(
        template.replace("__PHOTO_DATA__", payload), encoding="utf-8"
    )
    print(review / "index.html")


if __name__ == "__main__":
    main()
