"""Tests del modulo workspaces: CRUD scoped por usuario + integracion con apps."""
from datetime import datetime, timedelta, timezone

import jwt
import pytest


def _jwt(app, sub):
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
    """Header de auth con un JWT valido; sub parametrizable."""
    def _make(sub="user-1"):
        return {"Authorization": f"Bearer {_jwt(app, sub)}"}
    return _make


def _create_workspace(client, auth, name, description=None):
    payload = {"name": name}
    if description is not None:
        payload["description"] = description
    return client.post("/api/workspaces", json=payload, headers=auth())


class TestWorkspaceCreate:
    def test_create_assigns_slug_and_owner_sub(self, client, auth):
        r = _create_workspace(client, auth, "Mi Workspace #1")
        assert r.status_code == 201
        data = r.get_json()
        assert data["slug"] == "mi-workspace-1"
        assert data["owner_sub"] == "user-1"
        assert data["apps_count"] == 0

    def test_create_owner_sub_from_jwt_not_body(self, client, auth):
        r = client.post("/api/workspaces", json={
            "name": "X", "owner_sub": "user-999"
        }, headers=auth("user-2"))
        assert r.status_code == 201
        assert r.get_json()["owner_sub"] == "user-2"

    def test_create_duplicate_name_returns_409(self, client, auth):
        _create_workspace(client, auth, "Notas")
        r = _create_workspace(client, auth, "Notas")
        assert r.status_code == 409

    def test_create_duplicate_slug_returns_409(self, client, auth):
        _create_workspace(client, auth, "Mi Notas")
        r = _create_workspace(client, auth, "Mi  Notas")
        assert r.status_code == 409

    def test_create_requires_auth(self, client):
        r = client.post("/api/workspaces", json={"name": "NoAuth"})
        assert r.status_code == 401

    def test_create_empty_name_returns_422(self, client, auth):
        r = _create_workspace(client, auth, "")
        assert r.status_code in (400, 422)


class TestWorkspaceList:
    def test_list_only_returns_own(self, client, auth):
        _create_workspace(client, auth, "De A", description="a")
        _create_workspace(client, auth, "De B", description="b")
        own = client.get("/api/workspaces", headers=auth("user-1")).get_json()
        assert own["total"] == 2
        assert {w["name"] for w in own["items"]} == {"De A", "De B"}

        other = client.get("/api/workspaces", headers=auth("user-2")).get_json()
        assert other["total"] == 0

    def test_list_pagination(self, client, auth):
        for i in range(3):
            _create_workspace(client, auth, f"Ws{i}")
        r = client.get("/api/workspaces?page=1&limit=2", headers=auth())
        assert r.status_code == 200
        data = r.get_json()
        assert data["total"] == 3
        assert len(data["items"]) == 2

    def test_list_requires_auth(self, client):
        assert client.get("/api/workspaces").status_code == 401


class TestWorkspaceGet:
    def test_get_own(self, client, auth):
        created = _create_workspace(client, auth, "Notas").get_json()
        r = client.get(f"/api/workspaces/{created['id']}", headers=auth())
        assert r.status_code == 200
        assert r.get_json()["name"] == "Notas"

    def test_get_other_users_returns_404(self, client, auth):
        created = _create_workspace(client, auth, "Secreto").get_json()
        r = client.get(f"/api/workspaces/{created['id']}", headers=auth("user-2"))
        assert r.status_code == 404

    def test_get_missing_returns_404(self, client, auth):
        assert client.get("/api/workspaces/99999", headers=auth()).status_code == 404


