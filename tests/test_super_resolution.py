import hashlib
import io
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from revive import models
from revive.config import load_config
from revive.errors import ReviveError
from revive.super_resolution import requirements, target_size, upscale_if_needed
from revive.worker import process


@pytest.mark.parametrize(
    "size,target,expected",
    [
        ((400, 300), 4096, (800, 600)),
        ((3000, 2000), 4096, (4096, 2731)),
        ((5000, 3000), 4096, (5000, 3000)),
        ((4096, 100), 4096, (4096, 100)),
        ((1, 1), 4096, (2, 2)),
        ((17, 23), 64, (34, 46)),
        ((199, 299), 512, (341, 512)),
    ],
)
def test_geometry_never_shrinks_or_exceeds_two_times(size, target, expected):
    assert target_size(size, target) == expected
    assert all(old <= new <= old * 2 for old, new in zip(size, expected, strict=True))


@pytest.mark.parametrize(
    "changes",
    [
        {"sr_enabled": 1},
        {"sr_tile_size": True},
        {"sr_tile_size": 96},
        {"sr_target_long_edge": 32},
        {"sr_target_long_edge": 8192},
        {"max_output_megapixels": False},
        {"max_output_megapixels": 0},
        {"sr_target_long_edge": float("nan")},
    ],
)
def test_config_rejects_unsafe_or_ambiguous_settings(config, changes):
    with pytest.raises(ReviveError):
        replace(config, **changes).validate()


def test_config_pipeline_requirement_and_fingerprint(config, tmp_path):
    with pytest.raises(ReviveError):
        replace(config, sr_enabled=True).validate()
    current = replace(config, adapter="normalize_png", sr_enabled=True)
    current.validate()
    assert current.fingerprint != replace(current, sr_tile_size=64).fingerprint
    assert current.fingerprint != replace(current, sr_target_long_edge=2048).fingerprint
    assert current.fingerprint != replace(current, sr_enabled=False).fingerprint
    assert current.fingerprint == replace(current, worker_memory_mib=2048).fingerprint
    assert (
        replace(current, sr_enabled=False).fingerprint
        == replace(current, sr_enabled=False, sr_tile_size=128).fingerprint
    )
    path = tmp_path / "config.toml"
    path.write_text(
        "schema_version=1\n[paths]\n"
        + "\n".join(
            f"{name}={json.dumps(str(getattr(config, name)))}"
            for name in ("input", "output", "app_data")
        )
        + '\n[pipeline]\nadapter="normalize_png"\n[super_resolution]\nenabled=true\ntarget_long_edge=2048\ntile_size=128\n[resources]\nmax_output_megapixels=16\n'
    )
    loaded = load_config(path)
    assert (
        loaded.sr_enabled
        and loaded.sr_tile_size == 128
        and loaded.max_output_megapixels == 16
    )


def test_bypasses_do_not_load_model(monkeypatch):
    from revive.swinir import SwinIRAdapter

    def forbidden(*args):
        pytest.fail("Bypassed input must not open a model")

    monkeypatch.setattr(SwinIRAdapter, "load", forbidden)
    for mode, enabled, size in (
        ("RGB", False, (5, 5)),
        ("RGBA", True, (10, 10)),
        ("RGB", True, (128, 100)),
    ):
        image = Image.new(mode, size)
        stages, notes = [], []
        result, info = upscale_if_needed(
            image, {"sr_enabled": enabled, "sr_target_long_edge": 128}, stages, notes
        )
        assert result is image and not info["applied"] and not stages


def test_memory_and_output_limits_before_model_load():
    image = Image.new("RGB", (500, 500))
    with pytest.raises(ReviveError) as failure:
        upscale_if_needed(image, {"sr_enabled": True, "worker_memory_mib": 128}, [], [])
    assert failure.value.code == "RAM_ADMISSION" and failure.value.retryable
    with pytest.raises(ReviveError) as failure:
        requirements((1000, 1000), {"max_output_megapixels": 1})
    assert failure.value.code == "IMAGE_LIMIT"


