"""Turn raw pipeline exceptions into a sentence a person can act on."""

from __future__ import annotations


def explain_pipeline_error(exc: BaseException) -> str:
    """Keep the exception type and add a short fix hint when we recognize it."""

    text = f"{type(exc).__name__}: {exc}"
    lowered = text.lower()
    hints: list[str] = []

    if "ffmpeg" in lowered or "imageio_ffmpeg" in lowered:
        hints.append(
            "Install ffmpeg or set app.ffmpeg_path (or FFMPEG_PATH) to the "
            "ffmpeg executable."
        )
    if any(token in lowered for token in ("imagemagick", "wand", "magick")):
        hints.append(
            "Install ImageMagick or set app.imagemagick_path. This build draws "
            "subtitles with Pillow, so also confirm the selected font file exists."
        )
    if "textclip" in lowered or ("font" in lowered and "no such file" in lowered):
        hints.append(
            "The subtitle font could not be loaded. Choose a font from "
            "resource/fonts, or install a system font such as DejaVu."
        )
    if any(token in lowered for token in ("api key", "unauthorized", "invalid_api_key", "401")):
        hints.append(
            "Check the provider API key in config.toml or the matching "
            "environment variable."
        )
    if any(token in lowered for token in ("connectionerror", "connecttimeout", "name or service not known", "timed out")):
        hints.append(
            "The server could not reach an external provider. Check DNS, "
            "proxy settings, and the provider status, then retry."
        )

    if not hints:
        return text
    return text + " — " + " ".join(hints)
