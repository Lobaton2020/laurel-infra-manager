"""Tests del modulo Builds: lista, get con poll, set current_version, jenkins helpers."""
import json

import pytest

from app.core.errors import AppError
from app.modules.integrations.jenkins.service import JenkinsService


@pytest.fixture
def app_with_webhook(app):
    """Habilita el webhook de GitHub con un secret conocido para estos tests."""
    app.config["GITHUB_WEBHOOK_SECRET"] = "test-secret"
    return app


def _create_app(client, name: str = "Notas") -> dict:
    r = client.post("/api/apps", json={"name": name})
    assert r.status_code == 201, r.get_json()
    return r.get_json()


def _sign(body: bytes, secret: str = "test-secret") -> str:
    import hashlib
    import hmac
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _post_signed(client, payload: dict, secret: str = "test-secret"):
    body = json.dumps(payload).encode()
    return client.post(
        "/api/webhooks/github",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": _sign(body, secret),
        },
    )


def _push_payload(slug: str, sha: str = "a" * 40) -> dict:
    return {
        "ref": "refs/heads/master",
        "after": sha,
        "head_commit": {"id": sha},
        "repository": {
            "name": f"laurel_{slug}",
            "full_name": f"laurel-applications/laurel_{slug}",
        },
    }


class TestCurrentVersion:
    def test_default_is_zero(self, client):
        app = _create_app(client)
        assert app["current_version"] == "0.0.1"

    def test_set_current_version(self, client):
        app = _create_app(client)
        r = client.patch(
            f"/api/apps/{app['id']}/current-version",
            json={"version": "1.2.3"},
        )
        assert r.status_code == 200
        assert r.get_json()["current_version"] == "1.2.3"

    def test_set_current_version_rejects_empty(self, client):
        app = _create_app(client)
        r = client.patch(
            f"/api/apps/{app['id']}/current-version",
            json={"version": "  "},
        )
        assert r.status_code == 422

    def test_set_current_version_rejects_unsafe_chars(self, client):
        app = _create_app(client)
        r = client.patch(
            f"/api/apps/{app['id']}/current-version",
            json={"version": "1.0; rm -rf /"},
        )
        assert r.status_code == 422

    def test_set_current_version_unknown_app_returns_404(self, client):
        r = client.patch(
            "/api/apps/9999/current-version",
            json={"version": "1.0.0"},
        )
        assert r.status_code == 404


class TestListBuilds:
    def test_empty_for_new_app(self, client):
        app = _create_app(client)
        r = client.get(f"/api/apps/{app['id']}/builds")
        assert r.status_code == 200
        assert r.get_json() == {"items": []}

    def test_unknown_app_returns_404(self, client):
        r = client.get("/api/apps/9999/builds")
        assert r.status_code == 404

    def test_list_after_webhook(self, app_with_webhook, client, monkeypatch):
        app = _create_app(client)
        # Subimos current_version a 1.0.0 para que el webhook la use.
        client.patch(
            f"/api/apps/{app['id']}/current-version",
            json={"version": "1.0.0"},
        )

        def _fake_trigger(slug, tag, test_cmd=None):
            return {
                "job": f"laurel_{slug}",
                "number": 5,
                "url": f"http://jenkins/job/laurel_{slug}/5",
            }

        monkeypatch.setattr(JenkinsService, "trigger_build", _fake_trigger)
        r = _post_signed(client, _push_payload("notas", sha="d" * 40))
        assert r.status_code == 200
        build_id = r.get_json()["build_id"]
        assert build_id is not None

        list_r = client.get(f"/api/apps/{app['id']}/builds")
        assert list_r.status_code == 200
        items = list_r.get_json()["items"]
        assert len(items) == 1
        b = items[0]
        assert b["id"] == build_id
        assert b["version"] == "1.0.0"
        assert b["commit_sha"] == "d" * 40
        assert b["status"] in ("pending", "running")
        assert b["jenkins_job"] == "laurel_notas"
        assert b["jenkins_number"] == 5
        assert b["jenkins_url"] == "http://jenkins/job/laurel_notas/5"