@pytest.mark.parametrize("size", [(1, 1), (9, 17), (130, 97), (64, 64)])
def test_tiles_cover_odd_edges_without_gaps_or_misalignment(size):
    np = pytest.importorskip("numpy")
    from revive.swinir import tiled_upscale

    y, x = np.mgrid[: size[1], : size[0]]
    array = np.stack(
        ((x * 17) % 256, (y * 23) % 256, ((x + y) * 11) % 256), axis=2
    ).astype(np.uint8)
    image = Image.fromarray(array)

    def predict(tile):
        assert tile.width <= 128 and tile.height <= 128
        assert tile.width % 8 == 0 and tile.height % 8 == 0
        return (
            np.repeat(np.repeat(np.asarray(tile), 2, axis=0), 2, axis=1).astype(
                np.float32
            )
            / 255
        )

    result, count = tiled_upscale(image, predict)
    expected = np.repeat(np.repeat(array, 2, axis=0), 2, axis=1)
    assert np.array_equal(np.asarray(result), expected) and count >= 1


def test_non_finite_tile_fails_instead_of_publishing():
    np = pytest.importorskip("numpy")
    from revive.swinir import tiled_upscale

    def predict(tile):
        return np.full((tile.height * 2, tile.width * 2, 3), np.nan, dtype=np.float32)

    with pytest.raises(ReviveError) as failure:
        tiled_upscale(Image.new("RGB", (16, 16)), predict)
    assert failure.value.code == "OUTPUT_INVALID"


def test_pipeline_sizing_metadata_and_source_integrity(tmp_path, monkeypatch):
    from revive.swinir import SwinIRAdapter

    def fake_upscale(self, image, target):
        return image.resize(target), {
            "model": "test-double",
            "output_size": list(target),
        }

    monkeypatch.setattr(SwinIRAdapter, "upscale", fake_upscale)
    source, output = tmp_path / "source.png", tmp_path / "output.png"
    exif = Image.Exif()
    exif[274] = 6
    exif[34665] = {40962: 100, 40963: 70, 40961: 65535}
    Image.new("RGB", (100, 70), "gray").save(source, exif=exif)
    original = source.read_bytes()
    result = process(
        {
            "snapshot": str(source),
            "output": str(output),
            "settings": {
                "adapter": "normalize_png",
                "max_input_megapixels": 40,
                "worker_memory_mib": 2048,
                "worker_commit_mib": 2048,
                "sr_enabled": True,
                "sr_target_long_edge": 160,
            },
        }
    )
    assert source.read_bytes() == original
    metadata = result["metadata"]
    assert metadata["size"] == [112, 160] and metadata["details_inferred"]
    assert not metadata["colors_inferred"]
    with Image.open(output) as final:
        assert final.size == (112, 160)
        subifd = final.getexif().get_ifd(34665)
        assert (subifd[40962], subifd[40963]) == final.size
        provenance = json.loads(final.text["revive:provenance"])
        assert (
            provenance["super_resolution"]["applied"] and provenance["details_inferred"]
        )


def test_disk_admission_counts_enlarged_output(tmp_path, monkeypatch):
    import revive.worker

    source = tmp_path / "source.png"
    Image.new("RGB", (500, 500)).save(source)
    # Enough for old-size PNG, insufficient for planned 2x artifact.
    monkeypatch.setattr(
        revive.worker.shutil, "disk_usage", lambda _: SimpleNamespace(free=6_000_000)
    )
    with pytest.raises(ReviveError) as failure:
        process(
            {
                "snapshot": str(source),
                "output": str(tmp_path / "out.png"),
                "settings": {
                    "adapter": "normalize_png",
                    "max_input_megapixels": 40,
                    "worker_memory_mib": 2048,
                    "worker_commit_mib": 2048,
                    "sr_enabled": True,
                },
            }
        )
    assert failure.value.code == "DISK_LOW"
    assert not (tmp_path / "out.png").exists()


def test_missing_model_fails_without_importing_torch(tmp_path):
    code = "from pathlib import Path; import sys; from revive.swinir import SwinIRAdapter; from revive.errors import ReviveError\ntry:\n SwinIRAdapter({'app_data':sys.argv[1]}).load()\nexcept ReviveError as e:\n assert e.code=='MODEL_MISSING'\nelse:\n raise AssertionError('missing model accepted')\nassert 'torch' not in sys.modules"
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)], capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr.decode()


