"""AI scene images for short videos, with a slow Ken Burns move per scene.

The flow for one video:

1. Split the narration into scenes of a few seconds each.
2. Ask the script LLM for one concrete visual prompt per scene.
3. Generate a still for every scene. Google is tried first (Imagen through
   ``:predict``, then the Gemini image models). A free Pollinations image is
   the optional bridge when Google refuses (for example a free-tier key with
   no billing). Anything still missing makes the caller fall back to stock.
4. Render all stills in one FFmpeg pass: each still gets a slow zoom or pan,
   and neighbouring scenes are joined with a short crossfade.

The result is a single MP4 covering the narration, so the normal combine and
subtitle stages run unchanged. Nothing here prints the API key.
"""

from __future__ import annotations

import base64
import io
import json
import math
import os
import random
import re
import subprocess
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

import requests
from loguru import logger
from PIL import Image

from app.config import config
from app.models.schema import VideoAspect
from app.utils import utils

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_IMAGE_MODELS = (
    "imagen-4.0-fast-generate-001",
    "imagen-4.0-generate-001",
    "imagen-3.0-generate-002",
    "gemini-2.5-flash-image",
    "gemini-3.1-flash-lite-image",
    "gemini-3.1-flash-image",
)
DEFAULT_STYLE = (
    "photorealistic, cinematic lighting, shallow depth of field, rich color, "
    "high detail, vertical composition, no text, no letters, no watermark, no logo"
)
_SCENE_TARGET_SECONDS = 4.5
_CROSSFADE_SECONDS = 0.45
_FPS = 30

# Models that refused for a reason that will not change within the hour
# (not found, billing, free tier limit 0). Skipping them saves a request per
# scene and keeps one video from burning the whole minute's quota.
_dead_models: dict[str, float] = {}
_dead_lock = threading.Lock()
_DEAD_TTL_SECONDS = 3600


class AIVisualsError(Exception):
    pass


@dataclass
class Scene:
    text: str
    duration: float
    prompt: str = ""
    image_path: str = ""
    image_provider: str = ""


@dataclass
class VisualsResult:
    video_path: str
    scenes: list[Scene] = field(default_factory=list)
    providers: dict[str, int] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "video_source": "gemini_image",
            "scene_count": len(self.scenes),
            "image_providers": dict(self.providers),
            "google_images": sum(
                n for p, n in self.providers.items() if p.startswith("google:")
            ),
            "scenes": [
                {
                    "text": s.text[:200],
                    "duration": round(s.duration, 2),
                    "prompt": s.prompt[:400],
                    "provider": s.image_provider,
                }
                for s in self.scenes
            ],
        }


# ---------------------------------------------------------------- settings


def _cfg(key: str, default: Any = None) -> Any:
    value = config.app.get(key, default)
    return default if value in (None, "") else value


def _api_key() -> str:
    return str(_cfg("gemini_api_key", "") or os.environ.get("GEMINI_API_KEY", "")).strip()


def is_enabled() -> bool:
    return bool(_api_key()) or _free_fallback_enabled()


def _free_fallback_enabled() -> bool:
    value = _cfg("ai_image_free_fallback", os.environ.get("AI_IMAGE_FREE_FALLBACK", "true"))
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _image_models() -> list[str]:
    raw = _cfg("gemini_image_models", os.environ.get("GEMINI_IMAGE_MODELS", ""))
    if isinstance(raw, str):
        models = [m.strip() for m in raw.split(",") if m.strip()]
    else:
        models = [str(m).strip() for m in (raw or []) if str(m).strip()]
    return models or list(DEFAULT_IMAGE_MODELS)


def _max_scenes() -> int:
    try:
        return max(2, min(int(_cfg("ai_image_max_scenes", 12)), 20))
    except (TypeError, ValueError):
        return 12


def _style() -> str:
    return str(_cfg("ai_image_style", DEFAULT_STYLE)).strip() or DEFAULT_STYLE


def _aspect_ratio(aspect: VideoAspect) -> str:
    return {"9:16": "9:16", "16:9": "16:9", "1:1": "1:1"}.get(aspect.value, "9:16")


def _redact(text: str) -> str:
    key = _api_key()
    text = str(text or "")
    return text.replace(key, "***") if key else text


# ------------------------------------------------------------------ scenes


