"""Offline CPU Real-ESRGAN x2plus, sharing bounded tile publication with SwinIR."""

from pathlib import Path

from .errors import ReviveError
from .models import verified_checkpoint
from .swinir import SwinIRAdapter


class RealESRGANAdapter(SwinIRAdapter):
    model_id = "realesrgan_x2plus"
    alignment = 2  # RRDB x2 first pixel-unshuffles into four channels.
    pre_pad = 10  # Match the upstream RealESRGANer's reflected right/bottom pad.

    def load(self):
        with verified_checkpoint(
            Path(self.settings["app_data"]), self.model_id
        ) as stream:
            try:
                import torch
            except (ImportError, OSError) as exc:
                raise ReviveError(
                    "BACKEND_UNSUPPORTED",
                    "Install the CPU super-resolution extra during setup.",
                ) from exc
            from ._vendor.realesrgan.rrdbnet_arch import RRDBNet

            torch.set_num_threads(self.settings.get("cpu_threads", 2))
            torch.manual_seed(0)
            try:
                model = RRDBNet(
                    num_in_ch=3,
                    num_out_ch=3,
                    scale=2,
                    num_feat=64,
                    num_block=23,
                    num_grow_ch=32,
                )
                state = torch.load(stream, map_location="cpu", weights_only=True)
                # The author's release contains the EMA inference checkpoint.
                model.load_state_dict(state["params_ema"], strict=True, assign=True)
                del state
                model.eval()
                return model
            except (RuntimeError, ValueError, KeyError) as exc:
                self.failure(exc, "MODEL_INCOMPATIBLE")

    @staticmethod
    def failure(exc, fallback):
        if "out of memory" in str(exc).lower() or "DefaultCPUAllocator" in str(exc):
            raise ReviveError(
                "RAM_LIMIT", "Real-ESRGAN exceeded the worker's memory budget.", True
            ) from exc
        raise ReviveError(
            fallback, "Real-ESRGAN model loading or CPU inference failed."
        ) from exc
