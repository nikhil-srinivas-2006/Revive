"""Conservative colorization policy; heavy modules load only for eligible inputs."""

from PIL import ImageChops

from .errors import ReviveError


def eligibility(image, inspection, include_sepia=False):
    if image.mode != "RGB":
        return False, "COLORIZATION_SKIPPED_TRANSPARENCY"
    classification = inspection.get("color_class")
    if classification == "sepia_candidate":
        return (
            (True, "SEPIA_EXPLICITLY_ENABLED")
            if include_sepia
            else (False, "COLORIZATION_SKIPPED_SEPIA_UNCERTAIN")
        )
    if classification != "grayscale_candidate":
        return False, "COLORIZATION_SKIPPED_COLOR_OR_UNCERTAIN"
    # A thumbnail can hide small colored objects. Check every full-resolution pixel
    # before allowing an automatic grayscale decision.
    r, g, b = image.split()
    differences = [
        ImageChops.difference(r, g),
        ImageChops.difference(r, b),
        ImageChops.difference(g, b),
    ]
    combined = ImageChops.lighter(
        ImageChops.lighter(differences[0], differences[1]), differences[2]
    )
    histogram = combined.histogram()
    count = sum(histogram)
    safe = sum(histogram[5:]) == 0 and sum(histogram[:3]) / count >= 0.999
    for channel in (r, g, b, combined, *differences):
        channel.close()
    return (
        (True, "GRAYSCALE_FULL_RESOLUTION_CHECK")
        if safe
        else (False, "COLORIZATION_SKIPPED_COLOR_OR_UNCERTAIN")
    )


def colorize_if_needed(image, inspection, settings, stages, notes):
    if not settings.get("colorization_enabled", False):
        return image, {"enabled": False, "applied": False}
    eligible, reason = eligibility(
        image, inspection, settings.get("colorize_sepia", False)
    )
    if not eligible:
        notes.append(reason)
        return image, {"enabled": True, "applied": False, "reason": reason}
    # No inference can occur at zero strength, and no model is needed.
    strength = settings.get("colorization_strength", 0.85)
    if strength == 0:
        notes.append("COLORIZATION_SKIPPED_ZERO_STRENGTH")
        return image, {"enabled": True, "applied": False, "reason": "ZERO_STRENGTH"}
    from .ddcolor import DDColorAdapter

    size = settings.get("colorization_input_size", 256)
    required = (
        896 if size == 256 else 1792
    ) + image.width * image.height * 24 // 1024**2
    if settings["worker_memory_mib"] < required:
        raise ReviveError(
            "RAM_ADMISSION",
            "Available worker budget is below the conservative DDColor admission estimate.",
            True,
        )
    commit_required = (
        1792 if size == 256 else 3072
    ) + image.width * image.height * 24 // 1024**2
    if (
        settings.get("worker_commit_mib", settings["worker_memory_mib"])
        < commit_required
    ):
        raise ReviveError(
            "RAM_ADMISSION",
            "Committed-memory budget is below the DDColor safety estimate.",
            True,
        )
    adapter = DDColorAdapter(settings)
    result, details = adapter.colorize(image, strength)
    image.close()
    stages.append("ddcolor_tiny_colorization")
    notes.append("COLORS_ARE_AI_INFERRED_NOT_HISTORICALLY_VERIFIED")
    return result, {
        "enabled": True,
        "applied": True,
        "eligibility": reason,
        "strength": strength,
        **details,
    }