def split_scenes(script: str, audio_duration: float) -> list[Scene]:
    """Group narration sentences into scenes of roughly equal screen time."""

    text = utils.normalize_script_for_subtitle_matching(
        utils.remove_pause_tags(script or "")
    ).strip()
    sentences = [s.strip() for s in utils.split_string_by_punctuations(text) if s.strip()]
    if not sentences:
        sentences = [text or "money"]
    total_chars = sum(len(s) for s in sentences) or 1
    duration = max(float(audio_duration), 1.0)

    target_count = max(2, min(_max_scenes(), round(duration / _SCENE_TARGET_SECONDS)))
    if len(sentences) <= target_count:
        groups = [[s] for s in sentences]
    else:
        per_scene = total_chars / target_count
        groups, current, size = [], [], 0
        for sentence in sentences:
            current.append(sentence)
            size += len(sentence)
            if size >= per_scene and len(groups) < target_count - 1:
                groups.append(current)
                current, size = [], 0
        if current:
            groups.append(current)

    # Very long single sentences become two scenes so no still lingers too long.
    scenes: list[Scene] = []
    for group in groups:
        body = " ".join(group)
        seconds = duration * len(body) / total_chars
        if seconds > 8 and len(scenes) < _max_scenes() - 1:
            scenes.append(Scene(text=body, duration=seconds / 2))
            scenes.append(Scene(text=body, duration=seconds / 2))
        else:
            scenes.append(Scene(text=body, duration=seconds))
    # Keep the sum exact; rounding in the proportional split can drift.
    drift = duration - sum(s.duration for s in scenes)
    scenes[-1].duration = max(0.8, scenes[-1].duration + drift)
    return scenes


def _parse_prompt_list(response: str, count: int) -> list[str]:
    text = (response or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    match = re.search(r"\[.*\]", text, flags=re.DOTALL)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return []
    prompts = []
    for item in data:
        if isinstance(item, dict):
            item = item.get("prompt") or item.get("visual") or ""
        item = str(item or "").strip()
        if item:
            prompts.append(item[:600])
    return prompts[:count] if len(prompts) >= count else []


def write_visual_prompts(subject: str, scenes: list[Scene]) -> None:
    """Fill ``scene.prompt`` with a concrete, shootable description."""

    from app.services import llm

    numbered = "\n".join(f"{i + 1}. {s.text}" for i, s in enumerate(scenes))
    prompt = (
        "You are the art director for a faceless YouTube Shorts channel.\n"
        f"Video topic: {subject}\n"
        "For each numbered narration line below, write ONE image prompt for a "
        "single photorealistic still that shows exactly what the line says. "
        "Be concrete: subject, action, setting, camera angle, lighting, mood. "
        "Show people from behind, as hands, or as silhouettes rather than faces. "
        "Keep a consistent visual style across all scenes. Never ask for text, "
        "captions, numbers, logos, or brand names in the image. 25 to 45 words each.\n\n"
        f"{numbered}\n\n"
        f"Reply with only a JSON array of exactly {len(scenes)} strings, in order."
    )
    prompts: list[str] = []
    for attempt in range(2):
        try:
            response = llm._generate_response(prompt)
        except Exception as exc:  # the LLM adapter normally returns "Error: ..."
            logger.warning(f"visual prompt request failed: {type(exc).__name__}")
            response = ""
        if isinstance(response, str) and not response.startswith("Error: "):
            prompts = _parse_prompt_list(response, len(scenes))
            if prompts:
                break
        logger.warning(f"visual prompts were not usable, attempt {attempt + 1}")
    for index, scene in enumerate(scenes):
        base = prompts[index] if prompts else f"{subject}: {scene.text}"
        scene.prompt = f"{base.rstrip('. ')}. {_style()}"


# ------------------------------------------------------------ image calls


def _mark_dead(model: str, reason: str) -> None:
    with _dead_lock:
        _dead_models[model] = time.time()
    logger.warning(f"image model disabled for an hour: model={model}, reason={reason}")


def _is_dead(model: str) -> bool:
    with _dead_lock:
        marked = _dead_models.get(model)
        if marked and time.time() - marked < _DEAD_TTL_SECONDS:
            return True
        _dead_models.pop(model, None)
        return False


def _error_message(response: requests.Response) -> str:
    try:
        body = response.json()
        return str((body.get("error") or {}).get("message") or body)[:300]
    except ValueError:
        return response.text[:300]


def _permanent_refusal(status: int, message: str) -> bool:
    lowered = message.lower()
    if status in (400, 403, 404):
        return True
    if status == 429 and ("limit: 0" in lowered or "billing" in lowered or "per day" in lowered):
        return True
    return False


def _google_image(model: str, prompt: str, aspect: VideoAspect) -> bytes | None:
    key = _api_key()
    headers = {"x-goog-api-key": key, "Content-Type": "application/json"}
    ratio = _aspect_ratio(aspect)
    if model.startswith("imagen"):
        url = f"{GEMINI_API_BASE}/models/{model}:predict"
        payload = {
            "instances": [{"prompt": prompt}],
            "parameters": {"sampleCount": 1, "aspectRatio": ratio, "personGeneration": "allow_adult"},
        }
    else:
        url = f"{GEMINI_API_BASE}/models/{model}:generateContent"
        payload = {
            "contents": [{"parts": [{"text": f"Generate an image: {prompt}"}]}],
            "generationConfig": {
                "responseModalities": ["IMAGE"],
                "imageConfig": {"aspectRatio": ratio},
            },
        }
    for attempt in range(2):
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=120)
        except requests.RequestException as exc:
            logger.warning(f"image request error: model={model}, {type(exc).__name__}")
            return None
        if response.status_code == 200:
            break
        message = _redact(_error_message(response))
        if _permanent_refusal(response.status_code, message):
            _mark_dead(model, f"HTTP {response.status_code}: {message[:160]}")
            return None
        if response.status_code in (429, 500, 503) and attempt == 0:
            time.sleep(8)
            continue
        logger.warning(f"image request failed: model={model}, HTTP {response.status_code}: {message[:160]}")
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    if model.startswith("imagen"):
        for prediction in body.get("predictions") or []:
            data = prediction.get("bytesBase64Encoded")
            if data:
                return base64.b64decode(data)
        return None
    for candidate in body.get("candidates") or []:
        for part in (candidate.get("content") or {}).get("parts") or []:
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                return base64.b64decode(inline["data"])
    return None


