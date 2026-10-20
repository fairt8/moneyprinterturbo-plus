"""Publishing settings from config.toml and the environment overlay."""

from __future__ import annotations

from dataclasses import dataclass


def _bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if value is None:
        return default
    return bool(value)


def _int(value, default: int) -> int:
    if isinstance(value, bool) or value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class YoutubeProject:
    id: str
    client_id: str
    client_secret: str
    daily_upload_quota: int


@dataclass(frozen=True)
class PublishSettings:
    public_base_url: str = ""
    youtube_projects: tuple[YoutubeProject, ...] = ()
    youtube_redirect_uri: str = ""
    tiktok_client_key: str = ""
    tiktok_client_secret: str = ""
    tiktok_redirect_uri: str = ""
    tiktok_audited: bool = False
    tiktok_default_mode: str = "direct"
    tiktok_daily_posts: int = 15
    stagger_seconds: int = 20
    max_concurrent_jobs: int = 1
    vary_copy_default: bool = False
    max_attempts: int = 3
    connect_ttl_seconds: int = 900

    def youtube_project(self, project_id: str) -> YoutubeProject | None:
        wanted = (project_id or "default").strip() or "default"
        for project in self.youtube_projects:
            if project.id == wanted:
                return project
        return None

    def youtube_configured(self) -> bool:
        return any(project.client_id and project.client_secret for project in self.youtube_projects)

    def tiktok_configured(self) -> bool:
        return bool(self.tiktok_client_key and self.tiktok_client_secret)


def _projects_from_config(raw) -> list[YoutubeProject]:
    projects: list[YoutubeProject] = []
    entries = raw if isinstance(raw, list) else []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        project_id = str(entry.get("id") or "default").strip() or "default"
        projects.append(
            YoutubeProject(
                id=project_id,
                client_id=str(entry.get("client_id") or "").strip(),
                client_secret=str(entry.get("client_secret") or "").strip(),
                daily_upload_quota=max(1, _int(entry.get("daily_upload_quota"), 6)),
            )
        )
    return projects


def load_publish_settings() -> PublishSettings:
    from app.config import config

    raw = config.publish if isinstance(getattr(config, "publish", None), dict) else {}
    public_base = str(
        raw.get("public_base_url")
        or config.app.get("endpoint")
        or ""
    ).strip().rstrip("/")
    projects = _projects_from_config(raw.get("youtube_projects"))
    if not projects:
        client_id = str(raw.get("youtube_client_id") or "").strip()
        client_secret = str(raw.get("youtube_client_secret") or "").strip()
        if client_id or client_secret:
            projects = [
                YoutubeProject(
                    id="default",
                    client_id=client_id,
                    client_secret=client_secret,
                    daily_upload_quota=max(1, _int(raw.get("youtube_daily_upload_quota"), 6)),
                )
            ]
    mode = str(raw.get("tiktok_default_mode") or "direct").strip().lower()
    if mode not in {"direct", "inbox"}:
        mode = "direct"
    youtube_redirect = str(raw.get("youtube_redirect_uri") or "").strip()
    tiktok_redirect = str(raw.get("tiktok_redirect_uri") or "").strip()
    if not youtube_redirect and public_base:
        youtube_redirect = public_base + "/auth/youtube/callback"
    if not tiktok_redirect and public_base:
        tiktok_redirect = public_base + "/auth/tiktok/callback"
    return PublishSettings(
        public_base_url=public_base,
        youtube_projects=tuple(projects),
        youtube_redirect_uri=youtube_redirect,
        tiktok_client_key=str(raw.get("tiktok_client_key") or "").strip(),
        tiktok_client_secret=str(raw.get("tiktok_client_secret") or "").strip(),
        tiktok_redirect_uri=tiktok_redirect,
        tiktok_audited=_bool(raw.get("tiktok_audited"), False),
        tiktok_default_mode=mode,
        tiktok_daily_posts=max(1, _int(raw.get("tiktok_daily_posts"), 15)),
        stagger_seconds=max(0, _int(raw.get("stagger_seconds"), 20)),
        max_concurrent_jobs=max(1, _int(raw.get("max_concurrent_jobs"), 1)),
        vary_copy_default=_bool(raw.get("vary_copy"), False),
        max_attempts=max(1, _int(raw.get("max_attempts"), 3)),
        connect_ttl_seconds=max(60, _int(raw.get("connect_ttl_seconds"), 900)),
    )
