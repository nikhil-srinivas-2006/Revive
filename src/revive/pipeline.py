"""Conservative image normalization, used only inside an isolated worker.

Inspection measurements are heuristics, not quality or historical-fidelity scores.
Optional colorization delegates chroma prediction to a separately loaded local
adapter. Optional super-resolution follows colorization. No face processing occurs.
"""

import base64
import hashlib
import io
import json
import struct

from PIL import (
    Image,
    ImageChops,
    ImageCms,
    ImageFilter,
    ImageOps,
    ImageStat,
    PngImagePlugin,
    features,
)

from . import __version__ as revive_version
from .errors import ReviveError

SUPPORTED_MODES = {"1", "L", "LA", "P", "RGB", "RGBA", "CMYK", "LAB", "YCbCr"}
STRUCTURAL_EXIF = {
    256,
    257,
    258,
    259,
    262,
    273,
    274,
    277,
    278,
    279,
    284,
    317,
    320,
    338,
    339,
    513,
    514,
    34675,
}


def pixel_hash(image):
    """Hash decoded pixels in strips rather than allocating another full image."""
    digest = hashlib.sha256()
    for y in range(0, image.height, 64):
        strip = image.crop((0, y, image.width, min(image.height, y + 64)))
        digest.update(strip.tobytes())
        strip.close()
    return digest.hexdigest()


