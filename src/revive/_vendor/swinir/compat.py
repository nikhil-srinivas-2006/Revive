"""Native helpers replacing SwinIR's optional timm dependency for inference."""

from torch import nn
from torch.nn.init import trunc_normal_  # noqa: F401


def to_2tuple(value):
    return value if isinstance(value, tuple) else (value, value)


class DropPath(nn.Module):
    """Evaluation is identity. Stochastic-depth training is unsupported."""

    def __init__(self, drop_prob=0.0):
        super().__init__()
        self.drop_prob = drop_prob

    def forward(self, value):
        if self.training and self.drop_prob:
            raise RuntimeError("Packaged SwinIR supports evaluation only")
        return value
