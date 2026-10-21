"""Explicit acquisition of pinned Microsoft research assets, outside the product.

The release has no published digest. SHA-256 records are trust-on-first-download,
not proof against a compromised publisher. Never executed by the monitor/worker.
"""

import argparse
import hashlib
import io
import json
import shutil
import urllib.request
import zipfile
from pathlib import Path

from revive.paths import check_plain_path

REVISION = "33875eccf4ebcd3665cf38cc56f3a0ce563d3a9c"
ARCHIVE_BYTES = 2036400762
ARCHIVE_URL = (
    "https://github.com/microsoft/Bringing-Old-Photos-Back-to-Life/"
    "releases/download/v1.0/global_checkpoints.zip"
)
ROOT = Path(__file__).resolve().parents[1] / ".runtime" / "microsoft_benchmark"
INFERENCE_MEMBERS = (
    "checkpoints/restoration/VAE_A_quality/latest_net_G.pth",
    "checkpoints/restoration/VAE_B_quality/latest_net_G.pth",
    "checkpoints/restoration/VAE_B_scratch/latest_net_G.pth",
    "checkpoints/restoration/mapping_quality/latest_net_mapping_net.pth",
    "checkpoints/restoration/mapping_scratch/latest_net_mapping_net.pth",
    "checkpoints/detection/FT_Epoch_latest.pt",
)
SOURCES = (
    "LICENSE",
    "Global/models/__init__.py",
    "Global/models/networks.py",
    "Global/models/base_model.py",
    "Global/models/mapping_model.py",
    "Global/models/NonLocal_feature_mapping_model.py",
    "Global/options/__init__.py",
    "Global/options/base_options.py",
    "Global/options/test_options.py",
    "Global/util/__init__.py",
    "Global/util/image_pool.py",
    "Global/detection_models/__init__.py",
    "Global/detection_models/networks.py",
    "Global/detection_models/antialiasing.py",
    "Global/detection.py",
    "Global/test.py",
)


def fetch(url, maximum):
    with urllib.request.urlopen(url, timeout=90) as response:
        if not response.url.startswith("https://"):
            raise ValueError("HTTPS required")
        data = response.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError("Download exceeds bound")
    return data


