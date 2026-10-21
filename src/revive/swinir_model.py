"""Lightweight SwinIR x2 catalog entry; no ML imports or network operations."""

MODEL_ID = "swinir_lightweight_x2"
SOURCE_REVISION = "6545850fbf8df298df73d81f3e8cba638787c8bd"
WEIGHT_SHA256 = "193b229909ca89cd8b55de9c9e7fce146ae759d59dfcd78d8feb9dd1d6fa0fd7"
WEIGHT_BYTES = 17147989
WEIGHT_URL = "https://github.com/JingyunLiang/SwinIR/releases/download/v0.0/002_lightweightSR_DIV2K_s64w8_SwinIR-S_x2.pth"


def model_info():
    return {
        "id": MODEL_ID,
        "license": "Apache-2.0",
        "source_revision": SOURCE_REVISION,
        "weight_revision": "v0.0",
        "sha256": WEIGHT_SHA256,
        "bytes": WEIGHT_BYTES,
        "url": WEIGHT_URL,
        "filename": "model.pth",
        "digest_origin": "SHA256 pinned from author's HTTPS release; no upstream published digest",
    }
