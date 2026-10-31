"""License filters, provider fallback, and the sources manifest."""

import json
import os
from unittest.mock import patch

import pytest

from app.models.schema import MaterialInfo, VideoAspect
from app.services import material, stock_media
from app.services.task_retention import cleanup_expired_tasks


def test_openverse_keeps_only_cc0_and_public_domain():
    kept = stock_media.material_from_openverse(
        {
            "id": "cc0-1",
            "title": "Harbor",
            "url": "https://cdn.example/harbor.jpg",
            "foreign_landing_url": "https://openverse.example/harbor",
            "license": "cc0",
            "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
            "creator": "Ada",
            "creator_url": "https://example.org/ada",
            "source": "flickr",
            "width": 1200,
            "height": 1600,
        },
        search_term="harbor",
        minimum_duration=4,
        video_aspect=VideoAspect.portrait,
    )
    assert kept is not None
    assert kept.provider == "openverse"
    assert kept.source_info["license"] == "CC0"
    assert kept.source_info["media_kind"] == "image"

    public_domain = stock_media.material_from_openverse(
        {
            "id": "pdm-1",
            "url": "https://cdn.example/dome.jpg",
            "license": "pdm",
            "creator": "NASA",
            "width": 800,
            "height": 800,
        },
        search_term="dome",
        minimum_duration=4,
        video_aspect=VideoAspect.square,
    )
    assert public_domain.source_info["license"] == "Public domain"

    for license_code in ("by", "by-sa", "by-nc", "by-nd", "cc-by"):
        assert stock_media.material_from_openverse(
            {
                "id": license_code,
                "url": "https://cdn.example/no.jpg",
                "license": license_code,
                "width": 1000,
                "height": 1000,
            },
            search_term="no",
            minimum_duration=4,
            video_aspect=VideoAspect.landscape,
        ) is None

    assert stock_media.material_from_openverse(
        {
            "id": "unsplash-1",
            "url": "https://cdn.example/unsplash.jpg",
            "license": "cc0",
            "source": "unsplash",
            "width": 1600,
            "height": 1200,
        },
        search_term="city",
        minimum_duration=4,
        video_aspect=VideoAspect.landscape,
    ) is None


def test_wikimedia_license_short_name_filter():
    def page(short_name, mime="image/jpeg", mediatype="BITMAP"):
        return {
            "title": "File:Example.jpg",
            "imageinfo": [
                {
                    "url": "https://upload.wikimedia.org/example.jpg",
                    "descriptionurl": "https://commons.wikimedia.org/wiki/File:Example.jpg",
                    "mime": mime,
                    "mediatype": mediatype,
                    "width": 1280,
                    "height": 720,
                    "size": 1000,
                    "thumburl": "https://upload.wikimedia.org/thumb/example.jpg",
                    "thumbwidth": 1280,
                    "thumbheight": 720,
                    "extmetadata": {
                        "LicenseShortName": {"value": short_name},
                        "Artist": {"value": "<a href=\"https://example.org\">Ada</a>"},
                        "LicenseUrl": {"value": "https://creativecommons.org/publicdomain/zero/1.0/"},
                    },
                }
            ],
        }

    cc0 = stock_media.material_from_wikimedia(
        page("CC0 1.0"),
        search_term="bridge",
        minimum_duration=5,
        video_aspect=VideoAspect.landscape,
    )
    assert cc0.source_info["license"] == "CC0"
    assert cc0.source_info["creator"]["name"] == "Ada"
    assert cc0.source_info["media_kind"] == "image"

    public_domain = stock_media.material_from_wikimedia(
        page("Public domain"),
        search_term="bridge",
        minimum_duration=5,
        video_aspect=VideoAspect.landscape,
    )
    assert public_domain.source_info["license"] == "Public domain"

    for short_name in ("CC BY 4.0", "CC BY-SA 4.0", "GFDL", "Copyrighted free use"):
        assert stock_media.material_from_wikimedia(
            page(short_name),
            search_term="bridge",
            minimum_duration=5,
            video_aspect=VideoAspect.landscape,
        ) is None

    video = stock_media.material_from_wikimedia(
        page("Public domain", mime="video/webm", mediatype="VIDEO"),
        search_term="bridge",
        minimum_duration=5,
        video_aspect=VideoAspect.landscape,
    )
    assert video.source_info["media_kind"] == "video"
    assert video.url == "https://upload.wikimedia.org/example.jpg"


def test_provider_chain_and_fallback_stop_when_duration_is_covered():
    assert stock_media.provider_chain("pixabay", None) == [
        "pixabay",
        "openverse",
        "wikimedia",
    ]
    assert stock_media.provider_chain("openverse", None) == [
        "openverse",
        "pixabay",
        "wikimedia",
    ]
    assert stock_media.provider_chain("pexels", None) == ["pexels"]
    assert stock_media.provider_chain(
        "pixabay",
        "pexels, pixabay, openverse, wikimedia, unsplash",
    ) == ["pixabay", "pexels", "openverse", "wikimedia"]

    calls = []

    def search(provider):
        calls.append(provider)
        if provider == "pixabay":
            raise RuntimeError("blocked")
        if provider == "openverse":
            return [MaterialInfo(url="https://cdn.example/short.jpg", duration=2)]
        return [MaterialInfo(url="https://cdn.example/long.webm", duration=12)]

    found = 0.0
    chosen = []
    for provider in stock_media.provider_chain("pixabay", None):
        calls_before = len(calls)
        try:
            items = search(provider)
            failed = False
        except RuntimeError:
            items = []
            failed = True
        assert failed or calls_before < len(calls)
        found += sum(item.duration for item in items)
        chosen.extend(items)
        if not stock_media.should_try_next_provider(
            found_duration=found,
            audio_duration=10,
            added_count=0 if failed else len(items),
        ):
            break

    assert calls == ["pixabay", "openverse", "wikimedia"]
    assert [item.url for item in chosen][-1].endswith("long.webm")
    assert stock_media.should_try_next_provider(
        found_duration=12, audio_duration=10, added_count=1
    ) is False


