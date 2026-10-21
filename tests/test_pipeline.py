import base64
import io
import json
import random
import struct
import time
from dataclasses import replace

import pytest
from PIL import Image, ImageCms, ImageOps, PngImagePlugin

from revive.engine import Engine, file_hash
from revive.errors import ReviveError
from revive.pipeline import inspect_pixels, pixel_hash, validate_png
from revive.worker import process


def run_pipeline(tmp_path, image, fmt="PNG", settings=None, **save_options):
    source, output = tmp_path / "source", tmp_path / "output"
    image.save(source, format=fmt, **save_options)
    result = process(
        {
            "snapshot": str(source),
            "output": str(output),
            "settings": {
                "adapter": "normalize_png",
                "max_input_megapixels": 40,
                "worker_memory_mib": 512,
                **(settings or {}),
            },
        }
    )
    return source, output, result


def pattern():
    image = Image.new("RGB", (9, 5))
    image.putdata([(x * 25, y * 40, (x + y) * 15) for y in range(5) for x in range(9)])
    return image


@pytest.mark.parametrize("orientation", range(1, 9))
def test_all_exif_orientations_applied_exactly_once(tmp_path, orientation):
    image = pattern()
    exif = Image.Exif()
    exif[274] = orientation
    exif[315] = "Family archive"
    source, output, result = run_pipeline(tmp_path, image, exif=exif)
    with Image.open(source) as original, Image.open(output) as final:
        expected = ImageOps.exif_transpose(original).convert("RGB")
        assert final.size == expected.size
        assert final.tobytes() == expected.tobytes()
        assert final.getexif().get(274, 1) == 1
        assert final.getexif()[315] == "Family archive"
    assert result["metadata"]["source"]["orientation"] == orientation
    raw = result["metadata"]["original_metadata"]["exif"]
    saved = Image.Exif()
    saved.load(base64.b64decode(raw["value"]))
    assert saved[274] == orientation


def test_tiff_orientation_not_applied_twice(tmp_path):
    exif = Image.Exif()
    exif[274] = 6
    source, output, result = run_pipeline(tmp_path, pattern(), fmt="TIFF", exif=exif)
    with Image.open(source) as image, Image.open(output) as final:
        image.load()
        expected = ImageOps.exif_transpose(image).convert("RGB")
        assert final.size == expected.size
        assert final.tobytes() == expected.tobytes()
    assert result["metadata"]["source"]["orientation"] == 6


def test_rgb_pixels_unchanged_by_default_and_labeled_assumption(tmp_path):
    image = pattern()
    _, output, result = run_pipeline(tmp_path, image)
    with Image.open(output) as final:
        assert final.format == "PNG"
        assert final.mode == "RGB"
        assert final.tobytes() == image.tobytes()
        assert ImageCms.getProfileName(
            ImageCms.ImageCmsProfile(io.BytesIO(final.info["icc_profile"]))
        )
    assert result["metadata"]["notes"] == ["ASSUMED_SRGB"]
    assert result["metadata"]["restoration_performed"] is False
    assert result["metadata"]["colors_inferred"] is False


def test_profiled_rgb_conversion_preserves_valid_profile(tmp_path):
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    _, output, result = run_pipeline(tmp_path, pattern(), icc_profile=profile)
    assert "icc_to_srgb" in result["metadata"]["stages"]
    assert "ASSUMED_SRGB" not in result["metadata"]["notes"]
    assert (
        base64.b64decode(
            result["metadata"]["original_metadata"]["icc_profile"]["value"]
        )
        == profile
    )
    with Image.open(output) as final:
        assert final.info["icc_profile"]


def test_lab_profile_converts_to_srgb(tmp_path):
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("LAB")).tobytes()
    lab = Image.new("LAB", (5, 7), (128, 128, 128))
    _, output, result = run_pipeline(tmp_path, lab, fmt="TIFF", icc_profile=profile)
    with Image.open(output) as final:
        assert final.mode == "RGB"
        assert final.size == lab.size
    assert "icc_to_srgb" in result["metadata"]["stages"]


