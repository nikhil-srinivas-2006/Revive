"""Developer-only refresh of the audited DDColor architecture (never weights).

This script downloads only pinned source text and licenses. Review its diff before
updating the packaged adapter. Normal operation never invokes this script.
"""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.request import urlopen

REVISION = "2adb63f2656ac41cbdf7b894cddd94121a3faf13"
ROOT = Path(__file__).resolve().parents[1] / "src/revive/_vendor/ddcolor"
FILES = {
    "model.py": "ddcolor/model.py",
    **{
        name + ".py": "basicsr/archs/ddcolor_arch_utils/" + name + ".py"
        for name in ("unet", "convnext", "transformer_utils", "position_encoding")
    },
    "LICENSE": "LICENSE",
}


def fetch(item):
    name, path = item
    url = f"https://raw.githubusercontent.com/piddnad/DDColor/{REVISION}/{path}"
    with urlopen(url, timeout=45) as response:
        content = response.read(2 * 1024**2)
    if len(content) >= 2 * 1024**2:
        raise ValueError("Unexpected source size")
    digest = hashlib.sha256(content).hexdigest()
    if name == "model.py":
        content = content.replace(b"from basicsr.archs.ddcolor_arch_utils.", b"from .")
    return (
        name,
        content,
        {
            "source": url,
            "upstream_sha256": digest,
            "packaged_sha256": hashlib.sha256(content).hexdigest(),
        },
    )


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    manifest = {"revision": REVISION, "files": {}}
    with ThreadPoolExecutor(max_workers=6) as pool:
        for name, content, entry in pool.map(fetch, FILES.items()):
            (ROOT / name).write_bytes(content)
            manifest["files"][name] = entry
    # ConvNeXt retains its original Meta copyright header. Carry its MIT license
    # alongside DDColor's Apache-2.0 license, with source provenance.
    url = "https://api.github.com/repos/facebookresearch/ConvNeXt/contents/LICENSE"
    with urlopen(url, timeout=45) as response:
        license_info = json.load(response)
    import base64

    content = base64.b64decode(license_info["content"])
    (ROOT / "LICENSE-ConvNeXt").write_bytes(content)
    manifest["files"]["LICENSE-ConvNeXt"] = {
        "source": license_info["html_url"],
        "blob_sha": license_info["sha"],
        "packaged_sha256": hashlib.sha256(content).hexdigest(),
    }
    (ROOT / "upstream.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    print("Vendored architecture and licenses. No model weights downloaded.")


if __name__ == "__main__":
    main()
