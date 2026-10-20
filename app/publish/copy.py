"""Titles, descriptions, and hashtags, with an optional per-account variation."""

from __future__ import annotations

from app.services.llm import generate_social_metadata


def generate_copy(
    *,
    subject: str,
    script: str,
    platform: str,
    language: str = "",
    variation: int = 1,
    total: int = 1,
) -> dict:
    brief = (subject or "").strip() or "short video"
    if total > 1:
        brief = (
            f"{brief}\n\nWrite variation {variation} of {total} for a different "
            "account. Change the hook and the wording so the posts are not identical."
        )
    raw = generate_social_metadata(
        video_subject=brief,
        video_script=script or "",
        language=language or "auto",
        platform="youtube_shorts" if platform == "youtube" else "tiktok",
    )
    return {
        "title": str(raw.get("title") or brief)[:100],
        "description": str(raw.get("caption") or ""),
        "hashtags": [str(tag) for tag in raw.get("hashtags") or [] if str(tag).strip()],
    }


def finalize_copy(
    *,
    platform: str,
    title: str,
    description: str,
    hashtags: list[str],
    shorts: bool,
) -> dict:
    cleaned = []
    seen = set()
    for tag in hashtags:
        text = str(tag).strip()
        if not text:
            continue
        if not text.startswith("#"):
            text = "#" + text.replace(" ", "")
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(text)
    title = " ".join((title or "").split())
    description = (description or "").strip()
    if platform == "youtube":
        if shorts and "#shorts" not in title.lower():
            suffix = " #Shorts"
            if len(title) + len(suffix) <= 100:
                title = (title + suffix).strip()
            elif not any(tag.lower() == "#shorts" for tag in cleaned):
                cleaned.append("#Shorts")
        if shorts and "#shorts" not in description.lower() and not any(tag.lower() == "#shorts" for tag in cleaned):
            cleaned.append("#Shorts")
        title = title[:100]
        description = description[:5000]
        cleaned = cleaned[:15]
    else:
        title = title[:150]
        description = description[:2000]
        cleaned = cleaned[:12]
    return {"title": title, "description": description, "hashtags": cleaned}


def tiktok_caption(title: str, description: str, hashtags: list[str]) -> str:
    parts = [part.strip() for part in (title, description, " ".join(hashtags)) if part and part.strip()]
    return "\n".join(parts)[:2200]
