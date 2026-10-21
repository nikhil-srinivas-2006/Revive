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


def model_path(app_data):
    return within(app_data, app_data / "models" / MODEL_ID / "pytorch_model.bin")


def model_info():
    return {
        "id": MODEL_ID,
        "license": MODEL_LICENSE,
        "source_revision": SOURCE_REVISION,
        "weight_revision": WEIGHT_REVISION,
        "sha256": WEIGHT_SHA256,
        "bytes": WEIGHT_BYTES,
        "url": WEIGHT_URL,
    }


@contextmanager
def verified_checkpoint(app_data):
    path = model_path(app_data)
    if not path.is_file():
        raise ReviveError(
            "MODEL_MISSING",
            "DDColor Tiny is not installed. Run models install during setup.",
        )
    with source_reader(app_data, path) as stream:
        if os.fstat(stream.fileno()).st_size != WEIGHT_BYTES:
            raise ReviveError(
                "MODEL_HASH_MISMATCH",
                "Model size does not match the pinned checkpoint.",
            )
        if hashlib.file_digest(stream, "sha256").hexdigest() != WEIGHT_SHA256:
            raise ReviveError(
                "MODEL_HASH_MISMATCH",
                "Model checksum does not match the pinned checkpoint.",
            )
        stream.seek(0)
        yield stream


def model_status(app_data):
    status = {**model_info(), "installed": False, "verified": False}
    if model_path(app_data).exists():
        status["installed"] = True
        try:
            with verified_checkpoint(app_data):
                status["verified"] = True
        except ReviveError as exc:
            status["error_code"] = exc.code
    return status


def install_model(app_data):
    """The only weight-downloading operation. Never called from run/doctor/status."""
    target = model_path(app_data)
    if target.exists():
        with verified_checkpoint(app_data):
            return model_info()
    target.parent.mkdir(parents=True, exist_ok=True)
    within(app_data, target)
    stage = None
    try:
        fd, name = tempfile.mkstemp(
            prefix=".ddcolor-", suffix=".part", dir=target.parent
        )
        stage = Path(name)
        digest, total = hashlib.sha256(), 0
        with os.fdopen(fd, "wb") as output, urlopen(WEIGHT_URL, timeout=60) as response:
            if not response.geturl().startswith("https://"):
                raise ReviveError(
                    "MODEL_DOWNLOAD_FAILED",
                    "Model download redirected to an insecure endpoint.",
                )
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > WEIGHT_BYTES:
                    raise ReviveError(
                        "MODEL_HASH_MISMATCH",
                        "Downloaded model exceeds the pinned size.",
                    )
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if total != WEIGHT_BYTES or digest.hexdigest() != WEIGHT_SHA256:
            raise ReviveError(
                "MODEL_HASH_MISMATCH",
                "Downloaded checkpoint failed integrity verification.",
            )
        within(app_data, target)
        try:
            os.link(stage, target)
        except FileExistsError:
            with verified_checkpoint(app_data):
                pass
        return model_info()
    except OSError as exc:
        code = "DISK_LOW" if exc.errno == 28 else "MODEL_DOWNLOAD_FAILED"
        raise ReviveError(
            code, "Model setup failed; check network access and available disk space."
        ) from exc
    finally:
        if stage is not None:
            stage.unlink(missing_ok=True)
