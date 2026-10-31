"""Openverse and Wikimedia Commons stock fallbacks.

Pixabay stays the first free source. These two providers are used only when an
earlier provider errors or does not cover the narration, and only for CC0 or
public-domain files. Unsplash is not queried.
"""

from __future__ import annotations

import html
import os
import re
import subprocess
import tempfile
import threading
import time
from typing import Any
from urllib.parse import quote

import requests
from loguru import logger
from PIL import Image, UnidentifiedImageError

from app.config import config
from app.models.schema import MaterialInfo, VideoAspect
from app.utils import utils

OPENVERSE_IMAGES_URL = "https://api.openverse.org/v1/images/"
OPENVERSE_TOKEN_URL = "https://api.openverse.org/v1/auth_tokens/token/"
WIKIMEDIA_API_URL = "https://commons.wikimedia.org/w/api.php"
PROJECT_URL = "https://github.com/harry0703/MoneyPrinterTurbo"

STOCK_PROVIDERS = ("pexels", "pixabay", "coverr", "openverse", "wikimedia")
BUILTIN_FALLBACK = ("pixabay", "openverse", "wikimedia")
ALLOWED_OPENVERSE_LICENSES = {"cc0": "CC0", "pdm": "Public domain"}
EXCLUDED_OPENVERSE_SOURCES = {"unsplash"}
WIKIMEDIA_IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
WIKIMEDIA_VIDEO_MIMES = {"video/webm", "video/ogg", "application/ogg"}
MIN_STILL_SIDE = 480
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_WIKIMEDIA_VIDEO_BYTES = 80 * 1024 * 1024

_TAG_RE = re.compile(r"<[^>]+>")
_pace_lock = threading.Lock()
_next_request_at = {"openverse": 0.0, "wikimedia": 0.0}
_token_lock = threading.Lock()
_openverse_token: dict[str, Any] = {"value": "", "expires_at": 0.0}


def stock_user_agent() -> str:
    """User-Agent with a contact URL, as Wikimedia and Openverse both ask for."""

    contact = str(
        os.environ.get("STOCK_MEDIA_CONTACT")
        or config.app.get("stock_media_contact")
        or PROJECT_URL
    ).strip() or PROJECT_URL
    return f"MoneyPrinterTurbo/1.3.8 ({contact}) bot"