def test_new_catalog_entry_uses_shared_verified_installer(config, monkeypatch):
    import revive.swinir_model as asset

    data = b"synthetic upscaler checkpoint"
    monkeypatch.setattr(asset, "WEIGHT_BYTES", len(data))
    monkeypatch.setattr(asset, "WEIGHT_SHA256", hashlib.sha256(data).hexdigest())

    class Response(io.BytesIO):
        def geturl(self):
            return "https://github.com/official-test"

    monkeypatch.setattr(models, "urlopen", lambda *a, **kw: Response(data))
    config.app_data.mkdir()
    models.install_model(config.app_data, asset.MODEL_ID)
    assert models.model_status(config.app_data, asset.MODEL_ID)["verified"]
    assert models.model_path(config.app_data, asset.MODEL_ID).name == "model.pth"
    assert not models.model_status(config.app_data)["installed"]
    models.model_path(config.app_data, asset.MODEL_ID).write_bytes(b"x" * len(data))
    with pytest.raises(ReviveError):
        models.install_model(config.app_data, asset.MODEL_ID)


def test_source_integrity_and_optional_inference_helpers():
    root = Path(__file__).resolve().parents[1] / "src/revive/_vendor/swinir"
    manifest = json.loads((root / "upstream.json").read_text())
    for name, entry in manifest["files"].items():
        assert (
            hashlib.sha256((root / name).read_bytes()).hexdigest()
            == entry["packaged_sha256"]
        )
    torch = pytest.importorskip("torch")
    from revive._vendor.swinir.compat import DropPath

    layer = DropPath(0.1)
    with pytest.raises(RuntimeError, match="evaluation only"):
        layer(torch.ones(1))
    layer.eval()
    value = torch.ones(1)
    assert layer(value) is value


def test_neutral_photo_stays_neutral_but_small_colored_marks_prevent_conversion(
    monkeypatch,
):
    np = pytest.importorskip("numpy")
    from revive.swinir import SwinIRAdapter

    monkeypatch.setattr(SwinIRAdapter, "load", lambda self: None)

    def predict(model, tile):
        result = np.zeros((tile.height * 2, tile.width * 2, 3), dtype=np.float32)
        result[:, :, 0] = 1
        return result

    monkeypatch.setattr(SwinIRAdapter, "predict", staticmethod(predict))
    adapter = SwinIRAdapter({})
    image = Image.new("RGB", (20, 20), "gray")
    neutral, details = adapter.upscale(image, (40, 40))
    r, g, b = neutral.getpixel((0, 0))
    assert r == g == b and details["neutral_channels_preserved"]
    image.putpixel((10, 10), (0, 0, 255))
    colored, details = adapter.upscale(image, (40, 40))
    assert (
        colored.getpixel((0, 0)) == (255, 0, 0)
        and not details["neutral_channels_preserved"]
    )


def test_combined_stage_order_and_published_name(config, monkeypatch):
    from revive import colorization
    from revive.engine import Engine
    from revive.swinir import SwinIRAdapter

    calls = []

    def fake_colorize(image, inspection, settings, stages, notes):
        calls.append("colorize")
        image.paste((40, 80, 120), (0, 0, image.width, image.height))
        return image, {"enabled": True, "applied": True}

    def fake_upscale(self, image, target):
        assert image.getpixel((0, 0)) == (40, 80, 120)
        calls.append("upscale")
        return image.resize(target), {"model": "test-double"}

    monkeypatch.setattr(colorization, "colorize_if_needed", fake_colorize)
    monkeypatch.setattr(SwinIRAdapter, "upscale", fake_upscale)
    current = replace(
        config,
        adapter="normalize_png",
        colorization_enabled=True,
        sr_enabled=True,
        worker_memory_mib=2048,
    )
    source = current.input / "photo.png"
    Image.new("RGB", (30, 20), "gray").save(source)
    original = source.read_bytes()
    instance = Engine(current)
    try:
        monkeypatch.setattr(
            instance,
            "_worker",
            lambda snapshot, output: process(
                {
                    "snapshot": str(snapshot),
                    "output": str(output),
                    "settings": current.worker_settings(),
                }
            ),
        )
        instance.run(once=True)
        assert instance.state.counts() == {"completed": 1}
        row = instance.state.db.execute(
            "SELECT output_path,metadata FROM jobs"
        ).fetchone()
        assert "_colorized_upscaled_" in row["output_path"]
        metadata = json.loads(row["metadata"])
        assert metadata["colors_inferred"] and metadata["details_inferred"]
        assert calls == ["colorize", "upscale"] and source.read_bytes() == original
    finally:
        instance.close()
