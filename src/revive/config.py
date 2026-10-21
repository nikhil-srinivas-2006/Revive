import hashlib
import json
import os
import tomllib
from dataclasses import asdict, dataclass
from importlib.metadata import version
from pathlib import Path

from .errors import ReviveError
from .paths import check_plain_path


@dataclass(frozen=True)
class Config:
    input: Path
    output: Path
    app_data: Path
    recursive: bool = True
    process_existing: bool = True
    quiet_seconds: float = 10
    stable_samples: int = 3
    sample_seconds: float = 5
    reconcile_seconds: float = 300
    adapter: str = "mock_copy"
    denoise_strength: float = 0
    contrast_strength: float = 0
    max_source_mib: int = 512
    max_input_megapixels: int = 40
    max_pending_jobs: int = 1000
    min_free_disk_mib: int = 512
    worker_timeout_seconds: float = 300
    worker_memory_mib: int = 0
    max_attempts: int = 4
    delays_seconds: tuple[int, ...] = (60, 300, 1800)

    def validate(self):
        roots = (self.input, self.output, self.app_data)
        for root in roots:
            check_plain_path(root)
            if not root.is_absolute() or root == Path(root.anchor):
                raise ReviveError(
                    "CONFIG_INVALID", "Use absolute non-root directories."
                )
            if root.exists() and not root.is_dir():
                raise ReviveError("CONFIG_INVALID", "A configured directory is a file.")
        for i, a in enumerate(roots):
            for b in roots[i + 1 :]:
                if a.is_relative_to(b) or b.is_relative_to(a):
                    raise ReviveError(
                        "CONFIG_INVALID", "Configured directories must not overlap."
                    )
        if self.adapter not in ("mock_copy", "normalize_png"):
            raise ReviveError(
                "BACKEND_UNSUPPORTED",
                "Supported adapters are mock_copy and normalize_png.",
            )
        for name in ("denoise_strength", "contrast_strength"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not 0 <= value <= 0.25:
                raise ReviveError(
                    "CONFIG_INVALID", f"{name} must be between 0 and 0.25."
                )
            if self.adapter == "mock_copy" and value:
                raise ReviveError(
                    "CONFIG_INVALID", "Cleanup settings require normalize_png."
                )
        if type(self.worker_memory_mib) is not int or self.worker_memory_mib < 0:
            raise ReviveError(
                "CONFIG_INVALID",
                "worker_memory_mib must be a nonnegative integer (0 = auto).",
            )
        for name in ("recursive", "process_existing"):
            if type(getattr(self, name)) is not bool:
                raise ReviveError("CONFIG_INVALID", f"{name} must be boolean.")
        for name in (
            "stable_samples",
            "max_source_mib",
            "max_input_megapixels",
            "max_pending_jobs",
            "min_free_disk_mib",
            "max_attempts",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ReviveError(
                    "CONFIG_INVALID", f"{name} must be a positive integer."
                )
        if self.stable_samples < 2:
            raise ReviveError(
                "CONFIG_INVALID", "At least two stability samples are required."
            )
        for name in (
            "quiet_seconds",
            "sample_seconds",
            "reconcile_seconds",
            "worker_timeout_seconds",
        ):
            value = getattr(self, name)
            if type(value) not in (int, float) or not 0 < value < 86401:
                raise ReviveError(
                    "CONFIG_INVALID", f"{name} must be between 0 and 86400 seconds."
                )
        if (
            not isinstance(self.delays_seconds, (tuple, list))
            or not self.delays_seconds
            or any(type(x) is not int or x < 1 for x in self.delays_seconds)
        ):
            raise ReviveError(
                "CONFIG_INVALID", "Retry delays must be positive integers."
            )

    @property
    def fingerprint(self):
        # Include anything that can affect the emitted artifact, not scheduling.
        policy = {"adapter": self.adapter, "protocol": 1}
        if self.adapter == "normalize_png":
            policy.update(
                protocol=1,
                denoise_strength=self.denoise_strength,
                contrast_strength=self.contrast_strength,
                pillow_version=version("Pillow"),
            )
        return hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()

    def worker_settings(self):
        from .resources import worker_budget_mib

        return {
            "max_input_megapixels": self.max_input_megapixels,
            "adapter": self.adapter,
            "denoise_strength": self.denoise_strength,
            "contrast_strength": self.contrast_strength,
            "worker_memory_mib": worker_budget_mib(self.worker_memory_mib),
            "min_free_disk_mib": self.min_free_disk_mib,
        }


def load_config(path: Path) -> Config:
    try:
        with path.open("rb") as stream:
            raw = tomllib.load(stream)
        groups = {
            "paths": {"input", "output", "app_data"},
            "watch": {
                "recursive",
                "process_existing",
                "quiet_seconds",
                "stable_samples",
                "sample_seconds",
                "reconcile_seconds",
            },
            "pipeline": {"adapter", "denoise_strength", "contrast_strength"},
            "resources": {
                "max_source_mib",
                "max_input_megapixels",
                "max_pending_jobs",
                "min_free_disk_mib",
                "worker_timeout_seconds",
                "worker_memory_mib",
            },
            "retry": {"max_attempts", "delays_seconds"},
        }
        if type(raw.get("schema_version")) is not int or raw["schema_version"] != 1:
            raise ValueError("Unsupported schema_version")
        if set(raw) - (set(groups) | {"schema_version"}):
            raise ValueError("Unknown configuration section")
        values = {}
        for group, allowed in groups.items():
            section = raw.get(group, {})
            if not isinstance(section, dict) or set(section) - allowed:
                raise ValueError(f"Unknown setting in {group}")
            values.update(section)
        for name in groups["paths"]:
            value = values[name]
            if not isinstance(value, str) or not value:
                raise ValueError("Paths must be nonempty strings")
            expanded = Path(os.path.expandvars(value))
            check_plain_path(expanded)
            values[name] = expanded.absolute()
            if not expanded.is_absolute():
                raise ValueError("Paths must be absolute")
        if "delays_seconds" in values:
            values["delays_seconds"] = tuple(values["delays_seconds"])
        config = Config(**values)
        config.validate()
        return config
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise ReviveError(
            "CONFIG_INVALID", "Invalid or unreadable TOML configuration."
        ) from exc


def describe(config: Config):
    return {k: str(v) if isinstance(v, Path) else v for k, v in asdict(config).items()}
