"""The compose file must start the API, the WebUI, and the bot together."""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def test_compose_runs_api_webui_and_bot():
    document = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    services = document["services"]
    assert set(services) >= {"api", "webui", "bot"}
    assert services["api"]["command"] == ["python3", "main.py"]
    assert services["bot"]["command"] == ["python3", "-m", "app.telegram"]
    assert "streamlit" in services["webui"]["command"]
    assert services["bot"]["environment"]["MPT_API_BASE_URL"] == "http://api:8080"
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "imagemagick" in dockerfile
    assert "ffmpeg" in dockerfile
    assert "python-telegram-bot" in (ROOT / "requirements.txt").read_text()