def test_download_falls_through_when_pixabay_returns_nothing(tmp_path):
    saved = []

    def pixabay(*_args, **_kwargs):
        return []

    def openverse(*_args, **_kwargs):
        return [
            MaterialInfo(
                provider="openverse",
                url="https://cdn.example/harbor.jpg",
                duration=12,
                source_info={
                    "provider": "openverse",
                    "source_page": "https://openverse.example/harbor",
                    "creator": {"name": "Ada"},
                    "license": "CC0",
                    "media_kind": "image",
                    "frame": {"width": 720, "height": 1280},
                },
            )
        ]

    def wikimedia(*_args, **_kwargs):
        raise AssertionError("wikimedia should not run after openverse covers the narration")

    def store(item, save_dir):
        saved.append((item.provider, save_dir))
        return str(tmp_path / "clip.mp4")

    with (
        patch.dict(material.config.app, {"video_sources": "", "material_directory": str(tmp_path)}, clear=False),
        patch.object(material, "search_videos_pixabay", side_effect=pixabay),
        patch.object(stock_media, "search_images_openverse", side_effect=openverse),
        patch.object(stock_media, "search_media_wikimedia", side_effect=wikimedia),
        patch.object(material.material_cache, "load_material_search_cache", return_value=None),
        patch.object(material.material_cache, "save_material_search_cache"),
        patch.object(material, "_store_downloaded_material", side_effect=store),
        patch.object(material.task_artifacts, "patch_script_data", return_value=True),
    ):
        paths = material.download_videos(
            task_id="fallback-task",
            search_terms=["harbor"],
            source="pixabay",
            audio_duration=8,
            max_clip_duration=5,
        )

    assert paths == [str(tmp_path / "clip.mp4")]
    assert saved[0][0] == "openverse"


def test_sources_json_is_removed_with_the_task_folder(tmp_path, monkeypatch):
    task_id = "credited-task"
    task_dir = tmp_path / task_id
    task_dir.mkdir()
    (task_dir / "final-1.mp4").write_bytes(b"video")
    monkeypatch.setattr(
        "app.services.stock_media.utils.task_dir",
        lambda sub_dir="": str(tmp_path / sub_dir) if sub_dir else str(tmp_path),
    )
    stock_media.write_sources_manifest(
        task_id,
        [
            {
                "provider": "wikimedia",
                "source_page": "https://commons.wikimedia.org/wiki/File:Example.jpg",
                "creator": {"name": "Ada"},
                "license": "Public domain",
                "local_file": "vid-1.mp4",
            }
        ],
    )
    manifest = task_dir / "sources.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["clips"] == [
        {
            "provider": "wikimedia",
            "page_url": "https://commons.wikimedia.org/wiki/File:Example.jpg",
            "creator": "Ada",
            "license": "Public domain",
            "local_file": "vid-1.mp4",
        }
    ]
    old = manifest.stat().st_mtime - 48 * 3600
    os.utime(task_dir, (old, old))
    removed = cleanup_expired_tasks(
        task_root=str(tmp_path),
        max_age_hours=24,
        now=manifest.stat().st_mtime + 48 * 3600,
    )
    assert removed == [task_id]
    assert not manifest.exists()
    assert not task_dir.exists()


def test_telegram_token_enables_the_bot_unless_the_env_disables_it(monkeypatch):
    from app.telegram.settings import _telegram_enabled

    monkeypatch.delenv("TELEGRAM_ENABLED", raising=False)
    assert _telegram_enabled({"enabled": False}, "123:ABC") is True
    assert _telegram_enabled({"enabled": False}, "") is False
    monkeypatch.setenv("TELEGRAM_ENABLED", "false")
    assert _telegram_enabled({"enabled": True}, "123:ABC") is False
    monkeypatch.setenv("TELEGRAM_ENABLED", "true")
    assert _telegram_enabled({"enabled": False}, "") is True


def test_environment_overrides_stock_sources_and_telegram_enabled():
    from app.config.env_overlay import apply_environment_overrides

    app = {}
    telegram = {"enabled": False}
    applied = apply_environment_overrides(
        app,
        telegram,
        {
            "VIDEO_SOURCES": "pixabay, openverse, wikimedia",
            "OPENVERSE_CLIENT_ID": "client",
            "OPENVERSE_CLIENT_SECRET": "secret",
            "TELEGRAM_ENABLED": "true",
        },
    )
    assert app["video_sources"] == ["pixabay", "openverse", "wikimedia"]
    assert app["openverse_client_id"] == "client"
    assert app["openverse_client_secret"] == "secret"
    assert telegram["enabled"] is True
    assert "secret" not in " ".join(applied)
    assert "TELEGRAM_ENABLED" in applied


def test_low_memory_ken_burns_frame_is_720p(monkeypatch, tmp_path):
    monkeypatch.setenv("MPT_LOW_MEMORY", "true")
    assert stock_media._frame_size(0, 0) == (1280, 720)
    assert stock_media._frame_size(720, 1280) == (720, 1280)
    image = tmp_path / "still.png"
    from PIL import Image as PILImage

    PILImage.new("RGB", (80, 120), (20, 80, 40)).save(image)
    if not __import__("shutil").which("ffmpeg"):
        pytest.skip("ffmpeg is not installed")
    output = tmp_path / "clip.mp4"
    assert stock_media._render_ken_burns(str(image), str(output), 1, 320, 180)
    assert output.stat().st_size > 0