class TestWorkspaceUpdate:
    def test_update_renames_and_derives_slug(self, client, auth):
        created = _create_workspace(client, auth, "Viejo Nombre").get_json()
        r = client.put(
            f"/api/workspaces/{created['id']}",
            json={"name": "Nuevo Nombre", "description": "texto"},
            headers=auth(),
        )
        assert r.status_code == 200
        data = r.get_json()
        assert data["name"] == "Nuevo Nombre"
        assert data["slug"] == "nuevo-nombre"
        assert data["description"] == "texto"

    def test_update_duplicate_name_returns_409(self, client, auth):
        _create_workspace(client, auth, "Primero")
        segundo = _create_workspace(client, auth, "Segundo").get_json()
        r = client.put(
            f"/api/workspaces/{segundo['id']}",
            json={"name": "Primero"},
            headers=auth(),
        )
        assert r.status_code == 409

    def test_update_own_slug_is_ok(self, client, auth):
        created = _create_workspace(client, auth, "Token").get_json()
        r = client.put(
            f"/api/workspaces/{created['id']}",
            json={"name": "Token"},
            headers=auth(),
        )
        assert r.status_code == 200

    def test_update_other_users_returns_404(self, client, auth):
        created = _create_workspace(client, auth, "Ajeno").get_json()
        r = client.put(
            f"/api/workspaces/{created['id']}",
            json={"name": "Hacked"},
            headers=auth("user-2"),
        )
        assert r.status_code == 404


class TestWorkspaceDelete:
    def test_soft_delete_hides_from_list(self, client, auth):
        created = _create_workspace(client, auth, "Vanish").get_json()
        r = client.delete(f"/api/workspaces/{created['id']}", headers=auth())
        assert r.status_code == 200
        listed = client.get("/api/workspaces", headers=auth()).get_json()["items"]
        assert all(w["id"] != created["id"] for w in listed)

    def test_soft_delete_ungroups_apps(self, client, auth):
        ws = _create_workspace(client, auth, "Grupo").get_json()
        app = client.post("/api/apps", json={
            "name": "Dentro", "workspace_id": ws["id"],
        }).get_json()
        assert app["workspace_id"] == ws["id"]

        client.delete(f"/api/workspaces/{ws['id']}", headers=auth())
        after = client.get(f"/api/apps/{app['id']}").get_json()
        assert after["workspace_id"] is None

    def test_soft_delete_then_get_returns_404(self, client, auth):
        created = _create_workspace(client, auth, "Gone").get_json()
        client.delete(f"/api/workspaces/{created['id']}", headers=auth())
        assert client.get(
            f"/api/workspaces/{created['id']}", headers=auth()
        ).status_code == 404

    def test_delete_other_users_returns_404(self, client, auth):
        created = _create_workspace(client, auth, "Tuya").get_json()
        r = client.delete(f"/api/workspaces/{created['id']}", headers=auth("user-2"))
        assert r.status_code == 404


class TestWorkspaceAppAssociation:
    def test_create_app_with_workspace_and_apps_count(self, client, auth):
        ws = _create_workspace(client, auth, "Pesado").get_json()
        client.post("/api/apps", json={"name": "App1", "workspace_id": ws["id"]})
        client.post("/api/apps", json={"name": "App2", "workspace_id": ws["id"]})
        got = client.get(f"/api/workspaces/{ws['id']}", headers=auth()).get_json()
        assert got["apps_count"] == 2

        listed = client.get("/api/workspaces", headers=auth()).get_json()["items"]
        assert listed[0]["apps_count"] == 2

    def test_create_app_with_invalid_workspace_returns_404(self, client, auth):
        r = client.post("/api/apps", json={"name": "Huerfana", "workspace_id": 99999})
        assert r.status_code == 404

    def test_app_can_change_workspace(self, client, auth):
        ws1 = _create_workspace(client, auth, "A").get_json()
        ws2 = _create_workspace(client, auth, "B").get_json()
        app = client.post("/api/apps", json={
            "name": "Movil", "workspace_id": ws1["id"],
        }).get_json()
        r = client.put(f"/api/apps/{app['id']}", json={"workspace_id": ws2["id"]})
        assert r.status_code == 200
        assert r.get_json()["workspace_id"] == ws2["id"]