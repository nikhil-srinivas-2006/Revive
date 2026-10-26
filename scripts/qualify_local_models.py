"""Bounded real-model trial on the smallest photos; uses production containment."""

import argparse
import html
import json
import time
import uuid
from pathlib import Path

from PIL import Image

from revive.config import load_config
from revive.engine import EXTENSIONS, Engine, file_hash
from revive.errors import ReviveError
from revive.paths import InstanceLock, signature, within


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=3)
    args = parser.parse_args()
    if not 1 <= args.limit <= 10:
        parser.error("limit must be 1..10")
    config = load_config(args.config)
    config.app_data.mkdir(parents=True, exist_ok=True)
    report = []
    with InstanceLock(within(config.app_data, config.app_data / "monitor.lock")):
        engine = Engine(config)
        try:
            candidates = []
            paths = (
                config.input.rglob("*") if config.recursive else config.input.iterdir()
            )
            for path in paths:
                if path.suffix.lower() in EXTENSIONS and path.is_file():
                    within(config.input, path)
                    with Image.open(path) as image:
                        candidates.append((image.width * image.height, path))
            directory = within(
                config.app_data, config.app_data / "qualification" / uuid.uuid4().hex
            )
            directory.mkdir(parents=True)
            for _, path in sorted(candidates)[: args.limit]:
                relative = path.relative_to(config.input).as_posix()
                row = {"relative_path": relative, "signature": signature(path.stat())}
                snapshot = directory / "source"
                output = directory / f"result-{len(report) + 1}.png"
                started = time.perf_counter()
                record = {"relative_path": relative}
                try:
                    digest = engine._snapshot(row, snapshot)
                    record["original_sha256"] = digest
                    response = engine._worker(snapshot, output)
                    if response["hash"] != file_hash(output):
                        raise ReviveError(
                            "OUTPUT_INVALID", "Probe output checksum mismatch."
                        )
                    record.update(
                        ok=True, output=str(output), metadata=response["metadata"]
                    )
                except ReviveError as error:
                    record.update(ok=False, error_code=error.code, message=str(error))
                finally:
                    snapshot.unlink(missing_ok=True)
                record["original_unchanged"] = record.get(
                    "original_sha256"
                ) == file_hash(path)
                record["seconds"] = round(time.perf_counter() - started, 3)
                report.append(record)
                print(json.dumps(record), flush=True)
        finally:
            engine.close()
    (directory / "report.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    sections = []
    for item in report:
        source = within(config.input, config.input / item["relative_path"])
        images = f'<figure><img src="{html.escape(source.as_uri(), quote=True)}"><figcaption>Original</figcaption></figure>'
        if item["ok"]:
            images += f'<figure><img src="{html.escape(Path(item["output"]).as_uri(), quote=True)}"><figcaption>New local pipeline</figcaption></figure>'
        sections.append(
            f"<h2>{html.escape(item['relative_path'])}</h2><div>{images}</div><pre>{html.escape(json.dumps({k: v for k, v in item.items() if k != 'metadata'}))}</pre>"
        )
    (directory / "index.html").write_text(
        '<!doctype html><meta charset="utf-8"><title>Local model trial</title>'
        "<style>body{background:#202020;color:#eee;font:16px system-ui;margin:24px}div{display:flex;gap:20px}figure{flex:1;margin:0}img{width:100%;max-height:600px;object-fit:contain}pre{white-space:pre-wrap}</style>"
        "<h1>Bounded local-model qualification</h1><p>Compare faces and colors at equal displayed size. This is not a claim of historically accurate reconstruction.</p>"
        + "".join(sections),
        encoding="utf-8",
    )
    print("Review:", directory / "index.html")
    return 0 if all(item["ok"] and item["original_unchanged"] for item in report) else 2


if __name__ == "__main__":
    raise SystemExit(main())