def _pollinations_image(prompt: str, aspect: VideoAspect) -> bytes | None:
    width, height = {"9:16": (768, 1344), "16:9": (1344, 768)}.get(aspect.value, (1024, 1024))
    short = prompt[:700]
    url = (
        "https://image.pollinations.ai/prompt/"
        + urllib.parse.quote(short, safe="")
        + f"?width={width}&height={height}&nologo=true&model=flux&seed={random.randint(1, 10**6)}"
    )
    for attempt in range(3):
        try:
            response = requests.get(url, timeout=120)
            if response.status_code == 200 and response.headers.get("content-type", "").startswith("image/"):
                return response.content
            logger.warning(f"pollinations HTTP {response.status_code}")
        except requests.RequestException as exc:
            logger.warning(f"pollinations error: {type(exc).__name__}")
        time.sleep(4 + attempt * 4)
    return None


def _save_image(data: bytes, path: str, crop_bottom: float = 0.0) -> bool:
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            image = image.convert("RGB")
            if crop_bottom > 0:
                w, h = image.size
                image = image.crop((0, 0, w, int(h * (1 - crop_bottom))))
            image.save(path, format="JPEG", quality=93)
        return True
    except Exception as exc:
        logger.warning(f"generated image could not be decoded: {type(exc).__name__}: {exc}")
        return False


def generate_image(prompt: str, aspect: VideoAspect, path: str) -> str:
    """Write one still to ``path``. Returns the provider label, or '' on failure."""

    if _api_key():
        for model in _image_models():
            if _is_dead(model):
                continue
            data = _google_image(model, prompt, aspect)
            if data and _save_image(data, path):
                return f"google:{model}"
    if _free_fallback_enabled():
        data = _pollinations_image(prompt, aspect)
        # Pollinations stamps a small mark in the bottom corner; trim it.
        if data and _save_image(data, path, crop_bottom=0.045):
            return "pollinations:flux"
    return ""


# ---------------------------------------------------------------- render

_MOVES = ("zoom_in", "pan_right", "zoom_out", "pan_up", "zoom_in", "pan_left")


def _zoompan(move: str, frames: int, width: int, height: int) -> str:
    n = max(frames - 1, 1)
    zmax = 1.14
    if move == "zoom_in":
        z, x, y = f"1+{zmax - 1:.3f}*on/{n}", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    elif move == "zoom_out":
        z, x, y = f"{zmax:.3f}-{zmax - 1:.3f}*on/{n}", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    elif move == "pan_right":
        z, x, y = "1.12", f"(iw-iw/zoom)*on/{n}", "ih/2-(ih/zoom/2)"
    elif move == "pan_left":
        z, x, y = "1.12", f"(iw-iw/zoom)*(1-on/{n})", "ih/2-(ih/zoom/2)"
    else:  # pan_up
        z, x, y = "1.12", "iw/2-(iw/zoom/2)", f"(ih-ih/zoom)*(1-on/{n})"
    return f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={width}x{height}:fps={_FPS}"