class RemoteZip(io.RawIOBase):
    """Seekable, bounded HTTPS ranges; reject servers ignoring Range."""

    def __init__(self):
        self.position = 0
        self.block_start = -1
        self.block = b""

    def seekable(self):
        return True

    def readable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=0):
        self.position = (0, self.position, ARCHIVE_BYTES)[whence] + offset
        if not 0 <= self.position <= ARCHIVE_BYTES:
            raise ValueError("Invalid seek")
        return self.position

    def read(self, size=-1):
        if size < 0:
            size = ARCHIVE_BYTES - self.position
        size = min(size, ARCHIVE_BYTES - self.position)
        if size == 0:
            return b""
        if size > 16 * 1024**2:
            raise ValueError("Single range read exceeds the 16 MiB bound")
        if not (
            self.block_start <= self.position
            and self.position + size <= self.block_start + len(self.block)
        ):
            start = self.position
            end = min(ARCHIVE_BYTES - 1, start + max(size, 8 * 1024**2) - 1)
            request = urllib.request.Request(
                ARCHIVE_URL,
                headers={
                    "Range": f"bytes={start}-{end}",
                    "Accept-Encoding": "identity",
                },
            )
            with urllib.request.urlopen(request, timeout=120) as response:
                expected = f"bytes {start}-{end}/{ARCHIVE_BYTES}"
                if (
                    response.status != 206
                    or response.headers.get("Content-Range") != expected
                ):
                    raise ValueError("Release server did not honor the bounded range")
                self.block = response.read(end - start + 2)
                if len(self.block) != end - start + 1:
                    raise ValueError("Incomplete range")
                self.block_start = start
        offset = self.position - self.block_start
        data = self.block[offset : offset + size]
        self.position += len(data)
        return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("sources", "inspect", "weights"))
    parser.add_argument("--member", action="append", default=[])
    parser.add_argument(
        "--inference", action="store_true", help="Select the six inference checkpoints"
    )
    args = parser.parse_args()
    if args.inference:
        if args.action != "weights" or args.member:
            parser.error("Use --inference only with weights, without --member")
        args.member = list(INFERENCE_MEMBERS)
    check_plain_path(ROOT)
    ROOT.mkdir(parents=True, exist_ok=True)
    manifest_path = ROOT / "acquisition.json"
    check_plain_path(manifest_path)
    manifest = (
        json.loads(manifest_path.read_text())
        if manifest_path.exists()
        else {
            "revision": REVISION,
            "archive_url": ARCHIVE_URL,
            "upstream_digest": None,
            "integrity": "TLS + ZIP CRC; SHA256 pinned on first acquisition",
            "sources": {},
            "weights": {},
        }
    )
    if args.action == "sources":
        for name in SOURCES:
            target = ROOT / "source" / name
            check_plain_path(target)
            if target.exists():
                data = target.read_bytes()
                if hashlib.sha256(data).hexdigest() != manifest["sources"].get(name):
                    raise ValueError(f"Existing source differs: {name}")
                continue
            data = fetch(
                f"https://raw.githubusercontent.com/microsoft/Bringing-Old-Photos-Back-to-Life/{REVISION}/{name}",
                200_000,
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            manifest["sources"][name] = hashlib.sha256(data).hexdigest()
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
            print(name, len(data), flush=True)
        return
    with zipfile.ZipFile(RemoteZip()) as archive:
        if args.action == "inspect":
            print(
                json.dumps(
                    [
                        {
                            "name": item.filename,
                            "bytes": item.file_size,
                            "compressed_bytes": item.compress_size,
                            "crc": item.CRC,
                        }
                        for item in archive.infolist()
                        if not item.is_dir()
                    ],
                    indent=2,
                )
            )
            return
        if not args.member:
            parser.error("Select exact --member names after inspecting the archive")
        for name in args.member:
            if name not in INFERENCE_MEMBERS:
                parser.error(
                    "Only the six reviewed inference checkpoint names are allowed"
                )
            item = archive.getinfo(name)
            if item.is_dir() or item.file_size > 900_000_000:
                raise ValueError("Unexpected checkpoint")
            # Flatten only the explicitly selected member, never extractall.
            target = ROOT / "weights" / (name.replace("/", "__"))
            check_plain_path(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                with target.open("rb") as existing:
                    digest = hashlib.file_digest(existing, "sha256").hexdigest()
                if digest != manifest["weights"].get(name, {}).get("sha256"):
                    raise ValueError(f"Existing checkpoint differs: {name}")
                print("Verified", name, flush=True)
                continue
            temporary = target.with_suffix(".partial")
            check_plain_path(temporary)
            if shutil.disk_usage(target.parent).free < item.file_size + 64 * 1024**2:
                raise ValueError(
                    "Insufficient space for checkpoint plus 64 MiB reserve"
                )
            digest = hashlib.sha256()
            count = 0
            try:
                with archive.open(item) as source, temporary.open("xb") as output:
                    while data := source.read(1024 * 1024):
                        count += len(data)
                        if count > item.file_size:
                            raise ValueError(
                                "Expanded checkpoint exceeds declared size"
                            )
                        output.write(data)
                        digest.update(data)
                if count != item.file_size:
                    raise ValueError("Incomplete checkpoint")
                temporary.rename(target)
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
            manifest["weights"][name] = {
                "file": target.name,
                "bytes": count,
                "sha256": digest.hexdigest(),
                "zip_crc": item.CRC,
            }
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
            print("Acquired", name, count, flush=True)


if __name__ == "__main__":
    main()
