"""Developer-only refresh of pinned SwinIR source and licenses, never weights."""

import base64
import hashlib
import json
from pathlib import Path
from urllib.request import urlopen

REVISION = "6545850fbf8df298df73d81f3e8cba638787c8bd"
ROOT = Path(__file__).resolve().parents[1] / "src/revive/_vendor/swinir"


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    manifest = {"revision": REVISION, "files": {}}
    for name, relative in {
        "network_swinir.py": "models/network_swinir.py",
        "LICENSE": "LICENSE",
    }.items():
        url = f"https://raw.githubusercontent.com/JingyunLiang/SwinIR/{REVISION}/{relative}"
        with urlopen(url, timeout=45) as response:
            content = response.read(200_001)
        if len(content) > 200_000:
            raise ValueError("Unexpected source size")
        upstream_digest = hashlib.sha256(content).hexdigest()
        if name.endswith(".py"):
            before = (
                b"from timm.models.layers import DropPath, to_2tuple, trunc_normal_"
            )
            if content.count(before) != 1:
                raise ValueError("Expected compatibility import changed")
            content = content.replace(
                before, b"from .compat import DropPath, to_2tuple, trunc_normal_"
            )
        (ROOT / name).write_bytes(content)
        manifest["files"][name] = {
            "source": url,
            "upstream_sha256": upstream_digest,
            "packaged_sha256": hashlib.sha256(content).hexdigest(),
        }
    # SwinIR acknowledges code derived from these MIT projects. Retain their
    # notices as requested by its README, recording immutable Git blob IDs.
    for name, repo in {
        "LICENSE-Swin-Transformer": "microsoft/Swin-Transformer",
        "LICENSE-KAIR": "cszn/KAIR",
    }.items():
        with urlopen(
            f"https://api.github.com/repos/{repo}/contents/LICENSE", timeout=45
        ) as response:
            entry = json.loads(response.read(50_000))
        content = base64.b64decode(entry["content"], validate=False)
        (ROOT / name).write_bytes(content)
        manifest["files"][name] = {
            "source": f"https://api.github.com/repos/{repo}/git/blobs/{entry['sha']}",
            "blob_sha": entry["sha"],
            "packaged_sha256": hashlib.sha256(content).hexdigest(),
        }
    (ROOT / "upstream.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    print(
        "Pinned SwinIR source and Apache-2.0 license acquired; no weights downloaded."
    )


if __name__ == "__main__":
    main()
