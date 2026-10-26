"""Pinned model catalog, explicit setup, and verified offline checkpoint access."""

import hashlib
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from urllib.request import urlopen

from .errors import ReviveError
from .paths import source_reader, within

MODEL_ID = "ddcolor_tiny"
SOURCE_REVISION = "2adb63f2656ac41cbdf7b894cddd94121a3faf13"
WEIGHT_REVISION = "cf9fd99c1d7472689ec7413441c1b799a51866a3"
WEIGHT_SHA256 = "8a1277bc90a1bfbb6d2d83933a9a6bc821931879ca93e26e4fcec12165d41fce"
WEIGHT_BYTES = 220372845
WEIGHT_URL = f"https://huggingface.co/piddnad/ddcolor_paper_tiny/resolve/{WEIGHT_REVISION}/pytorch_model.bin"
MODEL_LICENSE = "Apache-2.0"


def model_path(app_data, model_id=MODEL_ID):
    info = model_info(model_id)
    return within(app_data, app_data / "models" / model_id / info["filename"])


def model_info(model_id=MODEL_ID):
    if model_id in ("ddcolor_modelscope", "realesrgan_x2plus"):
        from .quality_models import DDCOLOR, REALESRGAN

        return dict(DDCOLOR if model_id == "ddcolor_modelscope" else REALESRGAN)
    if model_id == "swinir_lightweight_x2":
        from .swinir_model import model_info as swinir_info

        return swinir_info()
    if model_id != MODEL_ID:
        raise ReviveError("MODEL_UNSUPPORTED", "Unknown model identifier.")
    return {
        "id": MODEL_ID,
        "license": MODEL_LICENSE,
        "source_revision": SOURCE_REVISION,
        "weight_revision": WEIGHT_REVISION,
        "sha256": WEIGHT_SHA256,
        "bytes": WEIGHT_BYTES,
        "url": WEIGHT_URL,
        "filename": "pytorch_model.bin",
    }


@contextmanager
def verified_checkpoint(app_data, model_id=MODEL_ID):
    info = model_info(model_id)
    path = model_path(app_data, model_id)
    if not path.is_file():
        raise ReviveError(
            "MODEL_MISSING",
            f"{model_id} is not installed. Run models install during setup.",
        )
    with source_reader(app_data, path) as stream:
        if os.fstat(stream.fileno()).st_size != info["bytes"]:
            raise ReviveError(
                "MODEL_HASH_MISMATCH",
                "Model size does not match the pinned checkpoint.",
            )
        if hashlib.file_digest(stream, "sha256").hexdigest() != info["sha256"]:
            raise ReviveError(
                "MODEL_HASH_MISMATCH",
                "Model checksum does not match the pinned checkpoint.",
            )
        stream.seek(0)
        yield stream


def model_status(app_data, model_id=MODEL_ID):
    status = {**model_info(model_id), "installed": False, "verified": False}
    if model_path(app_data, model_id).exists():
        status["installed"] = True
        try:
            with verified_checkpoint(app_data, model_id):
                status["verified"] = True
        except ReviveError as exc:
            status["error_code"] = exc.code
    return status


def install_model(app_data, model_id=MODEL_ID):
    """The only weight-downloading operation. Never called from run/doctor/status."""
    info = model_info(model_id)
    target = model_path(app_data, model_id)
    if target.exists():
        with verified_checkpoint(app_data, model_id):
            return info
    target.parent.mkdir(parents=True, exist_ok=True)
    within(app_data, target)
    stage = None
    try:
        fd, name = tempfile.mkstemp(
            prefix=f".{model_id}-", suffix=".part", dir=target.parent
        )
        stage = Path(name)
        digest, total = hashlib.sha256(), 0
        with (
            os.fdopen(fd, "wb") as output,
            urlopen(info["url"], timeout=60) as response,
        ):
            if not response.geturl().startswith("https://"):
                raise ReviveError(
                    "MODEL_DOWNLOAD_FAILED",
                    "Model download redirected to an insecure endpoint.",
                )
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > info["bytes"]:
                    raise ReviveError(
                        "MODEL_HASH_MISMATCH",
                        "Downloaded model exceeds the pinned size.",
                    )
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if total != info["bytes"] or digest.hexdigest() != info["sha256"]:
            raise ReviveError(
                "MODEL_HASH_MISMATCH",
                "Downloaded checkpoint failed integrity verification.",
            )
        within(app_data, target)
        try:
            os.link(stage, target)
        except FileExistsError:
            with verified_checkpoint(app_data, model_id):
                pass
        return info
    except OSError as exc:
        code = "DISK_LOW" if exc.errno == 28 else "MODEL_DOWNLOAD_FAILED"
        raise ReviveError(
            code, "Model setup failed; check network access and available disk space."
        ) from exc
    finally:
        if stage is not None:
            stage.unlink(missing_ok=True)
