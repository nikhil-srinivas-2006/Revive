"""Offline CPU DDColor adapter with bounded-view inference and strip reconstruction."""

from pathlib import Path

from .errors import ReviveError
from .models import (
    MODEL_ID,
    SOURCE_REVISION,
    WEIGHT_REVISION,
    WEIGHT_SHA256,
    verified_checkpoint,
)


class DDColorAdapter:
    def __init__(self, settings):
        self.settings = settings
        self.measurements = []

    def record_memory(self, stage):
        if self.settings.get("qualification_metrics"):
            from .resources import process_memory_mib

            measurement = {"stage": stage, **process_memory_mib()}
            self.measurements.append(measurement)
            import json

            print(json.dumps({"qualification_memory": measurement}), flush=True)

    def predict(self, image):
        """Return Lab a/b at the fixed inference resolution, never generated luminance."""
        with verified_checkpoint(Path(self.settings["app_data"])) as checkpoint:
            try:
                import cv2
                import numpy as np
                import torch
            except (ImportError, OSError) as exc:
                raise ReviveError(
                    "BACKEND_UNSUPPORTED",
                    "Install the optional CPU colorization dependencies during setup.",
                ) from exc
            from ._vendor.ddcolor.model import DDColor

            self.record_memory("after_imports")

            torch.set_num_threads(self.settings.get("cpu_threads", 2))
            cv2.setNumThreads(self.settings.get("cpu_threads", 2))
            torch.manual_seed(0)
            size = self.settings.get("colorization_input_size", 256)
            try:
                # The constructor's dummy encoder pass only discovers channel
                # widths. A 32-pixel view yields the same parameter shapes without
                # a full-size dummy inference or meta-device scalar incompatibility.
                model = DDColor(
                    encoder_name="convnext-t",
                    input_size=(32, 32),
                    num_output_channels=2,
                    last_norm="Spectral",
                    do_normalize=False,
                    num_queries=100,
                    num_scales=3,
                    dec_layers=9,
                )
                self.record_memory("after_model_construction")
                state = torch.load(checkpoint, map_location="cpu", weights_only=True)
                self.record_memory("after_checkpoint_load")
                if isinstance(state, dict) and "params" in state:
                    state = state["params"]
                model.load_state_dict(state, strict=True, assign=True)
                self.record_memory("after_state_assign")
                del state
                model.eval()
            except (RuntimeError, ValueError, KeyError) as exc:
                if (
                    "DefaultCPUAllocator" in str(exc)
                    or "out of memory" in str(exc).lower()
                ):
                    raise ReviveError(
                        "RAM_LIMIT",
                        "DDColor model loading exceeded its memory budget.",
                        True,
                    ) from exc
                raise ReviveError(
                    "MODEL_INCOMPATIBLE",
                    "Pinned checkpoint does not match the packaged architecture.",
                ) from exc
        from PIL import Image

        view = image.resize((size, size), Image.Resampling.BILINEAR)
        rgb = np.asarray(view, dtype=np.float32) / 255
        view.close()
        gray_lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB)
        gray_lab[:, :, 1:] = 0
        gray_rgb = cv2.cvtColor(gray_lab, cv2.COLOR_LAB2RGB)
        tensor = torch.from_numpy(gray_rgb.transpose(2, 0, 1).copy()).unsqueeze(0)
        try:
            with torch.inference_mode():
                output = model(tensor).cpu().numpy()[0].transpose(1, 2, 0).copy()
                self.record_memory("after_inference")
        except RuntimeError as exc:
            if "DefaultCPUAllocator" in str(exc) or "out of memory" in str(exc).lower():
                raise ReviveError(
                    "RAM_LIMIT",
                    "DDColor exceeded its CPU memory budget; work will retry.",
                    True,
                ) from exc
            raise ReviveError(
                "INFERENCE_FAILED", "DDColor CPU inference failed."
            ) from exc
        if output.shape != (size, size, 2) or not np.isfinite(output).all():
            raise ReviveError("OUTPUT_INVALID", "Colorizer produced invalid chroma.")
        return output

    def colorize(self, image, strength):
        chroma = self.predict(image)
        output, max_error = reconstruct_chroma(image, chroma, strength)
        return output, {
            "model": MODEL_ID,
            "weight_sha256": WEIGHT_SHA256,
            "weight_revision": WEIGHT_REVISION,
            "source_revision": SOURCE_REVISION,
            "input_size": self.settings.get("colorization_input_size", 256),
            "backend": "pytorch_cpu_fp32",
            "max_lab_l_error": max_error,
        }


def reconstruct_chroma(image, chroma, strength):
    """Preserve source Lab L; reduce inferred chroma where clipping changes L.

    Full-resolution floating-point buffers are restricted to 64-row strips.
    The 1.0 Lab L acceptance bound is a numerical guard, not a visual quality score.
    """
    import cv2
    import numpy as np
    from PIL import Image

    if (
        image.mode != "RGB"
        or not 0 <= strength <= 1
        or chroma.ndim != 3
        or chroma.shape[2] != 2
        or not np.isfinite(chroma).all()
    ):
        raise ReviveError(
            "OUTPUT_INVALID", "Invalid input to luminance-preserving reconstruction."
        )
    chroma = np.clip(chroma.astype(np.float32), -110, 110) * strength
    out = Image.new("RGB", image.size)
    x = (np.arange(image.width, dtype=np.float32) + 0.5) * chroma.shape[
        1
    ] / image.width - 0.5
    largest_error = 0.0
    try:
        for top in range(0, image.height, 64):
            bottom = min(image.height, top + 64)
            with image.crop((0, top, image.width, bottom)) as strip:
                rgb = np.asarray(strip, dtype=np.float32) / 255
            original_l = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB)[:, :, 0]
            y = (np.arange(top, bottom, dtype=np.float32) + 0.5) * chroma.shape[
                0
            ] / image.height - 0.5
            map_x = np.broadcast_to(x[None, :], original_l.shape).copy()
            map_y = np.broadcast_to(y[:, None], original_l.shape).copy()
            ab = cv2.remap(
                chroma, map_x, map_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE
            )
            reconstructed = np.zeros((*original_l.shape, 3), dtype=np.uint8)
            pending = np.ones(original_l.shape, dtype=bool)
            for scale in (1.0, 0.75, 0.5, 0.25, 0.0):
                lab = np.concatenate((original_l[:, :, None], ab * scale), axis=2)
                encoded = np.clip(
                    np.rint(cv2.cvtColor(lab, cv2.COLOR_LAB2RGB) * 255), 0, 255
                ).astype(np.uint8)
                actual_l = cv2.cvtColor(
                    encoded.astype(np.float32) / 255, cv2.COLOR_RGB2LAB
                )[:, :, 0]
                error = np.abs(actual_l - original_l)
                accepted = pending & (error <= 1.0)
                if accepted.any():
                    reconstructed[accepted] = encoded[accepted]
                    largest_error = max(largest_error, float(error[accepted].max()))
                pending &= ~accepted
                if not pending.any():
                    break
            if pending.any():
                raise ReviveError(
                    "QUALITY_REGRESSION",
                    "Colorized output exceeded the luminance error bound.",
                )
            tile = Image.fromarray(reconstructed)
            out.paste(tile, (0, top))
            tile.close()
        return out, round(largest_error, 6)
    except BaseException:
        out.close()
        raise
