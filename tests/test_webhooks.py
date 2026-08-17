"""Tests del webhook GitHub -> bump de version + trigger Jenkins."""
import hashlib
import hmac
import json

import pytest

from app.core.errors import AppError
from app.modules.integrations.jenkins.service import JenkinsService
from app.modules.webhooks import service as webhook_service

SECRET = "test-secret"


def _sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _push_payload(slug: str, ref: str = "refs/heads/master", sha: str = "a" * 40) -> dict:
    return {
        "ref": ref,
        "after": sha,
        "head_commit": {"id": sha},
        "repository": {
            "name": f"laurel_{slug}",
            "full_name": f"laurel-applications/laurel_{slug}",
        },
    }


def _post(client, payload: dict, secret: str = SECRET, signature: str | None = None):
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if signature is None:
        headers["X-Hub-Signature-256"] = _sign(body, secret)
    else:
        headers["X-Hub-Signature-256"] = signature
    return client.post("/api/webhooks/github", data=body, headers=headers)


def _create_app_and_scoop(client) -> dict:
    r = client.post("/api/apps", json={"name": "Portafolio Web"})
    assert r.status_code == 201
    app_data = r.get_json()
    r = client.post("/api/scoops", json={
        "name": "portafolio",
        "application": "portafolio-web",
        "application_id": app_data["id"],
        "type": "api",
        "version": "1.4.2",
        "url_registry": "ghcr.io/lobaton/portafolio:latest",
        "requested_vcpu": "100m",
        "requested_memory": "128Mi",
        "limit_vcpu": "500m",
        "limit_memory": "512Mi",
        "min_replicas": 1,
        "max_replicas": 1,
    })
    assert r.status_code == 201, r.get_json()
    return app_data


class TestBumpVersion:
    def test_semver_patch_bump(self):
        assert webhook_service._bump_version("1.2.3") == "1.2.4"

    def test_non_semver_uses_short_sha(self):
        assert webhook_service._bump_version("v1", "abcdef0123456") == "abcdef0"

    def test_non_semver_without_sha_keeps_current(self):
        assert webhook_service._bump_version("v1", "") == "v1"


class TestVerifySignature:
    def test_valid_signature(self):
        body = b"hola"
        assert webhook_service._verify_signature(SECRET, body, _sign(body)) is True

    def test_invalid_signature(self):
        assert webhook_service._verify_signature(SECRET, b"hola", "sha256=deadbeef") is False

    def test_empty_header(self):
        assert webhook_service._verify_signature(SECRET, b"hola", "") is False


class TestWebhookEndpoint:
    def test_secret_not_configured_returns_503(self, app, client):
        app.config["GITHUB_WEBHOOK_SECRET"] = ""
        r = _post(client, _push_payload("portafolio-web"), secret="")
        assert r.status_code == 503
        assert "not configured" in r.get_json()["error"]

    def test_invalid_signature_returns_401(self, app, client):
        app.config["GITHUB_WEBHOOK_SECRET"] = SECRET
        r = _post(client, _push_payload("portafolio-web"), signature="sha256=0" * 4)
        assert r.status_code == 401

    def test_valid_push_bumps_scoops_and_triggers_jenkins(self, app, client, monkeypatch):
        app.config["GITHUB_WEBHOOK_SECRET"] = SECRET
        _create_app_and_scoop(client)
        triggered = {}

        def _fake_trigger(slug, tag):
            triggered["slug"] = slug
            triggered["tag"] = tag
            return {"job": f"laurel_{slug}", "url": f"http://jenkins:8080/job/laurel_{slug}"}

        monkeypatch.setattr(JenkinsService, "trigger_build", _fake_trigger)
        r = _post(client, _push_payload("portafolio-web", sha="b" * 40))
        assert r.status_code == 200
        data = r.get_json()
        assert data["received"] is True
        assert data["app"] == "portafolio-web"
        assert data["new_version"] == "1.4.3"
        assert data["jenkins"] == {
            "triggered": True,
            "job": "laurel_portafolio-web",
            "url": "http://jenkins:8080/job/laurel_portafolio-web",
        }
        assert triggered == {"slug": "portafolio-web", "tag": "1.4.3"}

    def test_jenkins_failure_returns_200_with_error(self, app, client, monkeypatch):
        app.config["GITHUB_WEBHOOK_SECRET"] = SECRET
        _create_app_and_scoop(client)

        def _boom(slug, tag):
            raise AppError(f"Jenkins job 'laurel_{slug}' not found", status_code=404)

        monkeypatch.setattr(JenkinsService, "trigger_build", _boom)
        r = _post(client, _push_payload("portafolio-web"))
        assert r.status_code == 200
        data = r.get_json()
        assert data["new_version"] == "1.4.3"
        assert data["jenkins"]["triggered"] is False
        assert "not found" in data["jenkins"]["error"]

    def test_non_master_push_skipped(self, app, client, monkeypatch):
        app.config["GITHUB_WEBHOOK_SECRET"] = SECRET
        # no se toca Jenkins ni se bumpa nada
        def _unexpected(*a, **k):
            raise AssertionError("no debe dispararse jenkins")
        monkeypatch.setattr(JenkinsService, "trigger_build", _unexpected)
        r = _post(client, _push_payload("portafolio-web", ref="refs/heads/develop"))
        assert r.status_code == 200
        data = r.get_json()
        assert data["ref"] == "refs/heads/develop"
        assert data["skipped"] == "not master"

    def test_unknown_app_returns_200_skipped(self, app, client, monkeypatch):
        app.config["GITHUB_WEBHOOK_SECRET"] = SECRET
        def _unexpected(*a, **k):
            raise AssertionError("no debe dispararse jenkins")
        monkeypatch.setattr(JenkinsService, "trigger_build", _unexpected)
        r = _post(client, _push_payload("app-no-gestionada"))
        assert r.status_code == 200
        data = r.get_json()
        assert data["skipped"] == "unknown app"
        assert data["app"] == "app-no-gestionada"