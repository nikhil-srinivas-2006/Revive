"""Offline SwinIR x2 CPU inference with window-aligned tiles and context crop."""

from functools import partial
from pathlib import Path

from .errors import ReviveError
from .models import verified_checkpoint
from .swinir_model import MODEL_ID, SOURCE_REVISION, WEIGHT_SHA256

HALO = 32


def tiled_upscale(image, predict, tile_size=64):
    """Never create a full-image float tensor or a full-image weight accumulator.

    Every tile origin aligns to the global 8-pixel attention window grid. Context
    is cropped, not averaged. Finite context is approximate, not full-frame parity.
    """
    import numpy as np
    from PIL import Image

    width, height = image.size
    array = np.asarray(image)
    array = np.pad(
        array, ((0, (-height) % 8), (0, (-width) % 8), (0, 0)), mode="symmetric"
    )
    padded = Image.fromarray(array)
    del array
    canvas = Image.new("RGB", (padded.width * 2, padded.height * 2))
    count = 0
    try:
        for top in range(0, padded.height, tile_size):
            for left in range(0, padded.width, tile_size):
                right = min(left + tile_size, padded.width)
                bottom = min(top + tile_size, padded.height)
                x0, y0 = max(0, left - HALO), max(0, top - HALO)
                x1, y1 = (
                    min(padded.width, right + HALO),
                    min(padded.height, bottom + HALO),
                )
                tile = padded.crop((x0, y0, x1, y1))
                try:
                    values = predict(tile)
                    if (
                        values.shape != (tile.height * 2, tile.width * 2, 3)
                        or not np.isfinite(values).all()
                    ):
                        raise ReviveError(
                            "OUTPUT_INVALID",
                            "Super-resolution tile has invalid shape or values.",
                        )
                    crop = values[
                        (top - y0) * 2 : (bottom - y0) * 2,
                        (left - x0) * 2 : (right - x0) * 2,
                    ]
                    core = Image.fromarray(
                        np.rint(np.clip(crop, 0, 1) * 255).astype(np.uint8)
                    )
                    canvas.paste(core, (left * 2, top * 2))
                    core.close()
                    del values, crop
                finally:
                    tile.close()
                count += 1
        result = canvas.crop((0, 0, width * 2, height * 2))
        return result, count
    finally:
        padded.close()
        canvas.close()


class SwinIRAdapter:
    def __init__(self, settings):
        self.settings = settings

    def load(self):
        # Missing/corrupt model fails before importing torch.
        with verified_checkpoint(Path(self.settings["app_data"]), MODEL_ID) as stream:
            try:
                import numpy as np  # noqa: F401
                import torch
            except (ImportError, OSError) as exc:
                raise ReviveError(
                    "BACKEND_UNSUPPORTED",
                    "Install the CPU super-resolution extra during setup.",
                ) from exc
            from ._vendor.swinir.network_swinir import SwinIR

            torch.set_num_threads(self.settings.get("cpu_threads", 2))
            torch.manual_seed(0)
            try:
                model = SwinIR(
                    upscale=2,
                    in_chans=3,
                    img_size=64,
                    window_size=8,
                    img_range=1.0,
                    depths=[6, 6, 6, 6],
                    embed_dim=60,
                    num_heads=[6, 6, 6, 6],
                    mlp_ratio=2,
                    upsampler="pixelshuffledirect",
                    resi_connection="1conv",
                )
                state = torch.load(stream, map_location="cpu", weights_only=True)
                model.load_state_dict(state["params"], strict=True, assign=True)
                del state
                model.eval()
                return model
            except (RuntimeError, ValueError, KeyError) as exc:
                self.failure(exc, "MODEL_INCOMPATIBLE")

    @staticmethod
    def failure(exc, fallback):
        if "out of memory" in str(exc).lower() or "DefaultCPUAllocator" in str(exc):
            raise ReviveError(
                "RAM_LIMIT", "SwinIR exceeded the worker's memory budget.", True
            ) from exc
        raise ReviveError(
            fallback, "SwinIR model loading or CPU inference failed."
        ) from exc

    @staticmethod
    def predict(model, image):
        import numpy as np
        import torch

        array = np.array(image, dtype=np.float32) / 255
        tensor = torch.from_numpy(array.transpose(2, 0, 1).copy()).unsqueeze(0)
        with torch.inference_mode():
            result = model(tensor)
        return result[0].permute(1, 2, 0).contiguous().numpy()

    def upscale(self, image, target):
        from PIL import Image, ImageChops

        model = self.load()
        try:
            result, count = tiled_upscale(
                image,
                partial(self.predict, model),
                self.settings.get("sr_tile_size", 64),
            )
        except RuntimeError as exc:
            self.failure(exc, "OUTPUT_INVALID")
        finally:
            del model
        # RGB reconstruction must not introduce inferred color into exact neutral
        # originals. A full-pixel test retains even tiny existing colored marks.
        channels = image.split()
        differences = [
            ImageChops.difference(channels[0], channels[1]),
            ImageChops.difference(channels[1], channels[2]),
        ]
        neutral = all(difference.getbbox() is None for difference in differences)
        for buffer in (*channels, *differences):
            buffer.close()
        if neutral:
            gray = result.convert("L")
            result.close()
            result = gray.convert("RGB")
            gray.close()
        if result.size != target:
            resized = result.resize(target, Image.Resampling.LANCZOS)
            result.close()
            result = resized
        return result, {
            "model": MODEL_ID,
            "source_revision": SOURCE_REVISION,
            "weight_sha256": WEIGHT_SHA256,
            "native_scale": 2,
            "input_size": list(image.size),
            "output_size": list(target),
            "tile_size": self.settings.get("sr_tile_size", 64),
            "context_pixels": HALO,
            "tiles": count,
            "tiling_is_approximate": True,
            "neutral_channels_preserved": neutral,
            "torch_version": __import__("torch").__version__,
        }