def test_invalid_icc_fails_instead_of_silently_retagging(tmp_path):
    with pytest.raises(ReviveError) as error:
        run_pipeline(tmp_path, pattern(), icc_profile=b"not a color profile")
    assert error.value.code == "COLOR_PROFILE_INVALID"
    assert not (tmp_path / "output").exists()


def test_untagged_cmyk_needs_profile(tmp_path):
    with pytest.raises(ReviveError) as error:
        run_pipeline(tmp_path, Image.new("CMYK", (5, 5)), fmt="JPEG")
    assert error.value.code == "COLOR_PROFILE_REQUIRED"


@pytest.mark.parametrize("mode", ["RGBA", "LA", "P"])
def test_transparency_preserved_and_cleanup_skipped(tmp_path, mode):
    image = pattern().convert("RGBA")
    alpha = Image.new("L", image.size)
    alpha.putdata([0, 127, 255] * 15)
    image.putalpha(alpha)
    options = {}
    if mode == "LA":
        image = image.convert("LA")
    elif mode == "P":
        image = image.convert("P")
    source, output, result = run_pipeline(
        tmp_path,
        image,
        settings={"denoise_strength": 0.2, "contrast_strength": 0.2},
        **options,
    )
    with Image.open(source) as before, Image.open(output) as after:
        assert before.convert("RGBA").tobytes() == after.convert("RGBA").tobytes()
        assert after.mode == "RGBA"
    assert "CLEANUP_SKIPPED_TRANSPARENCY" in result["metadata"]["notes"]


def test_fully_transparent_inspection_unknown(tmp_path):
    _, _, result = run_pipeline(tmp_path, Image.new("RGBA", (10, 10), (255, 0, 0, 0)))
    assert result["metadata"]["inspection"]["color_class"] == "unknown"


@pytest.mark.parametrize("mode", ["1", "L", "RGB"])
def test_grayscale_inspection_is_candidate_not_historical_claim(tmp_path, mode):
    image = Image.new("L", (50, 40), 100).convert(mode)
    _, _, result = run_pipeline(tmp_path, image)
    inspection = result["metadata"]["inspection"]
    assert inspection["color_class"] == "grayscale_candidate"
    assert inspection["quality_scores_are_heuristic"] is True
    assert inspection["faces"] == "not_analyzed"


def test_sepia_and_color_inspection_candidates():
    sepia = inspect_pixels(Image.new("RGB", (10, 10), (140, 120, 95)))
    color = inspect_pixels(Image.new("RGB", (10, 10), (0, 255, 20)))
    assert sepia["color_class"] == "sepia_candidate"
    assert color["color_class"] == "color_or_uncertain"


def test_sixteen_bit_input_fails_without_reducing_bit_depth(tmp_path):
    with pytest.raises(ReviveError) as error:
        run_pipeline(tmp_path, Image.new("I;16", (10, 10), 32768))
    assert error.value.code == "BIT_DEPTH_UNSUPPORTED"
    assert not (tmp_path / "output").exists()


def test_png_text_and_provenance_retained(tmp_path):
    text = PngImagePlugin.PngInfo()
    text.add_itxt("Description", "Family holiday — été")
    text.add_itxt("revive:provenance", "old provenance")
    _, output, result = run_pipeline(tmp_path, pattern(), pnginfo=text)
    with Image.open(output) as final:
        assert final.text["Description"] == "Family holiday — été"
        assert json.loads(final.text["revive:provenance"])["colors_inferred"] is False
    assert (
        result["metadata"]["original_metadata"]["png_text"]["revive:provenance"]
        == "old provenance"
    )


def test_xmp_retained_in_manifest_without_stale_orientation(tmp_path):
    text = PngImagePlugin.PngInfo()
    text.add_itxt("XML:com.adobe.xmp", '<rdf tiff:Orientation="6"/>')
    _, output, result = run_pipeline(tmp_path, pattern(), pnginfo=text)
    with Image.open(output) as final:
        assert "XML:com.adobe.xmp" not in final.text
    assert "ORIGINAL_XMP_RETAINED_IN_MANIFEST" in result["metadata"]["notes"]
    assert result["metadata"]["original_metadata"]["png_text"]["XML:com.adobe.xmp"]


