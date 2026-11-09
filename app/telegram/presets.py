"""Options offered by the bot keyboards and the Mini App form."""

ASPECTS = (
    ("9:16", "Portrait 9:16"),
    ("16:9", "Landscape 16:9"),
    ("1:1", "Square 1:1"),
)

VOICES = (
    ("gemini:Charon-Informative", "Gemini, Charon (calm narrator)"),
    ("gemini:Puck-Upbeat", "Gemini, Puck (upbeat)"),
    ("gemini:Kore-Firm", "Gemini, Kore (firm)"),
    ("gemini:Fenrir-Excitable", "Gemini, Fenrir (energetic)"),
    ("gemini:Aoede-Breezy", "Gemini, Aoede (breezy)"),
    ("en-US-AriaNeural", "English, Aria"),
    ("en-US-GuyNeural", "English, Guy"),
    ("en-GB-SoniaNeural", "English UK, Sonia"),
    ("zh-CN-XiaoxiaoNeural", "Chinese, Xiaoxiao"),
    ("zh-CN-YunxiNeural", "Chinese, Yunxi"),
    ("es-ES-ElviraNeural", "Spanish, Elvira"),
    ("fr-FR-DeniseNeural", "French, Denise"),
    ("de-DE-KatjaNeural", "German, Katja"),
    ("pt-BR-FranciscaNeural", "Portuguese, Francisca"),
    ("hi-IN-SwaraNeural", "Hindi, Swara"),
)

LANGUAGES = (
    ("", "Auto"),
    ("en", "English"),
    ("zh-CN", "Chinese"),
    ("es", "Spanish"),
    ("fr", "French"),
    ("de", "German"),
    ("pt", "Portuguese"),
    ("hi", "Hindi"),
)

FOOTAGE_SOURCES = (
    ("gemini_image", "AI images (Gemini), stock as fallback"),
    ("pixabay", "Pixabay, then CC0 fallbacks"),
    ("openverse", "Openverse (CC0 / public domain)"),
    ("wikimedia", "Wikimedia Commons (CC0 / public domain)"),
    ("pexels", "Pexels"),
    ("coverr", "Coverr"),
)

VOICE_NAME_RE_SOURCE = (
    r"^(?:[A-Za-z]{2,3}-[A-Za-z]{2,3}-[A-Za-z0-9]+|gemini:[A-Za-z]+(?:-[A-Za-z]+)?)$"
)

VALID_ASPECTS = {value for value, _label in ASPECTS}
VALID_FOOTAGE_SOURCES = {value for value, _label in FOOTAGE_SOURCES}
VALID_VOICES = {value for value, _label in VOICES}
VALID_LANGUAGES = {value for value, _label in LANGUAGES}


def describe_stage(progress: int, state: int) -> str:
    """Map the pipeline's progress checkpoints to a short status line."""

    if state == 1:
        return "Ready"
    if state == -1:
        return "Failed"
    if progress < 10:
        return "Writing the script"
    if progress < 20:
        return "Choosing search terms"
    if progress < 30:
        return "Recording the voiceover"
    if progress < 40:
        return "Building subtitles"
    if progress < 50:
        return "Gathering footage"
    if progress < 100:
        return "Rendering the video"
    return "Finishing"
