"""Integration tests para `GET /api/apps/<slug>/next_version`.

Mockea `version_bump.next_version` para no hablar con Docker Hub real.
Cubre el caso feliz, credenciales ausentes (503) y error del registry (502).
"""

from __future__ import annotations

import pytest

from app.modules.apps import controller as apps_controller
from app.modules.integrations.docker import version_bump as vb_mod


@pytest.fixture
def app_creds(app):
    """Asegura credenciales presentes para el caso feliz (TestConfig las vacía)."""
    app.config["DOCKERHUB_USER"] = "aflobaton"
    app.config["DOCKERHUB_PASSWORD"] = "fake"
    return app


def _set_next_version(monkeypatch, value=None, raise_exc=None):
    if raise_exc is not None:
        def _fake(*a, **kw):
            raise raise_exc
        monkeypatch.setattr(vb_mod, "next_version", _fake)
    else:
        monkeypatch.setattr(vb_mod, "next_version", lambda *a, **kw: value)


def test_next_version_success(monkeypatch, app_creds, client):
    _set_next_version(monkeypatch, value="0.0.3")
    resp = client.get("/api/apps/notas-test/next_version")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body == {
        "slug": "notas-test",
        "namespace": "aflobaton",
        "image": "aflobaton/laurel_notas-test",
        "next_version": "0.0.3",
    }


def test_next_version_uses_version_bump_args(client, monkeypatch, app_creds):
    """next_version debe recibir namespace 'aflobaton' y repo 'laurel_notas-test'."""
    captured = {}

    def _capture(user, password, *, repo=None, **_):
        captured["user"] = user
        captured["password"] = password
        captured["repo"] = repo
        return "9.9.10"

    monkeypatch.setattr(vb_mod, "next_version", _capture)
    resp = client.get("/api/apps/laurel-mis-cosas/next_version")
    assert resp.status_code == 200
    assert captured == {
        "user": "aflobaton",
        "password": "fake",
        "repo": "laurel_laurel-mis-cosas",
    }


def test_next_version_no_creds_returns_503(client, app):
    # TestConfig deja DOCKERHUB_USER='' y DOCKERHUB_PASSWORD=''; el endpoint
    # debe responder 503 con reason=dockerhub_unconfigured sin llamar a Docker Hub.
    app.config["DOCKERHUB_USER"] = ""
    app.config["DOCKERHUB_PASSWORD"] = ""
    resp = client.get("/api/apps/notas-test/next_version")
    assert resp.status_code == 503
    body = resp.get_json()
    assert body.get("reason") == "dockerhub_unconfigured"
    assert "not configured" in body.get("error", "").lower()


def test_next_version_dockerhub_error_returns_502(monkeypatch, app_creds, client):
    _set_next_version(
        monkeypatch,
        raise_exc=vb_mod.DockerHubError("dockerhub login failed: HTTP 401"),
    )
    resp = client.get("/api/apps/notas-test/next_version")
    assert resp.status_code == 502
    body = resp.get_json()
    assert body.get("reason") == "dockerhub_error"
    assert "401" in body.get("error", "")


# Slugs invalidos deben rechazarse con 400 invalid_slug antes de hablar
# con Docker Hub. Flask <string:slug> ya bloquea slashes, pero el resto
# (espacios, unicode, upper-case, demasiado largo) lo cubrimos aca.
import pytest


@pytest.mark.parametrize("bad_slug", [
    "With Spaces",        # espacios
    "UPPER",              # mayusculas
    "-leading-dash",      # guion al inicio
    "trailing-dash-",     # guion al final
    "_underscore_start",  # underscore al inicio
    "a" * 64,             # 64 chars (limite es 63)
    "中文",               # unicode
    "has.dot",            # punto (no permitido en slug)
    "a.b",                # punto
    "v1.2.3",             # parece version
])
def test_next_version_invalid_slug_returns_400(client, bad_slug):
    resp = client.get(f"/api/apps/{bad_slug}/next_version")
    assert resp.status_code == 400, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body.get("reason") == "invalid_slug"


@pytest.mark.parametrize("good_slug", [
    "notas-test",
    "demo",
    "a",
    "a-b-c",
    "a_b_c",
    "x" * 63,  # limite exacto
    "app1",
])
def test_next_version_valid_slug_accepted(monkeypatch, app_creds, client, good_slug):
    _set_next_version(monkeypatch, value="1.2.3")
    resp = client.get(f"/api/apps/{good_slug}/next_version")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()["slug"] == good_slug
