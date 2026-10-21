"""Short-lived decoder/adapter process. No watcher or database imports here."""

import hashlib
import json
import shutil
import sys
import warnings
from pathlib import Path

from .errors import ReviveError

FORMATS = {"JPEG", "PNG", "TIFF", "BMP", "WEBP"}


def _header(image, source):
    orientation = image.getexif().get(274, 1)
    if type(orientation) is not int or orientation not in range(1, 9):
        raise ReviveError("ORIENTATION_INVALID", "EXIF orientation is invalid.")
    bits = getattr(image, "bits", 8)
    if image.format == "PNG":
        with source.open("rb") as stream:
            header = stream.read(25)
            bits = header[24]
    elif image.format == "TIFF":
        values = image.tag_v2.get(258, (8,))
        bits = max(values) if isinstance(values, tuple) else values
    return {
        "format": image.format,
        "size": list(image.size),
        "mode": image.mode,
        "orientation": orientation,
        "has_alpha": image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info,
        "has_icc": bool(image.info.get("icc_profile")),
        "bits_per_channel": bits,
    }


def process(request):
    from PIL import Image, UnidentifiedImageError

    source, output = Path(request["snapshot"]), Path(request["output"])
    settings = request["settings"]
    adapter = settings.get("adapter", "mock_copy")
    if adapter not in ("mock_copy", "normalize_png"):
        raise ReviveError(
            "BACKEND_UNSUPPORTED", "Requested worker adapter is unavailable."
        )
    Image.MAX_IMAGE_PIXELS = settings["max_input_megapixels"] * 1_000_000
    # Estimates are admission heuristics, not guarantees of backend allocation.
    memory_limit = settings.get("worker_memory_mib", 512) * 1024**2
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(source) as image:
                fmt = image.format
                if fmt not in FORMATS:
                    raise ReviveError(
                        "FORMAT_UNSUPPORTED", "Decoded image format is unsupported."
                    )
                pixels = image.width * image.height
                if pixels > Image.MAX_IMAGE_PIXELS:
                    raise ReviveError(
                        "IMAGE_LIMIT", "Image exceeds configured pixel limit."
                    )
                estimate = (
                    pixels * (64 if adapter == "normalize_png" else 8) + 64 * 1024**2
                )
                if estimate > memory_limit:
                    raise ReviveError(
                        "RAM_ADMISSION",
                        "Image exceeds the current worker memory budget; retry with more available RAM or adjust the budget.",
                        True,
                    )
                if adapter == "normalize_png":
                    required_disk = (
                        pixels * 4
                        + 4 * 1024**2
                        + settings.get("min_free_disk_mib", 0) * 1024**2
                    )
                    if shutil.disk_usage(output.parent).free < required_disk:
                        raise ReviveError(
                            "DISK_LOW",
                            "Insufficient working disk space for a PNG derivative.",
                            True,
                        )
                if getattr(image, "n_frames", 1) != 1:
                    raise ReviveError(
                        "FORMAT_UNSUPPORTED",
                        "Animated and multipage images are unsupported.",
                    )
                image.verify()
            with Image.open(source) as image:
                source_info = _header(image, source)
                original_metadata = None
                if adapter == "normalize_png":
                    from .pipeline import retained_metadata

                    original_metadata = retained_metadata(image)
                image.load()
                if adapter == "normalize_png":
                    from .pipeline import normalize, retained_metadata

                    complete_metadata = retained_metadata(image)
                    # TIFF decoders may normalize orientation while loading.
                    complete_metadata.update(original_metadata)
                    response = normalize(
                        image, output, settings, source_info, complete_metadata
                    )
                else:
                    response = _mock_copy(source, output, image)
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ReviveError("IMAGE_LIMIT", "Image exceeds safe decoding limits.") from exc
    except (UnidentifiedImageError, SyntaxError, ValueError) as exc:
        raise ReviveError("SOURCE_CORRUPT", "Image cannot be fully decoded.") from exc
    except OSError as exc:
        if exc.errno is not None:
            code = (
                "DISK_LOW"
                if exc.errno == 28
                else ("ACCESS_DENIED" if exc.errno == 13 else "IO_ERROR")
            )
            raise ReviveError(
                code, "Worker filesystem operation failed; work will retry.", True
            ) from exc
        raise ReviveError("SOURCE_CORRUPT", "Image cannot be fully decoded.") from exc
    except MemoryError as exc:
        raise ReviveError(
            "RAM_LIMIT", "Worker memory allocation failed; work will retry.", True
        ) from exc
    with output.open("rb") as stream:
        response["hash"] = hashlib.file_digest(stream, "sha256").hexdigest()
    return response


def _mock_copy(source, output, image):
    from PIL import Image

    fmt, size, mode = image.format, image.size, image.mode
    with source.open("rb") as src, output.open("xb") as dest:
        shutil.copyfileobj(src, dest, 1024 * 1024)
    with Image.open(output) as check:
        check.load()
        if check.size != size or check.format != fmt:
            raise ReviveError("OUTPUT_INVALID", "Copied output failed validation.")
    return {
        "extension": {"JPEG": ".jpg", "TIFF": ".tif"}.get(fmt, "." + fmt.lower()),
        "metadata": {
            "adapter": "mock_copy",
            "stages": [],
            "colors_inferred": False,
            "format": fmt,
            "size": list(size),
            "mode": mode,
            "restoration_performed": False,
        },
    }


def main():
    try:
        result = {"ok": True, **process(json.load(sys.stdin))}
    except ReviveError as exc:
        result = {
            "ok": False,
            "code": exc.code,
            "message": str(exc),
            "retryable": exc.retryable,
        }
    except Exception:  # noqa: BLE001 -- process boundary: never leak private paths on stdout
        result = {
            "ok": False,
            "code": "WORKER_ERROR",
            "message": "Unexpected processing failure.",
            "retryable": False,
        }
    print(json.dumps(result))


if __name__ == "__main__":
    main()
