"""The compose file must start the API, the WebUI, and the bot together."""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def test_compose_runs_api_webui_and_bot():
    document = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    services = document["services"]
    assert document["name"] == "moneyprinterturbo"
    assert set(services) >= {"api", "webui", "bot", "cloudflared"}
    assert services["api"]["command"] == ["python3", "main.py"]
    assert services["bot"]["command"] == ["python3", "-m", "app.telegram"]
    assert "streamlit" in services["webui"]["command"]
    assert services["bot"]["environment"]["MPT_API_BASE_URL"] == "http://api:8080"
    assert services["api"]["mem_limit"] == "${MPT_API_MEM_LIMIT:-1536m}"
    assert services["api"]["cpus"] == "${MPT_API_CPUS:-1.0}"
    assert services["api"]["pids_limit"] == "${MPT_API_PIDS_LIMIT:-256}"
    assert services["api"]["logging"]["options"]["max-size"] == "${MPT_LOG_MAX_SIZE:-10m}"
    assert services["api"]["environment"]["MAX_CONCURRENT_TASKS"] == "${MAX_CONCURRENT_TASKS:-1}"
    assert services["api"]["environment"]["MPT_LOW_MEMORY"] == "${MPT_LOW_MEMORY:-true}"
    assert services["api"]["ports"] == ["127.0.0.1:8080:8080"]
    assert services["cloudflared"]["profiles"] == ["tunnel"]
    assert "CLOUDFLARE_TUNNEL_TOKEN" in services["cloudflared"]["command"][-1]
    assert document["volumes"]["mpt-data"] is None or isinstance(document["volumes"]["mpt-data"], dict)
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "imagemagick" in dockerfile
    assert "ffmpeg" in dockerfile
    assert "linux/arm64" in dockerfile
    assert "python:3.11-slim-bookworm" in dockerfile
    assert "bookworm-security" in dockerfile
    assert services["api"]["environment"]["TELEGRAM_ENABLED"] == "${TELEGRAM_ENABLED:-true}"
    assert services["api"]["environment"]["VIDEO_SOURCES"] == "${VIDEO_SOURCES:-pixabay,openverse,wikimedia}"
    assert services["api"]["environment"]["VIDEO_SOURCE"] == "${VIDEO_SOURCE:-pixabay}"
    assert "python-telegram-bot" in (ROOT / "requirements.txt").read_text()