def test_density_swapped_on_quarter_turn(tmp_path):
    exif = Image.Exif()
    exif[274] = 6
    exif[282], exif[283] = 100, 200
    _, output, _ = run_pipeline(tmp_path, pattern(), exif=exif, dpi=(100, 200))
    with Image.open(output) as final:
        assert final.info["dpi"] == pytest.approx((200, 100), abs=0.05)
        assert final.getexif()[282] == 200
        assert final.getexif()[283] == 100


def test_explicit_denoise_reduces_impulses_conservatively(tmp_path):
    image = Image.new("RGB", (100, 100), (128, 128, 128))
    rng = random.Random(10)
    for _ in range(1000):
        image.putpixel((rng.randrange(100), rng.randrange(100)), (255, 255, 255))
    _, output, result = run_pipeline(
        tmp_path, image, settings={"denoise_strength": 0.2}
    )
    assert "mild_median_denoise" in result["metadata"]["stages"]
    assert result["metadata"]["restoration_performed"] is True
    with Image.open(output) as final:
        assert final.size == image.size
        assert sum(
            final.getpixel((x, y))[0] for y in range(100) for x in range(100)
        ) < sum(image.getpixel((x, y))[0] for y in range(100) for x in range(100))


def test_denoise_skipped_without_evidence(tmp_path):
    _, _, result = run_pipeline(
        tmp_path,
        Image.new("RGB", (10, 10), (100, 100, 100)),
        settings={"denoise_strength": 0.2},
    )
    assert "DENOISE_SKIPPED_NO_EVIDENCE" in result["metadata"]["notes"]
    assert result["metadata"]["restoration_performed"] is False


def test_explicit_contrast_selective_and_bounded(tmp_path):
    image = Image.new("RGB", (100, 30))
    image.putdata([(100 + x % 40,) * 3 for x in range(3000)])
    _, output, result = run_pipeline(
        tmp_path, image, settings={"contrast_strength": 0.2}
    )
    assert "mild_tonal_adjustment" in result["metadata"]["stages"]
    with Image.open(output) as final:
        assert final.size == image.size
        assert all(
            abs(a - b) <= 51
            for a, b in zip(image.tobytes(), final.tobytes(), strict=True)
        )


def test_png_validation_detects_pixel_change(tmp_path):
    image = pattern()
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    other = Image.new("RGB", image.size, "red")
    output = tmp_path / "output"
    other.save(output, format="PNG", icc_profile=profile)
    with pytest.raises(ReviveError) as error:
        validate_png(output, image, profile)
    assert error.value.code == "OUTPUT_INVALID"


def test_memory_admission_before_full_decode(tmp_path):
    with pytest.raises(ReviveError) as error:
        run_pipeline(tmp_path, pattern(), settings={"worker_memory_mib": 1})
    assert error.value.code == "RAM_ADMISSION"
    assert error.value.retryable


def test_png_adapter_through_scheduler_and_real_worker(config):
    config = replace(config, adapter="normalize_png")
    image = pattern()
    source = config.input / "photo.bmp"
    image.save(source)
    before = file_hash(source)
    engine = Engine(config)
    try:
        engine.scan(now=time.time())
        engine.scan(now=time.time() + 1)
        engine.process_one()
        assert engine.state.counts() == {"completed": 1}
        row = next(engine.state.export())
        assert row["output_path"].endswith(".png")
        assert "_normalized_" in row["output_path"]
        assert row["output_hash"] != before
        assert row["source_hash"] == file_hash(source) == before
        with Image.open(config.output / row["output_path"]) as output:
            assert pixel_hash(output) == pixel_hash(image)
        engine.scan()
        assert not engine.process_one()
    finally:
        engine.close()


