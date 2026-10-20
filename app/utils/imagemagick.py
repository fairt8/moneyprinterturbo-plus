"""Locate an optional ImageMagick binary and export it for MoviePy.

MoviePy 2 renders subtitle TextClips with Pillow, so a missing ImageMagick
install does not block video generation. Older MoviePy releases and a few
font utilities still look for ``IMAGEMAGICK_BINARY``. When the binary exists
we publish it; when a configured path is missing we say so at startup.
"""

from __future__ import annotations

import os
import shutil

from loguru import logger


def configure_imagemagick(app_config: dict) -> str:
    configured = str(
        app_config.get("imagemagick_path")
        or os.getenv("IMAGEMAGICK_BINARY")
        or ""
    ).strip()
    candidates = [configured, "magick", "convert"]
    resolved = ""
    for candidate in candidates:
        if not candidate:
            continue
        if os.path.isfile(candidate):
            resolved = candidate
            break
        found = shutil.which(candidate)
        if found:
            resolved = found
            break

    if configured and not resolved:
        logger.warning(
            "app.imagemagick_path is set but is not an executable: "
            f"{configured}. Subtitles still render through Pillow. Install "
            "ImageMagick or fix IMAGEMAGICK_PATH if a font tool asks for it."
        )
        return ""

    if resolved:
        os.environ["IMAGEMAGICK_BINARY"] = resolved
        logger.info(f"ImageMagick available at {resolved}")
        return resolved

    logger.info(
        "ImageMagick was not found. Subtitles render with Pillow, so this "
        "is optional unless a tool reports an ImageMagick error."
    )
    return ""
