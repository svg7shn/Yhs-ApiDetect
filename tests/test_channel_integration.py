"""Offline integration contract: no external requests or credentials."""
from pathlib import Path

from fastapi.testclient import TestClient

from web.server import app


def test_channel_page_and_navigation():
    with TestClient(app) as client:
        response = client.get("/channel")
        assert response.status_code == 200
        text = response.text
        assert "渠道甄选检测" in text
        assert text.index('href="/authenticity"') < text.index('href="/channel"') < text.index('href="/image"')
        assert "/static/channel.js" in text
        assert "/static/channel.css" in text
        assert client.get("/authenticity").status_code == 200


def test_channel_sources_are_separate():
    with TestClient(app) as client:
        data = client.get("/api/channel/sources").json()
        sources = data["sources"]
        assert [sum(s["family"] == f for s in sources) for f in ("claude", "gpt", "gemini")] == [7, 2, 2]
        assert len({s["id"] for s in sources}) == 11
        assert client.get("/api/authenticity/sources").status_code == 200


def test_existing_authenticity_routes_and_no_core_imports():
    paths = {route.path for route in app.routes}
    assert {"/authenticity", "/api/authenticity/records", "/channel", "/api/channel/records"} <= paths
    for file in Path("web").glob("channel_*.py"):
        text = file.read_text(encoding="utf-8")
        assert "import authenticity" not in text
        assert "from .authenticity" not in text
