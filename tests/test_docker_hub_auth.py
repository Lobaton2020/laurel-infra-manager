"""Tests del flujo de auth de Docker Hub (JWT bearer)."""
import pytest
import requests

from app.modules.integrations.docker import service as docker_svc


class _Resp:
    def __init__(self, status, json_data=None, text=""):
        self.status_code = status
        self._json = json_data
        self.text = text if not json_data else ""

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


@pytest.fixture(autouse=True)
def _reset_bearer():
    docker_svc.reset_bearer_cache()
    yield
    docker_svc.reset_bearer_cache()


def test_hub_bearer_exchanges_pat_via_basic_auth_for_jwt(app, monkeypatch):
    """_hub_bearer() debe pedir /v2/auth/token con Basic auth y cachear el JWT."""
    app.config["DOCKER_HUB_TOKEN"] = "secret-pat"
    app.config["DOCKER_HUB_NAMESPACE"] = "aflobaton"

    calls = []

    def fake_get(url, auth=None, timeout=None, **kwargs):
        calls.append({"url": url, "auth": auth})
        assert auth == ("aflobaton", "secret-pat"), "Basic auth esperaba (namespace, pat)"
        return _Resp(200, {"token": "jwt-abc.def.ghi"})

    monkeypatch.setattr(docker_svc.requests, "get", fake_get)

    token = docker_svc._hub_bearer()

    assert token == "jwt-abc.def.ghi"
    assert calls == [{"url": "https://hub.docker.com/v2/auth/token", "auth": ("aflobaton", "secret-pat")}]
    # Segunda llamada dentro del TTL -> sin nueva request.
    assert docker_svc._hub_bearer() == "jwt-abc.def.ghi"
    assert len(calls) == 1


def test_create_empty_repo_uses_jwt_bearer_not_pat(app, monkeypatch):
    """create_empty_repo debe usar el JWT como Bearer, no el PAT crudo."""
    app.config["DOCKER_HUB_TOKEN"] = "raw-pat"
    app.config["DOCKER_HUB_NAMESPACE"] = "aflobaton"

    calls = []

    def fake_get(url, auth=None, timeout=None, **kwargs):
        calls.append({"url": url, "auth": auth})
        return _Resp(200, {"token": "jwt-xyz"})

    def fake_post(url, headers=None, json=None, timeout=None, **kwargs):
        calls.append({"url": url, "headers": headers, "json": json})
        return _Resp(
            201,
            {"namespace": "aflobaton", "name": "laurel_app1",
             "full_name": "aflobaton/laurel_app1", "is_private": False},
        )

    monkeypatch.setattr(docker_svc.requests, "get", fake_get)
    monkeypatch.setattr(docker_svc.requests, "post", fake_post)

    result = docker_svc.DockerHubService.create_empty_repo("app1")

    assert result["full_name"] == "aflobaton/laurel_app1"
    # 1: auth/token (Basic auth); 2: POST repo (Bearer JWT).
    assert calls[0]["url"] == "https://hub.docker.com/v2/auth/token"
    assert calls[0]["auth"] == ("aflobaton", "raw-pat")
    assert calls[1]["headers"]["Authorization"] == "Bearer jwt-xyz"
    assert "raw-pat" not in calls[1]["headers"]["Authorization"]
    assert calls[1]["url"] == "https://hub.docker.com/v2/repositories/aflobaton/"


def test_hub_bearer_falls_back_to_pat_when_auth_token_fails(app, monkeypatch):
    """Si /v2/auth/token falla, fallback al PAT crudo (compatibilidad)."""
    app.config["DOCKER_HUB_TOKEN"] = "pat-x"
    app.config["DOCKER_HUB_NAMESPACE"] = "aflobaton"

    def fake_get(url, auth=None, timeout=None, **kwargs):
        return _Resp(500, text="boom")

    monkeypatch.setattr(docker_svc.requests, "get", fake_get)

    assert docker_svc._hub_bearer() == "pat-x"


def test_hub_bearer_returns_none_without_pat(app):
    app.config["DOCKER_HUB_TOKEN"] = ""
    app.config["DOCKER_HUB_NAMESPACE"] = "aflobaton"
    assert docker_svc._hub_bearer() is None