def parse_video_sources(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = value.split(",")
    elif isinstance(value, (list, tuple)):
        parts = []
        for item in value:
            parts.extend(str(item).split(","))
    else:
        return []
    cleaned: list[str] = []
    for part in parts:
        name = str(part).strip().lower()
        if name in STOCK_PROVIDERS and name not in cleaned:
            cleaned.append(name)
    return cleaned


def provider_chain(selected: str, configured: Any = None) -> list[str]:
    """Order stock providers. The selected source is first when it is one of them."""

    selected_name = (selected or "").strip().lower()
    if selected_name not in STOCK_PROVIDERS:
        return [selected_name or "pexels"]
    configured_list = parse_video_sources(configured)
    if not configured_list:
        if selected_name in BUILTIN_FALLBACK:
            return [selected_name, *[name for name in BUILTIN_FALLBACK if name != selected_name]]
        return [selected_name]
    if selected_name in configured_list:
        return [selected_name, *[name for name in configured_list if name != selected_name]]
    return [selected_name, *configured_list]


def should_try_next_provider(
    *,
    found_duration: float,
    audio_duration: float,
    added_count: int,
) -> bool:
    """True when this provider errored, returned nothing, or left the narration short."""

    if added_count <= 0:
        return True
    if audio_duration <= 0:
        return False
    return found_duration < audio_duration


def openverse_license_label(license_code: Any) -> str | None:
    if not isinstance(license_code, str):
        return None
    return ALLOWED_OPENVERSE_LICENSES.get(license_code.strip().lower())


def wikimedia_license_label(short_name: Any) -> str | None:
    text = _plain_text(short_name).lower()
    if text == "cc0" or text.startswith("cc0 "):
        return "CC0"
    if text == "public domain" or text.startswith("public domain "):
        return "Public domain"
    return None


def material_from_openverse(
    result: Any,
    *,
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect,
) -> MaterialInfo | None:
    if not isinstance(result, dict):
        return None
    source_name = str(result.get("source") or result.get("provider") or "").strip().lower()
    if source_name in EXCLUDED_OPENVERSE_SOURCES:
        return None
    license_label = openverse_license_label(result.get("license"))
    if not license_label:
        return None
    url = result.get("url")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return None
    width = _positive_int(result.get("width"))
    height = _positive_int(result.get("height"))
    if width and height and min(width, height) < MIN_STILL_SIDE:
        return None
    frame_width, frame_height = VideoAspect(video_aspect).to_resolution()
    creator_name = str(result.get("creator") or "").strip()
    item = MaterialInfo()
    item.provider = "openverse"
    item.url = url
    item.duration = _clip_duration(minimum_duration)
    item.source_info = {
        "provider": "openverse",
        "search_term": search_term,
        "asset_id": str(result.get("id") or ""),
        "source_page": result.get("foreign_landing_url") or result.get("detail_url") or "",
        "creator": {"name": creator_name, "profile_page": result.get("creator_url") or ""},
        "license": license_label,
        "license_url": result.get("license_url") or "",
        "media_kind": "image",
        "frame": {"width": frame_width, "height": frame_height},
        # The search cache rejects a still whose stored size does not match
        # the output aspect. Stills are cropped to the frame, so record that.
        "rendition": {"width": frame_width, "height": frame_height},
    }
    return item


def material_from_wikimedia(
    page: Any,
    *,
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect,
) -> MaterialInfo | None:
    if not isinstance(page, dict) or page.get("missing"):
        return None
    info_list = page.get("imageinfo")
    if not isinstance(info_list, list) or not info_list or not isinstance(info_list[0], dict):
        return None
    info = info_list[0]
    metadata = info.get("extmetadata") if isinstance(info.get("extmetadata"), dict) else {}
    short_name = _metadata_value(metadata, "LicenseShortName")
    license_label = wikimedia_license_label(short_name)
    if not license_label:
        return None
    mime = str(info.get("mime") or "").split(";", 1)[0].strip().lower()
    mediatype = str(info.get("mediatype") or "").upper()
    is_video = mime in WIKIMEDIA_VIDEO_MIMES or mediatype == "VIDEO"
    is_image = mime in WIKIMEDIA_IMAGE_MIMES or mediatype in {"BITMAP", "DRAWING"}
    if is_video and mime not in WIKIMEDIA_VIDEO_MIMES and not str(page.get("title") or "").lower().endswith(
        (".webm", ".ogv")
    ):
        return None
    if not is_video and not is_image:
        return None
    width = _positive_int(info.get("thumbwidth") or info.get("width"))
    height = _positive_int(info.get("thumbheight") or info.get("height"))
    size = _positive_int(info.get("size"))
    if is_video and size and size > MAX_WIKIMEDIA_VIDEO_BYTES:
        return None
    if not is_video and width and height and min(width, height) < MIN_STILL_SIDE:
        return None
    url = info.get("url") if is_video else (info.get("thumburl") or info.get("url"))
    if not isinstance(url, str) or not url.startswith("https://"):
        return None
    if is_video and not _matches_video_orientation(width, height, video_aspect):
        return None
    frame_width, frame_height = VideoAspect(video_aspect).to_resolution()
    if is_video:
        parsed_duration = _wikimedia_duration(metadata)
        minimum = max(1, int(minimum_duration or 1))
        if parsed_duration and parsed_duration < minimum:
            return None
        duration = parsed_duration or _clip_duration(minimum_duration)
    else:
        duration = _clip_duration(minimum_duration)
    title = str(page.get("title") or "")
    page_url = info.get("descriptionurl") or _commons_page(title)
    item = MaterialInfo()
    item.provider = "wikimedia"
    item.url = url
    item.duration = int(duration)
    item.source_info = {
        "provider": "wikimedia",
        "search_term": search_term,
        "asset_id": title,
        "source_page": page_url,
        "creator": {"name": _plain_text(_metadata_value(metadata, "Artist"))},
        "license": license_label,
        "license_url": _metadata_value(metadata, "LicenseUrl"),
        "media_kind": "video" if is_video else "image",
        "frame": {"width": frame_width, "height": frame_height},
        "rendition": {
            "width": width if is_video and width and height else frame_width,
            "height": height if is_video and width and height else frame_height,
        },
    }
    return item


def search_images_openverse(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> list[MaterialInfo]:
    aspect = VideoAspect(video_aspect)
    params = {
        "q": search_term,
        "license": "cc0,pdm",
        "page_size": 12,
        "filter_dead": "true",
        "mature": "false",
    }
    if aspect == VideoAspect.portrait:
        params["aspect_ratio"] = "tall"
    elif aspect == VideoAspect.landscape:
        params["aspect_ratio"] = "wide"
    else:
        params["aspect_ratio"] = "square"
    headers = {"User-Agent": stock_user_agent(), "Accept": "application/json"}
    token = _openverse_access_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    _pace("openverse", 0.7 if token else 3.2)
    logger.info(f"searching images on openverse: term={search_term!r}, authenticated={bool(token)}")
    try:
        response = requests.get(
            OPENVERSE_IMAGES_URL,
            params=params,
            headers=headers,
            timeout=(20, 60),
            verify=_tls_verify(),
        )
        if response.status_code == 429 or response.status_code >= 400:
            logger.warning(f"openverse image search failed: status={response.status_code}")
            return []
        payload = response.json()
    except Exception as exc:
        logger.warning(f"openverse image search failed: error={type(exc).__name__}")
        return []
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        return []
    items: list[MaterialInfo] = []
    for result in results:
        item = material_from_openverse(
            result,
            search_term=search_term,
            minimum_duration=minimum_duration,
            video_aspect=aspect,
        )
        if item is not None:
            items.append(item)
    return items


def search_media_wikimedia(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> list[MaterialInfo]:
    aspect = VideoAspect(video_aspect)
    frame_width, _frame_height = aspect.to_resolution()
    params = {
        "action": "query",
        "format": "json",
        "formatversion": 2,
        "generator": "search",
        "gsrsearch": search_term,
        "gsrnamespace": 6,
        "gsrlimit": 8,
        "prop": "imageinfo",
        "iiprop": "url|size|mime|extmetadata|mediatype",
        "iiextmetadatafilter": "LicenseShortName|LicenseUrl|Artist",
        "iiurlwidth": max(frame_width, 720),
        "maxlag": 5,
    }
    headers = {
        "User-Agent": stock_user_agent(),
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
    }
    _pace("wikimedia", 1.1)
    logger.info(f"searching media on wikimedia commons: term={search_term!r}")
    try:
        response = requests.get(
            WIKIMEDIA_API_URL,
            params=params,
            headers=headers,
            timeout=(20, 60),
            verify=_tls_verify(),
        )
        if response.status_code == 429 or response.status_code >= 400:
            logger.warning(f"wikimedia search failed: status={response.status_code}")
            return []
        payload = response.json()
    except Exception as exc:
        logger.warning(f"wikimedia search failed: error={type(exc).__name__}")
        return []
    if isinstance(payload, dict) and payload.get("error"):
        logger.warning(f"wikimedia search returned an error: code={payload['error'].get('code')}")
        return []
    pages = []
    query = payload.get("query") if isinstance(payload, dict) else None
    if isinstance(query, dict) and isinstance(query.get("pages"), list):
        pages = query["pages"]
    items: list[MaterialInfo] = []
    for page in pages:
        item = material_from_wikimedia(
            page,
            search_term=search_term,
            minimum_duration=minimum_duration,
            video_aspect=aspect,
        )
        if item is not None:
            items.append(item)
    return items


def save_image_clip(
    image_url: str,
    save_dir: str,
    *,
    duration: int,
    width: int = 0,
    height: int = 0,
    user_agent: str = "",
) -> str:
    """Download a still image and render a short Ken Burns clip."""

    if not save_dir:
        save_dir = utils.storage_dir("cache_videos")
    os.makedirs(save_dir, exist_ok=True)
    frame_width, frame_height = _frame_size(width, height)
    clip_duration = max(1, int(duration or 4))
    identity = utils.md5(
        f"{image_url.split('#', 1)[0]}|{clip_duration}|{frame_width}x{frame_height}"
    )
    video_path = os.path.join(save_dir, f"vid-{identity}.mp4")
    if os.path.exists(video_path) and os.path.getsize(video_path) > 0:
        return video_path

    downloaded = ""
    prepared = ""
    temp_video = ""
    try:
        downloaded = _download_image(image_url, save_dir, user_agent or stock_user_agent())
        if not downloaded:
            return ""
        prepared = _prepare_still(downloaded, frame_width, frame_height)
        if not prepared:
            return ""
        descriptor, temp_video = tempfile.mkstemp(prefix=".kenburns-", suffix=".mp4", dir=save_dir)
        os.close(descriptor)
        if not _render_ken_burns(prepared, temp_video, clip_duration, frame_width, frame_height):
            return ""
        os.replace(temp_video, video_path)
        temp_video = ""
        return video_path
    finally:
        for path in (downloaded, prepared, temp_video):
            if path and os.path.exists(path) and os.path.abspath(path) != os.path.abspath(video_path):
                try:
                    os.remove(path)
                except OSError as exc:
                    logger.warning(f"failed to remove temporary still: error={type(exc).__name__}")


def write_sources_manifest(task_id: str, records: list[dict[str, Any]]) -> None:
    """Write sources.json beside the task video. Retention deletes the whole folder."""

    import json

    try:
        directory = utils.task_dir(task_id)
    except Exception as exc:
        logger.warning(f"could not create the task directory for sources.json: {type(exc).__name__}")
        return
    clips = []
    for record in records:
        if not isinstance(record, dict):
            continue
        creator = record.get("creator")
        if isinstance(creator, dict):
            creator_name = str(creator.get("name") or "").strip()
        else:
            creator_name = str(creator or "").strip()
        provider = str(record.get("provider") or "").strip()
        license_name = str(record.get("license") or "").strip() or _default_license(provider)
        clips.append(
            {
                "provider": provider,
                "page_url": str(record.get("source_page") or record.get("page_url") or ""),
                "creator": creator_name,
                "license": license_name,
                "local_file": str(record.get("local_file") or ""),
            }
        )
    target = os.path.join(directory, "sources.json")
    temporary = target + ".tmp"
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump({"clips": clips}, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, target)
    except OSError as exc:
        logger.warning(f"failed to write sources.json: task_id={task_id}, error={type(exc).__name__}")
        try:
            os.remove(temporary)
        except OSError:
            pass


def _default_license(provider: str) -> str:
    return {
        "pexels": "Pexels License",
        "pixabay": "Pixabay Content License",
        "coverr": "Coverr License",
        "openverse": "",
        "wikimedia": "",
    }.get(provider, "")


def _render_ken_burns(
    image_path: str,
    output_path: str,
    duration: int,
    width: int,
    height: int,
) -> bool:
    from app.runtime.profile import low_memory_enabled

    low_memory = low_memory_enabled()
    fps = 24 if low_memory else 30
    frames = max(fps, int(duration * fps))
    video_filter = (
        f"zoompan=z='min(1+0.12*on/{frames},1.12)':"
        f"x='(iw-iw/zoom)*on/{frames}*0.4':"
        f"y='(ih-ih/zoom)*0.45':"
        f"d={frames}:s={width}x{height}:fps={fps}"
    )
    command = [
        utils.get_ffmpeg_binary(),
        "-y",
        "-loop",
        "1",
        "-i",
        image_path,
        "-vf",
        video_filter,
        "-frames:v",
        str(frames),
        "-an",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-threads",
        "1" if low_memory else "2",
        "-preset",
        "veryfast" if low_memory else "medium",
        "-crf",
        "28" if low_memory else "23",
        "-movflags",
        "+faststart",
        output_path,
    ]
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=max(30, duration * 20),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning(f"ken burns ffmpeg failed: error={type(exc).__name__}")
        return _render_ken_burns_moviepy(image_path, output_path, duration)
    if completed.returncode != 0 or not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        detail = (completed.stderr or b"")[-400:].decode("utf-8", errors="replace")
        logger.warning(f"ken burns ffmpeg exited {completed.returncode}: {detail}")
        return _render_ken_burns_moviepy(image_path, output_path, duration)
    return True


def _render_ken_burns_moviepy(image_path: str, output_path: str, duration: int) -> bool:
    """MoviePy zoom fallback when the ffmpeg zoompan filter cannot run."""

    try:
        from app.services.video import render_image_zoom_video

        rendered = render_image_zoom_video(image_path, clip_duration=duration)
    except Exception as exc:
        logger.warning(f"moviepy ken burns fallback failed: error={type(exc).__name__}")
        return False
    if not rendered or not os.path.exists(rendered):
        return False
    if os.path.abspath(rendered) != os.path.abspath(output_path):
        os.replace(rendered, output_path)
    return os.path.exists(output_path) and os.path.getsize(output_path) > 0


def _prepare_still(image_path: str, width: int, height: int) -> str:
    canvas_width = _even(int(width * 1.2))
    canvas_height = _even(int(height * 1.2))
    try:
        with Image.open(image_path) as image:
            image = image.convert("RGB")
            if image.width * image.height > 24_000_000:
                image.thumbnail((4096, 4096), Image.Resampling.LANCZOS)
            scale = max(canvas_width / image.width, canvas_height / image.height)
            resized = image.resize(
                (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
                Image.Resampling.LANCZOS,
            )
            left = max(0, (resized.width - canvas_width) // 2)
            top = max(0, (resized.height - canvas_height) // 2)
            frame = resized.crop((left, top, left + canvas_width, top + canvas_height))
            prepared = image_path + ".kb.jpg"
            frame.save(prepared, format="JPEG", quality=85, optimize=True)
            return prepared
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        logger.warning(f"could not prepare a still image: error={type(exc).__name__}")
        return ""


def _download_image(image_url: str, save_dir: str, user_agent: str) -> str:
    descriptor, path = tempfile.mkstemp(prefix=".still-", suffix=".img", dir=save_dir)
    os.close(descriptor)
    saved = False
    try:
        with requests.get(
            image_url,
            headers={"User-Agent": user_agent, "Accept": "image/*"},
            timeout=(20, 120),
            stream=True,
            verify=_tls_verify(),
        ) as response:
            if response.status_code != 200:
                return ""
            size = 0
            with open(path, "wb") as handle:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > MAX_IMAGE_BYTES:
                        return ""
                    handle.write(chunk)
        if size <= 0:
            return ""
        saved = True
        return path
    except Exception as exc:
        logger.warning(f"still image download failed: error={type(exc).__name__}")
        return ""
    finally:
        if not saved:
            try:
                os.remove(path)
            except OSError:
                pass


def _openverse_access_token() -> str:
    client_id = str(
        os.environ.get("OPENVERSE_CLIENT_ID") or config.app.get("openverse_client_id") or ""
    ).strip()
    client_secret = str(
        os.environ.get("OPENVERSE_CLIENT_SECRET") or config.app.get("openverse_client_secret") or ""
    ).strip()
    if not client_id or not client_secret:
        return ""
    now = time.time()
    with _token_lock:
        if _openverse_token["value"] and _openverse_token["expires_at"] > now + 60:
            return str(_openverse_token["value"])
        try:
            response = requests.post(
                OPENVERSE_TOKEN_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
                headers={
                    "User-Agent": stock_user_agent(),
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=(15, 30),
                verify=_tls_verify(),
            )
            if response.status_code != 200:
                logger.warning(f"openverse token request failed: status={response.status_code}")
                return ""
            payload = response.json()
        except Exception as exc:
            logger.warning(f"openverse token request failed: error={type(exc).__name__}")
            return ""
        token = str(payload.get("access_token") or "") if isinstance(payload, dict) else ""
        if not token:
            return ""
        expires_in = _positive_int(payload.get("expires_in")) or 3600
        _openverse_token["value"] = token
        _openverse_token["expires_at"] = now + expires_in
        return token


def _pace(provider: str, interval: float) -> None:
    with _pace_lock:
        now = time.monotonic()
        delay = _next_request_at.get(provider, 0.0) - now
        if delay > 0:
            time.sleep(delay)
            now = time.monotonic()
        _next_request_at[provider] = now + interval


def _frame_size(width: int, height: int) -> tuple[int, int]:
    if width >= 2 and height >= 2:
        return _even(width), _even(height)
    from app.runtime.profile import low_memory_enabled

    if low_memory_enabled():
        return 1280, 720
    return 1920, 1080


def _clip_duration(minimum_duration: int) -> int:
    try:
        duration = int(minimum_duration)
    except (TypeError, ValueError):
        duration = 4
    return min(15, max(1, duration or 4))


def _matches_video_orientation(width: int, height: int, aspect: VideoAspect) -> bool:
    if not width or not height:
        return True
    if aspect == VideoAspect.square:
        return min(width, height) >= MIN_STILL_SIDE
    if aspect == VideoAspect.portrait:
        return height >= width and min(width, height) >= MIN_STILL_SIDE
    return width >= height and min(width, height) >= MIN_STILL_SIDE


def _wikimedia_duration(metadata: dict) -> int:
    raw = _plain_text(_metadata_value(metadata, "Duration"))
    match = re.search(r"\d+(?:\.\d+)?", raw)
    if not match:
        return 0
    try:
        return int(float(match.group(0)))
    except ValueError:
        return 0


def _metadata_value(metadata: dict, key: str) -> str:
    entry = metadata.get(key)
    if isinstance(entry, dict):
        return str(entry.get("value") or "")
    if isinstance(entry, str):
        return entry
    return ""


def _commons_page(title: str) -> str:
    if not title:
        return "https://commons.wikimedia.org/"
    slug = quote(title.replace(" ", "_"), safe=":/()")
    return "https://commons.wikimedia.org/wiki/" + slug


def _plain_text(value: Any) -> str:
    text = html.unescape(_TAG_RE.sub(" ", str(value or "")))
    return " ".join(text.replace("\xa0", " ").split())


def _positive_int(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _even(value: int) -> int:
    value = max(2, int(value))
    return value if value % 2 == 0 else value - 1


def _tls_verify() -> bool:
    tls_verify = config.app.get("tls_verify", True)
    if isinstance(tls_verify, str):
        return tls_verify.strip().lower() not in {"0", "false", "no", "off"}
    return bool(tls_verify)
