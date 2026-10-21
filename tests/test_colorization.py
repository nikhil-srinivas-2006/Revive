import hashlib
import io
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image

from revive import models
from revive.colorization import colorize_if_needed, eligibility
from revive.config import load_config
from revive.errors import ReviveError


def grayscale(size=(60, 80)):
    image = Image.new("RGB", size)
    image.putdata(
        [((x * 13 + y * 7) % 256,) * 3 for y in range(size[1]) for x in range(size[0])]
    )
    return image


def test_grayscale_requires_full_resolution_check():
    image = grayscale((1000, 1000))
    assert eligibility(image, {"color_class": "grayscale_candidate"})[0]
    image.putpixel((500, 500), (255, 0, 0))
    assert not eligibility(image, {"color_class": "grayscale_candidate"})[0]


@pytest.mark.parametrize(
    "mode,classification",
    [
        ("RGBA", "grayscale_candidate"),
        ("RGB", "color_or_uncertain"),
        ("RGB", "unknown"),
    ],
)
def test_ineligible_never_opens_model(mode, classification, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Model must not be loaded for skipped inputs")

    import revive.ddcolor

    monkeypatch.setattr(revive.ddcolor.DDColorAdapter, "predict", forbidden)
    image = Image.new(mode, (10, 10), "gray")
    stages, notes = [], []
    result, details = colorize_if_needed(
        image,
        {"color_class": classification},
        {"colorization_enabled": True},
        stages,
        notes,
    )
    assert result is image
    assert not details["applied"]
    assert not stages


def test_sepia_is_explicit_opt_in():
    image = Image.new("RGB", (10, 10), (150, 120, 100))
    assert not eligibility(image, {"color_class": "sepia_candidate"})[0]
    assert eligibility(image, {"color_class": "sepia_candidate"}, True)[0]


def test_zero_strength_needs_no_model():
    image = grayscale()
    result, details = colorize_if_needed(
        image,
        {"color_class": "grayscale_candidate"},
        {"colorization_enabled": True, "colorization_strength": 0},
        [],
        [],
    )
    assert result is image
    assert details["reason"] == "ZERO_STRENGTH"


def test_low_budget_defers_without_loading_model():
    with pytest.raises(ReviveError) as error:
        colorize_if_needed(
            grayscale(),
            {"color_class": "grayscale_candidate"},
            {"colorization_enabled": True, "worker_memory_mib": 128},
            [],
            [],
        )
    assert error.value.code == "RAM_ADMISSION"
    assert error.value.retryable


def test_low_commit_budget_defers_even_with_enough_physical_budget():
    with pytest.raises(ReviveError) as error:
        colorize_if_needed(
            grayscale(),
            {"color_class": "grayscale_candidate"},
            {
                "colorization_enabled": True,
                "worker_memory_mib": 2048,
                "worker_commit_mib": 1024,
            },
            [],
            [],
        )
    assert error.value.code == "RAM_ADMISSION"


def test_missing_model_does_not_import_torch(tmp_path):
    source = "from pathlib import Path; from revive.ddcolor import DDColorAdapter; from revive.errors import ReviveError; import sys\n"
    source += f"try:\n DDColorAdapter({{'app_data': {str(tmp_path)!r}}}).predict(None)\nexcept ReviveError as e:\n assert e.code=='MODEL_MISSING'\nelse:\n raise AssertionError('Expected missing model')\nassert 'torch' not in sys.modules"
    result = subprocess.run(
        [sys.executable, "-c", source], capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr.decode()


@pytest.fixture
def tiny_asset(config, monkeypatch):
    data = b"test checkpoint bytes (not a real AI model)"
    monkeypatch.setattr(models, "WEIGHT_BYTES", len(data))
    monkeypatch.setattr(models, "WEIGHT_SHA256", hashlib.sha256(data).hexdigest())
    config.app_data.mkdir()
    return data


def test_registry_install_verifies_atomic_asset(config, tiny_asset, monkeypatch):
    class Response(io.BytesIO):
        def geturl(self):
            return "https://huggingface.co/verified-test"

    monkeypatch.setattr(models, "urlopen", lambda *args, **kwargs: Response(tiny_asset))
    models.install_model(config.app_data)
    assert models.model_status(config.app_data)["verified"]
    with models.verified_checkpoint(config.app_data) as stream:
        assert stream.read() == tiny_asset
    assert not list(models.model_path(config.app_data).parent.glob("*.part"))


def test_registry_corruption_is_never_overwritten(config, tiny_asset):
    target = models.model_path(config.app_data)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x" * len(tiny_asset))
    with pytest.raises(ReviveError) as error:
        models.install_model(config.app_data)
    assert error.value.code == "MODEL_HASH_MISMATCH"
    assert target.read_bytes() == b"x" * len(tiny_asset)


@pytest.mark.parametrize("content", [b"short", b"x" * 100])
def test_bad_download_never_published(config, tiny_asset, monkeypatch, content):
    class Response(io.BytesIO):
        def geturl(self):
            return "https://huggingface.co/test"

    monkeypatch.setattr(models, "urlopen", lambda *args, **kwargs: Response(content))
    with pytest.raises(ReviveError) as error:
        models.install_model(config.app_data)
    assert error.value.code == "MODEL_HASH_MISMATCH"
    assert not models.model_path(config.app_data).exists()
    assert not list(models.model_path(config.app_data).parent.glob("*.part"))


def test_model_status_is_offline_and_does_not_create_directories(config, monkeypatch):
    monkeypatch.setattr(
        models, "urlopen", lambda *_: pytest.fail("Unexpected network call")
    )
    assert not models.model_status(config.app_data)["installed"]
    assert not config.app_data.exists()


def test_monitor_never_imports_ai_or_color_modules():
    code = "import revive.engine, revive.cli, sys; assert not {'torch','cv2','numpy','revive.pipeline','revive.ddcolor'} & sys.modules.keys()"
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr.decode()


@pytest.mark.parametrize(
    "settings",
    [
        {"colorization_strength": -0.1},
        {"colorization_strength": float("nan")},
        {"cpu_threads": True},
        {"colorization_enabled": "yes"},
        {"colorization_input_size": 128},
        {"colorize_sepia": 1},
    ],
)
def test_colorization_config_is_strict(config, settings):
    with pytest.raises(ReviveError):
        replace(config, **settings).validate()


def test_colorization_rejects_mock_adapter(config):
    with pytest.raises(ReviveError):
        replace(config, colorization_enabled=True).validate()


def test_colorization_settings_load_and_fingerprint(config, tmp_path):
    file = tmp_path / "config.toml"
    file.write_text(
        "schema_version = 1\n[paths]\n"
        + "\n".join(
            f"{name} = {json.dumps(getattr(config, name).as_posix())}"
            for name in ("input", "output", "app_data")
        )
        + '\n[pipeline]\nadapter = "normalize_png"\n[colorization]\nenabled = true\nstrength = 0.7\ninclude_sepia = false\ninput_size = 256\n'
    )
    loaded = load_config(file)
    assert loaded.colorization_enabled
    assert loaded.colorization_strength == 0.7
    assert loaded.fingerprint != replace(loaded, colorization_enabled=False).fingerprint
    assert loaded.fingerprint != replace(loaded, colorization_strength=0.6).fingerprint


def test_physical_and_committed_limits_are_distinct(monkeypatch):
    from revive import resources

    monkeypatch.setattr(resources, "memory_mib", lambda: (8192, 2048))
    monkeypatch.setattr(resources, "available_commit_mib", lambda: 8192)
    assert resources.worker_budget_mib() == 1024
    assert resources.worker_commit_budget_mib() == 2048
    monkeypatch.setattr(resources, "available_commit_mib", lambda: 1024)
    assert resources.worker_commit_budget_mib() == 512
    assert resources.worker_commit_budget_mib(768) == 768


def test_vendored_source_integrity():
    root = Path(__file__).resolve().parents[1] / "src/revive/_vendor/ddcolor"
    manifest = json.loads((root / "upstream.json").read_text(encoding="utf-8"))
    for name, entry in manifest["files"].items():
        assert (
            hashlib.sha256((root / name).read_bytes()).hexdigest()
            == entry["packaged_sha256"]
        )


def test_chroma_reconstruction_preserves_luminance_and_geometry():
    np = pytest.importorskip("numpy")
    cv2 = pytest.importorskip("cv2")
    from revive.ddcolor import reconstruct_chroma

    image = grayscale((83, 137))  # Cross several reconstruction strips.
    chroma = np.zeros((16, 16, 2), dtype=np.float32)
    chroma[:, :, 0], chroma[:, :, 1] = 70, -40
    result, error = reconstruct_chroma(image, chroma, 0.85)
    assert result.size == image.size
    assert error <= 1.0
    before = cv2.cvtColor(
        np.asarray(image).astype(np.float32) / 255, cv2.COLOR_RGB2LAB
    )[:, :, 0]
    after = cv2.cvtColor(
        np.asarray(result).astype(np.float32) / 255, cv2.COLOR_RGB2LAB
    )[:, :, 0]
    assert float(np.max(np.abs(before - after))) <= 1.0
    assert result.tobytes() != image.tobytes()


def test_bad_chroma_rejected():
    np = pytest.importorskip("numpy")
    pytest.importorskip("cv2")
    from revive.ddcolor import reconstruct_chroma

    with pytest.raises(ReviveError):
        reconstruct_chroma(
            grayscale(), np.full((16, 16, 2), np.nan, dtype=np.float32), 0.85
        )


def test_colorized_provenance_and_ineligible_bypass(tmp_path, monkeypatch):
    np = pytest.importorskip("numpy")
    pytest.importorskip("cv2")
    from revive.ddcolor import DDColorAdapter
    from revive.worker import process

    calls = []

    def fake_predict(self, image):
        calls.append(image.size)
        return np.full((16, 16, 2), 25, dtype=np.float32)

    monkeypatch.setattr(DDColorAdapter, "predict", fake_predict)
    source, output = tmp_path / "gray.png", tmp_path / "out.png"
    grayscale().save(source)
    settings = {
        "adapter": "normalize_png",
        "max_input_megapixels": 40,
        "colorization_enabled": True,
        "worker_memory_mib": 2048,
    }
    result = process(
        {"snapshot": str(source), "output": str(output), "settings": settings}
    )
    assert len(calls) == 1
    assert result["metadata"]["colors_inferred"] is True
    with Image.open(output) as final:
        provenance = json.loads(final.text["revive:provenance"])
        assert provenance["colors_inferred"] is True
        assert provenance["colorization"]["weight_sha256"] == models.WEIGHT_SHA256
    color = tmp_path / "color.png"
    Image.new("RGB", (10, 10), (0, 255, 0)).save(color)
    result = process(
        {
            "snapshot": str(color),
            "output": str(tmp_path / "color_out.png"),
            "settings": settings,
        }
    )
    assert len(calls) == 1
    assert result["metadata"]["colors_inferred"] is False