class TestGetBuild:
    def test_get_404_unknown_build(self, client):
        app = _create_app(client)
        r = client.get(f"/api/apps/{app['id']}/builds/9999")
        assert r.status_code == 404

    def test_get_polls_jenkins_when_pending(self, app_with_webhook, client, monkeypatch):
        app = _create_app(client)
        client.patch(
            f"/api/apps/{app['id']}/current-version",
            json={"version": "1.0.0"},
        )

        monkeypatch.setattr(
            JenkinsService, "trigger_build",
            lambda slug, tag, test_cmd=None: {"job": f"laurel_{slug}", "number": 9, "url": "http://x"},
        )
        r = _post_signed(client, _push_payload("notas"))
        assert r.status_code == 200
        build_id = r.get_json()["build_id"]

        # Simulamos que Jenkins ahora reporta SUCCESS.
        monkeypatch.setattr(
            JenkinsService, "get_build_status",
            lambda slug, build_number: {
                "status": "success",
                "building": False,
                "result": "SUCCESS",
                "timestamp": 1700000000000,
            },
        )
        r = client.get(f"/api/apps/{app['id']}/builds/{build_id}")
        assert r.status_code == 200
        data = r.get_json()
        assert data["status"] == "success"
        assert data["started_at"] is not None
        assert data["finished_at"] is not None

    def test_get_does_not_poll_when_terminal(self, client, monkeypatch):
        """Un build en estado terminal no se vuelve a consultar a Jenkins."""
        from datetime import datetime, timezone

        from app.core.db import db
        from app.modules.builds.model import AppBuild
        from app.modules.builds.service import BuildsService

        app = _create_app(client)
        # Creamos un build manual en estado terminal.
        build = BuildsService.create_pending(
            app_id=app["id"],
            version="1.0.0",
            commit_sha=None,
            jenkins_job="laurel_notas",
            jenkins_number=1,
            jenkins_url="http://x",
        )
        build.status = "success"
        build.started_at = datetime.now(timezone.utc)
        build.finished_at = datetime.now(timezone.utc)
        db.session.commit()

        def _should_not_be_called(*a, **k):
            raise AssertionError("no debe pegarle a Jenkins en build terminal")

        monkeypatch.setattr(
            JenkinsService, "get_build_status", _should_not_be_called
        )
        r = client.get(f"/api/apps/{app['id']}/builds/{build.id}")
        assert r.status_code == 200
        assert r.get_json()["status"] == "success"


class TestJenkinsHelpers:
    """Cubre `_parse_build_number` y `_map_jenkins_result` via el service real."""

    def test_parse_build_number_from_full_url(self, monkeypatch):
        from app.modules.integrations.jenkins import service
        assert service._parse_build_number(
            "http://jenkins:8080/job/laurel_x/42/"
        ) == 42
        assert service._parse_build_number("/job/laurel_x/42/") == 42
        assert service._parse_build_number("queue/item/123/") == 123

    def test_parse_build_number_returns_none_for_invalid(self):
        from app.modules.integrations.jenkins import service
        assert service._parse_build_number(None) is None
        assert service._parse_build_number("") is None
        assert service._parse_build_number("http://x/") is None

    def test_map_jenkins_result(self):
        from app.modules.integrations.jenkins import service
        assert service._map_jenkins_result("SUCCESS") == "success"
        assert service._map_jenkins_result("FAILURE") == "failed"
        assert service._map_jenkins_result("UNSTABLE") == "failed"
        assert service._map_jenkins_result("ABORTED") == "aborted"
        assert service._map_jenkins_result("NOT_BUILT") == "failed"
        # result=None con building=False -> 'failed' (defensivo).
        assert service._map_jenkins_result(None) == "failed"

    def test_trigger_build_parses_number_from_location_header(self, app, monkeypatch):
        """El trigger devuelve el numero y la URL directa al build."""
        from app.modules.integrations.jenkins import service
        app.config["JENKINS_URL"] = "http://jenkins:8080"

        class _FakeResp:
            status_code = 201
            headers = {"Location": "http://jenkins:8080/job/laurel_x/77/"}
            text = ""

        monkeypatch.setattr(service.requests, "post", lambda *a, **k: _FakeResp())
        monkeypatch.setattr(service, "_get_build_token", lambda: "tok")
        result = service.JenkinsService.trigger_build("x", "1.0.0")
        assert result == {
            "job": "laurel_x",
            "number": 77,
            "url": "http://jenkins:8080/job/laurel_x/77",
        }
