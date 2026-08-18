"""Tests del timeline de provision de apps (estado + eventos repos)."""
from datetime import datetime, timedelta, timezone

import jwt
import pytest


def _jwt(app, sub="user-1"):
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": sub,
            "email": f"{sub}@example.com",
            "name": sub,
            "iat": now,
            "exp": now + timedelta(hours=1),
        },
        app.config["SECRET_KEY"],
        algorithm=app.config["JWT_ALGORITHM"],
    )


@pytest.fixture
def auth(app):
    def _make(sub="user-1"):
        return {"Authorization": f"Bearer {_jwt(app, sub)}"}
    return _make


@pytest.fixture
def ws(client, auth):
    r = client.post("/api/workspaces", json={"name": "Mi Workspace"}, headers=auth())
    assert r.status_code == 201, r.get_json()
    return r.get_json()


class TestAppProvisionEvents:
    def test_create_app_with_workspace_records_events_and_ok(self, client, auth, ws):
        # Sin PATs (TestConfig los vacia): github no pedido -> error, GHCR ok
        # por defecto, namespace K8s ok (no-op en tests: cluster mocked),
        # Jenkins job ok (create_job mockeado en este test suite).
        r = client.post("/api/apps", json={"name": "Notas", "workspace_id": ws["id"]}, headers=auth())
        assert r.status_code == 201, r.get_json()
        app = r.get_json()
        assert app["workspace_id"] == ws["id"]
        assert app["status"] == "error"
        events = app["events"]
        # 4 eventos: github_repo, ghcr_repo, k8s_namespace, jenkins_job.
        assert len(events) == 4
        assert events[0]["event"] == "github_repo"
        assert events[0]["status"] == "error"
        assert events[1]["event"] == "ghcr_repo"
        assert events[1]["status"] == "ok"
        assert events[2]["event"] == "k8s_namespace"
        assert events[2]["status"] == "ok"
        assert events[3]["event"] == "jenkins_job"
        assert events[3]["status"] == "ok"

    def test_create_app_with_manual_repos_is_ok(self, client, auth, ws):
        # Con repos provistos manualmente, todos los checks son ok.
        r = client.post("/api/apps", json={
            "name": "Manual",
            "workspace_id": ws["id"],
            "github_repo_url": "https://github.com/laurel-applications/laurel_manual",
            "docker_image_base": "ghcr.io/laurel-applications/laurel_manual",
        }, headers=auth())
        assert r.status_code == 201, r.get_json()
        app = r.get_json()
        assert app["status"] == "ok"
        statuses = {e["event"]: e["status"] for e in app["events"]}
        assert statuses == {
            "github_repo": "ok",
            "ghcr_repo": "ok",
            "k8s_namespace": "ok",
            "jenkins_job": "ok",
        }

    def test_events_endpoint_returns_timeline(self, client, auth, ws):
        r = client.post("/api/apps", json={"name": "Notas", "workspace_id": ws["id"]}, headers=auth())
        app_id = r.get_json()["id"]
        r = client.get(f"/api/apps/{app_id}/events", headers=auth())
        assert r.status_code == 200
        items = r.get_json()["items"]
        assert len(items) == 4

    def test_list_apps_filters_by_workspace(self, client, auth, ws):
        ws2 = client.post("/api/workspaces", json={"name": "Otro"}, headers=auth()).get_json()
        client.post("/api/apps", json={"name": "AppA", "workspace_id": ws["id"]}, headers=auth())
        client.post("/api/apps", json={"name": "AppB", "workspace_id": ws2["id"]}, headers=auth())
        r = client.get(f"/api/apps?workspace_id={ws['id']}", headers=auth())
        items = r.get_json()["items"]
        assert len(items) == 1
        assert items[0]["name"] == "AppA"


class TestAppDeletionLog:
    def test_delete_creates_deletion_log_with_snapshot(self, client, auth, ws):
        r = client.post("/api/apps", json={"name": "Logs", "workspace_id": ws["id"]}, headers=auth())
        app_id = r.get_json()["id"]
        rd = client.delete(f"/api/apps/{app_id}", headers=auth())
        assert rd.status_code == 200
        rl = client.get(f"/api/apps/{app_id}/deletion-logs", headers=auth())
        assert rl.status_code == 200
        items = rl.get_json()["items"]
        assert len(items) == 1
        snap = items[0]["snapshot"]
        assert snap["app"]["slug"] == "logs"
        assert snap["k8s_namespace"] == "user-apps-logs"
        assert "scoops" in snap and "domains" in snap and "events" in snap
        assert "k8s_resources" in snap
        assert items[0]["deleted_by"] is not None  # viene del JWT del fixture