def render_scenes(scenes: list[Scene], aspect: VideoAspect, output: str) -> None:
    width, height = aspect.to_resolution()
    width -= width % 2
    height -= height % 2
    up_w, up_h = width * 2, height * 2
    fade = _CROSSFADE_SECONDS if len(scenes) > 1 else 0.0

    inputs: list[str] = []
    chains: list[str] = []
    lengths: list[float] = []
    for index, scene in enumerate(scenes):
        # Every scene but the last is lengthened by the crossfade so the
        # overlapping transitions do not shorten the total runtime.
        seconds = scene.duration + (fade if index < len(scenes) - 1 else 0.0)
        frames = max(int(math.ceil(seconds * _FPS)), 2)
        lengths.append(frames / _FPS)
        inputs += ["-i", scene.image_path]
        move = _MOVES[index % len(_MOVES)]
        chains.append(
            f"[{index}:v]scale={up_w}:{up_h}:force_original_aspect_ratio=increase,"
            f"crop={up_w}:{up_h},setsar=1,{_zoompan(move, frames, width, height)},"
            f"format=yuv420p,settb=AVTB[v{index}]"
        )
    last = "v0"
    offset = 0.0
    for index in range(1, len(scenes)):
        offset += lengths[index - 1] - fade
        out = f"x{index}"
        chains.append(
            f"[{last}][v{index}]xfade=transition=fade:duration={fade:.2f}:offset={offset:.3f}[{out}]"
        )
        last = out
    command = [
        utils.get_ffmpeg_binary(), "-y", "-hide_banner", "-loglevel", "error",
        *inputs,
        "-filter_complex", ";".join(chains),
        "-map", f"[{last}]",
        "-r", str(_FPS), "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-threads", "2", "-an", output,
    ]
    total = sum(s.duration for s in scenes)
    timeout = max(300, int(total * 20))
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0 or not os.path.isfile(output):
        raise AIVisualsError(f"ffmpeg Ken Burns render failed: {result.stderr[-600:]}")


# ----------------------------------------------------------------- entry


def build_ai_visuals(
    *,
    task_id: str,
    subject: str,
    script: str,
    audio_duration: float,
    aspect: VideoAspect | str = VideoAspect.portrait,
    progress: Any = None,
) -> VisualsResult:
    """Make one MP4 of animated AI stills covering ``audio_duration`` seconds.

    Raises AIVisualsError when too few scenes got an image, so the caller can
    switch to stock footage instead of shipping a broken video.
    """

    aspect = VideoAspect(aspect)
    work_dir = os.path.join(utils.task_dir(task_id), "ai-visuals")
    os.makedirs(work_dir, exist_ok=True)

    scenes = split_scenes(script, float(audio_duration) + 0.4)
    logger.info(f"ai visuals: {len(scenes)} scenes for {audio_duration:.1f}s of narration")
    write_visual_prompts(subject, scenes)

    providers: dict[str, int] = {}
    for index, scene in enumerate(scenes):
        path = os.path.join(work_dir, f"scene-{index:02d}.jpg")
        provider = generate_image(scene.prompt, aspect, path)
        if provider:
            scene.image_path = path
            scene.image_provider = provider
            providers[provider] = providers.get(provider, 0) + 1
            logger.info(f"ai visuals: scene {index + 1}/{len(scenes)} via {provider}")
        else:
            logger.warning(f"ai visuals: scene {index + 1} has no image")
        if callable(progress):
            try:
                progress((index + 1) / (len(scenes) + 1))
            except Exception:
                pass

    made = [s for s in scenes if s.image_path]
    if len(made) < max(2, math.ceil(len(scenes) * 0.6)):
        raise AIVisualsError(
            f"only {len(made)} of {len(scenes)} scene images were generated"
        )
    # A scene without an image borrows the previous still (or the next one)
    # but keeps its own duration and a different camera move.
    for index, scene in enumerate(scenes):
        if not scene.image_path:
            donor = next(
                (s for s in reversed(scenes[:index]) if s.image_path and s.image_provider != "reused"),
                None,
            ) or made[0]
            scene.image_path = donor.image_path
            scene.image_provider = "reused"

    output = os.path.join(work_dir, "ai-visuals.mp4")
    started = time.time()
    render_scenes(scenes, aspect, output)
    logger.info(f"ai visuals rendered in {time.time() - started:.1f}s: {output}")
    return VisualsResult(video_path=output, scenes=scenes, providers=providers)
