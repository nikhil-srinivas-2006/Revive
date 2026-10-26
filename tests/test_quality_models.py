import hashlib
import io
import json
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image

from revive.config import load_config
from revive.errors import ReviveError
from revive.models import model_info, model_status
from revive.resources import quality_budget_mib


@pytest.mark.parametrize("model", ["ddcolor_modelscope", "realesrgan_x2plus"])
def test_quality_catalog_and_offline_missing_status(tmp_path, monkeypatch, model):
    from revive import models

    monkeypatch.setattr(
        models, "urlopen", lambda *_: pytest.fail("Unexpected download")
    )
    info = model_info(model)
    assert len(info["sha256"]) == 64 and info["bytes"] > 0
    assert info["url"].startswith("https://")
    assert not model_status(tmp_path / "missing", model)["installed"]
    assert not (tmp_path / "missing").exists()


def test_model_selection_configuration_and_policy_identity(config, tmp_path):
    current = replace(
        config, adapter="normalize_png", colorization_enabled=True, sr_enabled=True
    )
    quality = replace(
        current, colorization_model="ddcolor_modelscope", sr_model="realesrgan_x2plus"
    )
    quality.validate()
    assert quality.fingerprint != current.fingerprint
    assert (
        replace(quality, sr_model=current.sr_model).fingerprint != quality.fingerprint
    )
    assert (
        replace(quality, colorization_model=current.colorization_model).fingerprint
        != quality.fingerprint
    )
    assert replace(quality, worker_memory_mib=4096).fingerprint == quality.fingerprint
    settings = quality.worker_settings()
    assert settings["colorization_model"] == "ddcolor_modelscope"
    assert settings["sr_model"] == "realesrgan_x2plus"
    path = tmp_path / "quality.toml"
    path.write_text(
        "schema_version=1\n[paths]\n"
        + "\n".join(
            f"{name}={json.dumps(str(getattr(config, name)))}"
            for name in ("input", "output", "app_data")
        )
        + '\n[pipeline]\nadapter="normalize_png"\n[colorization]\nenabled=true\nmodel="ddcolor_modelscope"\n[super_resolution]\nenabled=true\nmodel="realesrgan_x2plus"\n'
    )
    assert load_config(path).colorization_model == quality.colorization_model
    assert load_config(path).sr_model == quality.sr_model


@pytest.mark.parametrize(
    "changes",
    [
        {"colorization_model": "unknown"},
        {"sr_model": "unknown"},
        {"colorization_model": "ddcolor_modelscope", "worker_memory_mib": 12289},
        {"sr_model": "realesrgan_x2plus", "worker_memory_mib": 12289},
        {"colorization_model": "ddcolor_modelscope", "worker_memory_mib": "large"},
    ],
)
def test_bad_model_settings_rejected(config, changes):
    with pytest.raises(ReviveError):
        replace(config, **changes).validate()


def test_quality_budget_preserves_headroom_and_twelve_gib_cap(monkeypatch):
    from revive import resources

    monkeypatch.setattr(resources, "memory_mib", lambda: (32768, 30000))
    monkeypatch.setattr(resources, "available_commit_mib", lambda: 48000)
    assert quality_budget_mib() == quality_budget_mib(commit=True) == 12288
    monkeypatch.setattr(resources, "memory_mib", lambda: (8192, 2048))
    assert quality_budget_mib() == 1024
    assert quality_budget_mib(commit=True) == 4096
    assert quality_budget_mib(2048) == 2048


@pytest.mark.parametrize("size", [256, 512])
def test_large_colorizer_admission_precedes_checkpoint_access(size, monkeypatch):
    from revive.colorization import colorize_if_needed
    from revive.ddcolor import DDColorAdapter

    monkeypatch.setattr(
        DDColorAdapter, "colorize", lambda *_: pytest.fail("Inference before admission")
    )
    settings = {
        "colorization_enabled": True,
        "colorization_model": "ddcolor_modelscope",
        "colorization_input_size": size,
        "worker_memory_mib": 1024,
        "worker_commit_mib": 12288,
    }
    image = Image.new("RGB", (32, 24), "gray")
    with pytest.raises(ReviveError) as error:
        colorize_if_needed(
            image, {"color_class": "grayscale_candidate"}, settings, [], []
        )
    assert error.value.code == "RAM_ADMISSION" and error.value.retryable


