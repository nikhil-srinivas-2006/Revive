"""Output geometry and admission policy, before loading an optional SR model."""

from .errors import ReviveError


def target_size(size, target_long_edge=4096):
    """At most 2x, toward the target; never shrink an existing larger image."""
    width, height = size
    long_edge = max(size)
    if long_edge >= target_long_edge:
        return size
    if long_edge * 2 <= target_long_edge:
        return width * 2, height * 2
    return tuple(
        max(1, (value * target_long_edge + long_edge // 2) // long_edge)
        for value in size
    )


def requirements(size, settings):
    """Conservative whole-worker estimates, including native 2x intermediate."""
    target = target_size(size, settings.get("sr_target_long_edge", 4096))
    source_pixels = size[0] * size[1]
    output_pixels = target[0] * target[1]
    if output_pixels > settings.get("max_output_megapixels", 64) * 1_000_000:
        raise ReviveError(
            "IMAGE_LIMIT", "Planned output exceeds the output pixel limit."
        )
    if target == size:
        return target, 0, 0, 0
    native_pixels = ((size[0] + 7) // 8 * 16) * ((size[1] + 7) // 8 * 16)
    image_bytes = source_pixels * 64 + native_pixels * 8 + output_pixels * 12
    tile = settings.get("sr_tile_size", 64)
    # Both options keep the model's largest inference view at or below 192x192.
    # A separate bounded probe checks the larger view before qualification.
    model_mib = {64: 512, 128: 512}[tile]
    physical = model_mib + (image_bytes + 1024**2 - 1) // 1024**2
    commit = physical + 768
    # Encoded PNG upper allowance, plus scratch space. Source snapshot is separate.
    disk = output_pixels * 5 + 4 * 1024**2
    return target, physical, commit, disk


def upscale_if_needed(image, settings, stages, notes):
    if not settings.get("sr_enabled", False):
        return image, {"enabled": False, "applied": False}
    if image.mode != "RGB":
        notes.append("SUPER_RESOLUTION_SKIPPED_TRANSPARENCY")
        return image, {"enabled": True, "applied": False, "reason": "TRANSPARENCY"}
    target, physical, commit, _ = requirements(image.size, settings)
    if target == image.size:
        reason = "ALREADY_AT_TARGET"
        notes.append(f"SUPER_RESOLUTION_SKIPPED_{reason}")
        return image, {"enabled": True, "applied": False, "reason": reason}
    if (
        settings["worker_memory_mib"] < physical
        or settings.get("worker_commit_mib", settings["worker_memory_mib"]) < commit
    ):
        raise ReviveError(
            "RAM_ADMISSION",
            "Worker budget is below the super-resolution safety estimate.",
            True,
        )
    from .swinir import SwinIRAdapter

    result, details = SwinIRAdapter(settings).upscale(image, target)
    image.close()
    stages.append("swinir_lightweight_x2")
    notes.append("AI_UPSCALED_DETAILS_ARE_INFERRED")
    return result, {"enabled": True, "applied": True, **details}