def test_pipeline_fingerprint_includes_cleanup_but_not_resource_budget(config):
    config = replace(config, adapter="normalize_png")
    assert config.fingerprint != replace(config, denoise_strength=0.1).fingerprint
    assert config.fingerprint == replace(config, worker_memory_mib=1024).fingerprint


@pytest.mark.parametrize("strength", [-1, 0.3, float("nan"), True])
def test_cleanup_strength_rejected_outside_conservative_bounds(config, strength):
    with pytest.raises(ReviveError):
        replace(config, adapter="normalize_png", denoise_strength=strength).validate()


def test_mock_adapter_refuses_ignored_cleanup_settings(config):
    with pytest.raises(ReviveError):
        replace(config, contrast_strength=0.1).validate()


def test_memory_budget_adapts_to_available_laptop_ram(monkeypatch):
    from revive import resources

    monkeypatch.setattr(resources, "memory_mib", lambda: (4096, 1024))
    assert resources.worker_budget_mib() == 512
    monkeypatch.setattr(resources, "memory_mib", lambda: (32768, 24000))
    assert resources.worker_budget_mib() == 2048
    monkeypatch.setattr(resources, "memory_mib", lambda: (None, None))
    assert resources.worker_budget_mib() == 512
    assert resources.worker_budget_mib(768) == 768


def test_exif_subifd_dimensions_and_colorspace_updated(tmp_path):
    exif = Image.Exif()
    exif[274] = 6
    exif[34665] = {40961: 65535, 40962: 9, 40963: 5, 36867: "1965:01:01 12:00:00"}
    _, output, _ = run_pipeline(tmp_path, pattern(), exif=exif)
    with Image.open(output) as final:
        details = final.getexif().get_ifd(34665)
        assert (details[40962], details[40963]) == final.size
        assert details[40961] == 1
        assert details[36867] == "1965:01:01 12:00:00"


def test_repeat_normalization_is_byte_deterministic(tmp_path, monkeypatch):
    from revive import pipeline

    source, output, result = run_pipeline(tmp_path, pattern())
    original = ImageCms.createProfile
    profile = bytearray(ImageCms.ImageCmsProfile(original("sRGB")).tobytes())
    profile[24:36] = struct.pack(">6H", 2025, 5, 5, 3, 2, 1)
    changed_clock_profile = ImageCms.ImageCmsProfile(io.BytesIO(profile))
    monkeypatch.setattr(
        pipeline.ImageCms, "createProfile", lambda *_: changed_clock_profile.profile
    )
    second = tmp_path / "second"
    response = process(
        {
            "snapshot": str(source),
            "output": str(second),
            "settings": {
                "adapter": "normalize_png",
                "max_input_megapixels": 40,
                "worker_memory_mib": 512,
            },
        }
    )
    assert result["hash"] == response["hash"]
    assert output.read_bytes() == second.read_bytes()


def test_working_disk_admission_fails_before_output(tmp_path, monkeypatch):
    from revive import worker

    monkeypatch.setattr(
        worker.shutil, "disk_usage", lambda *_: type("Usage", (), {"free": 0})()
    )
    with pytest.raises(ReviveError) as error:
        run_pipeline(tmp_path, pattern())
    assert error.value.code == "DISK_LOW"
    assert not (tmp_path / "output").exists()


def test_encode_disk_full_is_retryable_without_source_changes(tmp_path, monkeypatch):
    image = pattern()
    source, output = tmp_path / "source", tmp_path / "output"
    image.save(source, format="PNG")
    before = source.read_bytes()

    def disk_full(*args, **kwargs):
        raise OSError(28, "No disk space")

    monkeypatch.setattr(Image.Image, "save", disk_full)
    with pytest.raises(ReviveError) as error:
        process(
            {
                "snapshot": str(source),
                "output": str(output),
                "settings": {
                    "adapter": "normalize_png",
                    "max_input_megapixels": 40,
                    "worker_memory_mib": 512,
                },
            }
        )
    assert error.value.code == "DISK_LOW"
    assert error.value.retryable
    assert source.read_bytes() == before