def inspect_pixels(image):
    scale = min(1, 256 / max(image.size))
    sample = image.resize(
        (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
        Image.Resampling.BOX,
    )
    rgba = sample.convert("RGBA")
    # Fully transparent colors carry no evidence about the visible photograph.
    data = rgba.load()
    pixels = [
        data[x, y][:3]
        for y in range(rgba.height)
        for x in range(rgba.width)
        if data[x, y][3] > 0
    ]
    rgba.close()
    if not pixels:
        sample.close()
        return {
            "color_class": "unknown",
            "classification_basis": "no_visible_pixels",
            "faces": "not_analyzed",
            "quality_scores_are_heuristic": True,
        }
    chroma = [max(p) - min(p) for p in pixels]
    neutral = sum(c <= 2 for c in chroma) / len(chroma)
    warm = sum(r >= g >= b and 3 <= r - b <= 80 for r, g, b in pixels) / len(pixels)
    classification = (
        "grayscale_candidate"
        if neutral >= 0.995
        else ("sepia_candidate" if warm >= 0.95 else "color_or_uncertain")
    )
    gray = sample.convert("L")
    histogram = gray.histogram()
    count = sum(histogram)

    def percentile(fraction):
        accumulated = 0
        for value, frequency in enumerate(histogram):
            accumulated += frequency
            if accumulated >= max(1, fraction * count):
                return value
        return 255

    median = gray.filter(ImageFilter.MedianFilter(3))
    residual = ImageChops.difference(gray, median)
    edges = gray.filter(ImageFilter.FIND_EDGES)
    if min(gray.size) > 2:
        edges = edges.crop((1, 1, gray.width - 1, gray.height - 1))
    result = {
        "color_class": classification,
        "neutral_fraction": round(neutral, 6),
        "warm_fraction": round(warm, 6),
        "tonal_span_p01_p99": percentile(0.99) - percentile(0.01),
        "clipped_fraction": round((histogram[0] + histogram[-1]) / count, 6),
        "median_residual": round(ImageStat.Stat(residual).mean[0] / 255, 6),
        "edge_energy": round(ImageStat.Stat(edges).mean[0] / 255, 6),
        "analysis_long_edge": max(sample.size),
        "faces": "not_analyzed",
        "quality_scores_are_heuristic": True,
    }
    for buffer in (sample, gray, median, residual, edges):
        buffer.close()
    return result


def retained_metadata(image):
    """Keep source metadata in the central manifest before changing its interpretation."""
    result = {}
    exif = image.info.get("exif") or image.getexif().tobytes()
    for key, value in (
        ("exif", exif),
        ("icc_profile", image.info.get("icc_profile")),
        ("xmp", image.info.get("xmp") or image.info.get("XML:com.adobe.xmp")),
    ):
        if isinstance(value, str):
            result[key] = {"encoding": "utf-8", "value": value}
        elif value:
            result[key] = {
                "encoding": "base64",
                "value": base64.b64encode(value).decode("ascii"),
            }
    if getattr(image, "text", None):
        result["png_text"] = dict(image.text)
    iptc = [
        base64.b64encode(data).decode("ascii")
        for marker, data in getattr(image, "applist", [])
        if marker == "APP13"
    ]
    if iptc:
        result["jpeg_app13_base64"] = iptc
    if len(json.dumps(result).encode("utf-8")) > 4 * 1024**2:
        raise ReviveError(
            "METADATA_LIMIT", "Image metadata exceeds the supported manifest size."
        )
    return result


def to_srgb(image, stages, notes):
    has_alpha = image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info
    alpha = image.convert("RGBA").getchannel("A") if has_alpha else None
    profile = image.info.get("icc_profile")
    # LittleCMS inserts the current creation time. Canonicalize non-color header
    # fields so identical source/policy pairs produce identical PNG bytes.
    profile_bytes = bytearray(
        ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    )
    profile_bytes[24:36] = struct.pack(">6H", 2000, 1, 1, 0, 0, 0)
    profile_bytes[84:100] = bytes(16)  # Optional profile ID is unspecified.
    srgb = ImageCms.ImageCmsProfile(io.BytesIO(profile_bytes))
    if profile:
        try:
            source_profile = ImageCms.ImageCmsProfile(io.BytesIO(profile))
            profile_input = image
            # Palette and alpha containers must be expressed in the ICC color space.
            if image.mode not in ("RGB", "CMYK", "LAB", "L"):
                profile_input = image.convert(
                    "L" if image.mode == "LA" else ("RGBA" if has_alpha else "RGB")
                )
                if profile_input.mode == "RGBA":
                    opaque = profile_input.convert("RGB")
                    profile_input.close()
                    profile_input = opaque
            converted = ImageCms.profileToProfile(
                profile_input, source_profile, srgb, outputMode="RGB", renderingIntent=1
            )
            if profile_input is not image:
                profile_input.close()
            stages.append("icc_to_srgb")
        except (ImageCms.PyCMSError, OSError, ValueError) as exc:
            raise ReviveError(
                "COLOR_PROFILE_INVALID",
                "Embedded color profile cannot be safely converted.",
            ) from exc
    else:
        if image.mode in ("CMYK", "LAB"):
            raise ReviveError(
                "COLOR_PROFILE_REQUIRED",
                "CMYK/LAB input needs a valid embedded color profile.",
            )
        if has_alpha:
            rgba = image.convert("RGBA")
            converted = rgba.convert("RGB")
            rgba.close()
        else:
            converted = image.convert("RGB")
        notes.append("ASSUMED_SRGB")
        stages.append("assume_srgb")
    if alpha is not None:
        converted.putalpha(alpha)
        alpha.close()
    return converted, srgb.tobytes()


def cleanup(image, inspection, settings, stages, notes):
    denoise = settings.get("denoise_strength", 0)
    contrast = settings.get("contrast_strength", 0)
    if image.mode == "RGBA":
        if denoise or contrast:
            notes.append("CLEANUP_SKIPPED_TRANSPARENCY")
        return image
    if denoise and inspection.get("median_residual", 0) >= 0.015:
        filtered = image.filter(ImageFilter.MedianFilter(3))
        candidate = Image.blend(image, filtered, denoise)
        filtered.close()
        image.close()
        image = candidate
        stages.append("mild_median_denoise")
    elif denoise:
        notes.append("DENOISE_SKIPPED_NO_EVIDENCE")
    span = inspection.get("tonal_span_p01_p99", 0)
    if contrast and 0 < span < 160:
        adjusted = ImageOps.autocontrast(image, cutoff=0.1, preserve_tone=True)
        candidate = Image.blend(image, adjusted, contrast)
        adjusted.close()
        image.close()
        image = candidate
        stages.append("mild_tonal_adjustment")
    elif contrast:
        notes.append("CONTRAST_SKIPPED_NO_EVIDENCE")
    return image


def normalize(image, output, settings, source_info, original_metadata):
    if image.mode not in SUPPORTED_MODES or source_info["bits_per_channel"] > 8:
        raise ReviveError(
            "BIT_DEPTH_UNSUPPORTED",
            "Normalization currently supports only 8-bit image channels.",
        )
    retained = original_metadata
    stages, notes = [], []
    oriented = ImageOps.exif_transpose(image)
    if source_info["orientation"] in range(2, 9):
        stages.append("exif_orientation")
    try:
        normalized, icc = to_srgb(oriented, stages, notes)
        exif = oriented.getexif()
        for key in STRUCTURAL_EXIF:
            if key in exif:
                del exif[key]
        # Update EXIF sub-IFD color space and dimension tags when present.
        if 34665 in exif:
            details = exif.get_ifd(34665)
            details[40961] = 1
            details[40962], details[40963] = normalized.size
        dpi = oriented.info.get("dpi")
        if dpi and source_info["orientation"] in (5, 6, 7, 8):
            dpi = tuple(reversed(dpi))
        if source_info["orientation"] in (5, 6, 7, 8) and 282 in exif and 283 in exif:
            exif[282], exif[283] = exif[283], exif[282]
        if "xmp" in retained or "XML:com.adobe.xmp" in getattr(image, "text", {}):
            notes.append("ORIGINAL_XMP_RETAINED_IN_MANIFEST")
        inspection = inspect_pixels(normalized)
        normalized = cleanup(normalized, inspection, settings, stages, notes)
        from .colorization import colorize_if_needed

        normalized, colorization = colorize_if_needed(
            normalized, inspection, settings, stages, notes
        )
        from .super_resolution import upscale_if_needed

        normalized, super_resolution = upscale_if_needed(
            normalized, settings, stages, notes
        )
        # Dimensions must describe the final derivative after all resizing.
        if 34665 in exif:
            details = exif.get_ifd(34665)
            details[40962], details[40963] = normalized.size
        inferred = colorization["applied"]
        stages.append("png_encode")
        metadata = {
            "adapter": "normalize_png",
            "pipeline_version": 1,
            "software_versions": {
                "revive": revive_version,
                "pillow": Image.__version__,
                "littlecms": features.version("littlecms2"),
            },
            "stages": stages,
            "colors_inferred": inferred,
            "colorization": colorization,
            "super_resolution": super_resolution,
            "details_inferred": super_resolution["applied"],
            "restoration_performed": super_resolution["applied"]
            or any(
                s in stages for s in ("mild_median_denoise", "mild_tonal_adjustment")
            ),
            "source": source_info,
            "inspection": inspection,
            "notes": notes,
            "original_metadata": retained,
            "format": "PNG",
            "size": list(normalized.size),
            "mode": normalized.mode,
            "color_space": "sRGB",
        }
        text = PngImagePlugin.PngInfo()
        for key, value in getattr(image, "text", {}).items():
            if key not in ("revive:provenance", "XML:com.adobe.xmp"):
                text.add_itxt(key, value)
        text.add_itxt(
            "revive:provenance",
            json.dumps(
                {
                    "adapter": "normalize_png",
                    "pipeline_version": 1,
                    "stages": stages,
                    "colors_inferred": inferred,
                    **(
                        {"colorization": colorization}
                        if settings.get("colorization_enabled")
                        else {}
                    ),
                    "restoration_performed": metadata["restoration_performed"],
                    **(
                        {
                            "super_resolution": super_resolution,
                            "details_inferred": super_resolution["applied"],
                        }
                        if settings.get("sr_enabled")
                        else {}
                    ),
                },
                sort_keys=True,
            ),
        )
        options = {
            "icc_profile": icc,
            "exif": exif.tobytes(),
            "pnginfo": text,
            "compress_level": 6,
        }
        if (
            dpi
            and len(dpi) == 2
            and all(isinstance(v, (int, float)) and 0 < v <= 1e6 for v in dpi)
        ):
            options["dpi"] = dpi
        try:
            with output.open("xb") as stream:
                normalized.save(stream, format="PNG", **options)
            validate_png(output, normalized, icc)
        finally:
            normalized.close()
        return {"extension": ".png", "metadata": metadata}
    finally:
        oriented.close()


def validate_png(output, expected, icc):
    try:
        with Image.open(output) as check:
            check.verify()
        with Image.open(output) as check:
            check.load()
            if (
                check.format != "PNG"
                or check.size != expected.size
                or check.mode != expected.mode
                or check.info.get("icc_profile") != icc
                or check.getexif().get(274, 1) != 1
                or pixel_hash(check) != pixel_hash(expected)
            ):
                raise ReviveError(
                    "OUTPUT_INVALID",
                    "PNG does not match the planned pixels, profile or geometry.",
                )
    except (OSError, ValueError, SyntaxError) as exc:
        raise ReviveError(
            "OUTPUT_INVALID", "Encoded PNG cannot be fully validated."
        ) from exc
