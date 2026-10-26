"""One configuration routes photos through either, both, or neither AI stage."""

import hashlib
import json

import pytest
from PIL import Image

from revive.ddcolor import DDColorAdapter
from revive.swinir import SwinIRAdapter
from revive.worker import process


@pytest.mark.parametrize(
    "color,expected_colorization",
    [("gray", True), ((0, 180, 60), False), ((140, 110, 80), False)],
    ids=["grayscale", "color", "sepia"],
)
@pytest.mark.parametrize("size", [(32, 24), (128, 96)], ids=["small", "at-target"])
def test_one_profile_selects_stages_from_original(
    tmp_path, monkeypatch, color, expected_colorization, size
):
    calls = []

    def colorize(self, image, strength):
        calls.append("colorize")
        # Stand in for inference; exercise real full-resolution eligibility.
        return Image.new("RGB", image.size, (50, 100, 150)), {"model": "test"}

    def upscale(self, image, target):
        calls.append("upscale")
        if expected_colorization:
            assert image.getpixel((0, 0)) == (50, 100, 150)
        return image.resize(target), {"model": "test"}

    monkeypatch.setattr(DDColorAdapter, "colorize", colorize)
    monkeypatch.setattr(SwinIRAdapter, "upscale", upscale)
    source, output = tmp_path / "original.png", tmp_path / "result.png"
    with Image.new("RGB", size, color) as image:
        image.save(source)
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    result = process(
        {
            "snapshot": str(source),
            "output": str(output),
            "settings": {
                "adapter": "normalize_png",
                "max_input_megapixels": 40,
                "worker_memory_mib": 4096,
                "worker_commit_mib": 4096,
                "colorization_enabled": True,
                "sr_enabled": True,
                "sr_target_long_edge": 128,
            },
        }
    )
    expected_upscale = max(size) < 128
    assert calls == (["colorize"] if expected_colorization else []) + (
        ["upscale"] if expected_upscale else []
    )
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_hash
    assert result["metadata"]["colors_inferred"] is expected_colorization
    assert result["metadata"]["details_inferred"] is expected_upscale
    with Image.open(output) as image:
        assert image.size == (tuple(x * 2 for x in size) if expected_upscale else size)
        provenance = json.loads(image.text["revive:provenance"])
        assert provenance["colorization"]["applied"] is expected_colorization
        assert provenance["super_resolution"]["applied"] is expected_upscale