def test_cli_uses_selected_colorizer_and_reports_both_models(config, tmp_path, capsys):
    from revive.cli import main

    path = tmp_path / "selected.toml"
    path.write_text(
        "schema_version=1\n[paths]\n"
        + "\n".join(
            f"{name}={json.dumps(str(getattr(config, name)))}"
            for name in ("input", "output", "app_data")
        )
        + '\n[pipeline]\nadapter="normalize_png"\n[colorization]\nmodel="ddcolor_modelscope"\n[super_resolution]\nmodel="realesrgan_x2plus"\n'
    )
    assert main(["--config", str(path), "models", "status"]) == 0
    assert json.loads(capsys.readouterr().out)["id"] == "ddcolor_modelscope"
    assert main(["--config", str(path), "doctor"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["colorization_model"] == "ddcolor_modelscope"
    assert result["super_resolution_model"] == "realesrgan_x2plus"


def test_vendored_rrdb_source_integrity():
    root = Path(__file__).resolve().parents[1] / "src/revive/_vendor/realesrgan"
    manifest = json.loads((root / "upstream.json").read_text())
    for name, entry in manifest["files"].items():
        assert (
            hashlib.sha256((root / name).read_bytes()).hexdigest()
            == entry["packaged_sha256"]
        )


@pytest.mark.parametrize("size", [(1, 1), (9, 17), (131, 97)])
def test_rrdb_tiles_preserve_channel_order_and_cover_odd_edges(size):
    np = pytest.importorskip("numpy")
    from revive.swinir import tiled_upscale

    image = Image.new("RGB", size, (17, 91, 203))

    def predict(tile):
        assert tile.width % 2 == tile.height % 2 == 0
        return np.repeat(
            np.repeat(np.asarray(tile, dtype=np.float32) / 255, 2, axis=0), 2, axis=1
        )

    output, count = tiled_upscale(image, predict, 64, alignment=2, pre_pad=10)
    assert output.size == tuple(x * 2 for x in size)
    assert output.getextrema() == ((17, 17), (91, 91), (203, 203))
    assert count > 0


def test_large_encoder_selected_and_strict_state_load(monkeypatch, tmp_path):
    torch = pytest.importorskip("torch")
    pytest.importorskip("cv2")
    from revive import ddcolor
    from revive._vendor.ddcolor import model as architecture

    received = {}

    class TinyDouble(torch.nn.Module):
        def __init__(self, **kwargs):
            super().__init__()
            received.update(kwargs)
            self.weight = torch.nn.Parameter(torch.ones(1))

        def forward(self, value):
            assert not torch.is_grad_enabled()
            return torch.zeros((1, 2, *value.shape[2:]))

    @contextmanager
    def checkpoint(*args):
        stream = io.BytesIO()
        torch.save({"weight": torch.ones(1)}, stream)
        stream.seek(0)
        yield stream

    monkeypatch.setattr(architecture, "DDColor", TinyDouble)
    monkeypatch.setattr(ddcolor, "verified_checkpoint", checkpoint)
    adapter = ddcolor.DDColorAdapter(
        {"app_data": str(tmp_path), "colorization_model": "ddcolor_modelscope"}
    )
    assert adapter.predict(Image.new("RGB", (16, 16), "gray")).shape == (256, 256, 2)
    assert received["encoder_name"] == "convnext-l"
    assert received["input_size"] == (32, 32)


def test_real_rrdb_forward_uses_native_two_times_scale():
    torch = pytest.importorskip("torch")
    from revive._vendor.realesrgan.rrdbnet_arch import RRDBNet

    model = RRDBNet(3, 3, scale=2, num_feat=8, num_block=1, num_grow_ch=4).eval()
    with torch.inference_mode():
        output = model(torch.zeros(1, 3, 6, 8))
    assert output.shape == (1, 3, 12, 16)
    assert torch.isfinite(output).all()


def test_new_adapters_route_together_and_record_provenance(tmp_path, monkeypatch):
    from revive.ddcolor import DDColorAdapter
    from revive.realesrgan import RealESRGANAdapter
    from revive.worker import process

    calls = []

    def colorize(self, image, strength):
        calls.append("colorize")
        return Image.new("RGB", image.size, (20, 80, 140)), {
            "model": "ddcolor_modelscope"
        }

    def upscale(self, image, target):
        calls.append("upscale")
        assert image.getpixel((0, 0)) == (20, 80, 140)
        return image.resize(target), {"model": "realesrgan_x2plus"}

    monkeypatch.setattr(DDColorAdapter, "colorize", colorize)
    monkeypatch.setattr(RealESRGANAdapter, "upscale", upscale)
    source, output = tmp_path / "original.png", tmp_path / "derivative.png"
    Image.new("RGB", (32, 24), "gray").save(source)
    original = source.read_bytes()
    result = process(
        {
            "snapshot": str(source),
            "output": str(output),
            "settings": {
                "adapter": "normalize_png",
                "max_input_megapixels": 40,
                "worker_memory_mib": 8192,
                "worker_commit_mib": 8192,
                "colorization_enabled": True,
                "colorization_model": "ddcolor_modelscope",
                "sr_enabled": True,
                "sr_model": "realesrgan_x2plus",
            },
        }
    )
    assert calls == ["colorize", "upscale"] and source.read_bytes() == original
    assert result["metadata"]["stages"][1:3] == [
        "ddcolor_modelscope_colorization",
        "realesrgan_x2plus",
    ]
    with Image.open(output) as image:
        assert image.size == (64, 48)
        info = json.loads(image.text["revive:provenance"])
        assert info["colors_inferred"] and info["details_inferred"]
