"""Short-lived decoder/validator. No watcher or database imports here."""

import hashlib
import json
import shutil
import sys
import warnings
from pathlib import Path

from .errors import ReviveError

FORMATS = {"JPEG", "PNG", "TIFF", "BMP", "WEBP"}


def process(request):
    from PIL import Image, UnidentifiedImageError

    source, output = Path(request["snapshot"]), Path(request["output"])
    Image.MAX_IMAGE_PIXELS = request["settings"]["max_input_megapixels"] * 1_000_000
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(source) as image:
                fmt = image.format
                size = image.size
                if fmt not in FORMATS:
                    raise ReviveError(
                        "FORMAT_UNSUPPORTED", "Decoded image format is unsupported."
                    )
                if size[0] * size[1] > Image.MAX_IMAGE_PIXELS:
                    raise ReviveError(
                        "IMAGE_LIMIT", "Image exceeds configured pixel limit."
                    )
                if getattr(image, "n_frames", 1) != 1:
                    raise ReviveError(
                        "FORMAT_UNSUPPORTED",
                        "Animated and multipage images are unsupported.",
                    )
                image.verify()
            with Image.open(source) as image:
                image.load()
                mode = image.mode
        # Deliberately byte-identical: this adapter proves automation, not restoration.
        with source.open("rb") as src, output.open("xb") as dest:
            shutil.copyfileobj(src, dest, 1024 * 1024)
        with Image.open(output) as check:
            check.load()
            if check.size != size or check.format != fmt:
                raise ReviveError("OUTPUT_INVALID", "Copied output failed validation.")
        with output.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        return {
            "hash": digest,
            "extension": {"JPEG": ".jpg", "TIFF": ".tif"}.get(fmt, "." + fmt.lower()),
            "metadata": {
                "adapter": "mock_copy",
                "stages": [],
                "colors_inferred": False,
                "format": fmt,
                "size": size,
                "mode": mode,
                "restoration_performed": False,
            },
        }
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ReviveError("IMAGE_LIMIT", "Image exceeds safe decoding limits.") from exc
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise ReviveError("SOURCE_CORRUPT", "Image cannot be fully decoded.") from exc


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
