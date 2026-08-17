"""Tests del modulo apps: Application CRUD + lifecycle."""
import pytest


@pytest.fixture
def app_payload():
    return {"name": "Notas", "description": "Backend principal"}


class TestApplicationCreate:
    def test_create_assigns_slug(self, client, app_payload):
        r = client.post("/api/apps", json=app_payload)
        assert r.status_code == 201
        data = r.get_json()
        assert data["slug"] == "notas"
        assert data["name"] == "Notas"
        assert data["namespace"] == "notas"

    def test_create_duplicate_name_returns_409(self, client, app_payload):
        client.post("/api/apps", json=app_payload)
        r = client.post("/api/apps", json=app_payload)
        assert r.status_code == 409

    def test_create_strips_special_chars(self, client):
        r = client.post("/api/apps", json={"name": "Mi App #1"})
        assert r.status_code == 201
        assert r.get_json()["slug"] == "mi-app-1"

    def test_create_empty_name_returns_400(self, client):
        r = client.post("/api/apps", json={"name": ""})
        assert r.status_code in (400, 422)

    def test_create_invalid_docker_image_base_returns_400(self, client):
        r = client.post("/api/apps", json={
            "name": "Foo",
            "docker_image_base": "!!!@@@"
        })
        # Validacion Pydantic responde 422 (mismo patron que el resto del API).
        assert r.status_code in (400, 422)


class TestApplicationListGet:
    def test_list_pagination(self, client):
        for i in range(3):
            client.post("/api/apps", json={"name": f"App{i}"})
        r = client.get("/api/apps?page=1&limit=2")
        assert r.status_code == 200
        data = r.get_json()
        assert data["total"] == 3
        assert len(data["items"]) == 2

    def test_get_existing(self, client, app_payload):
        created = client.post("/api/apps", json=app_payload).get_json()
        r = client.get(f"/api/apps/{created['id']}")
        assert r.status_code == 200
        assert r.get_json()["slug"] == "notas"

    def test_get_404(self, client):
        r = client.get("/api/apps/99999")
        assert r.status_code == 404


class TestApplicationUpdate:
    def test_update_description(self, client, app_payload):
        created = client.post("/api/apps", json=app_payload).get_json()
        r = client.put(f"/api/apps/{created['id']}",
                       json={"description": "Nuevo texto"})
        assert r.status_code == 200
        assert r.get_json()["description"] == "Nuevo texto"

    def test_update_github_repo_url_manual_override(self, client, app_payload):
        created = client.post("/api/apps", json=app_payload).get_json()
        r = client.put(f"/api/apps/{created['id']}", json={
            "github_repo_url": "https://github.com/other-org/repo"
        })
        assert r.status_code == 200
        assert r.get_json()["github_repo_url"] == "https://github.com/other-org/repo"

    def test_update_docker_image_base_manual_override(self, client, app_payload):
        created = client.post("/api/apps", json=app_payload).get_json()
        r = client.put(f"/api/apps/{created['id']}", json={
            "docker_image_base": "custom/namespace/app"
        })
        assert r.status_code == 200
        assert r.get_json()["docker_image_base"] == "custom/namespace/app"


class TestApplicationDelete:
    def test_delete_hides_from_list(self, client, app_payload):
        created = client.post("/api/apps", json=app_payload).get_json()
        client.delete(f"/api/apps/{created['id']}")
        listed = client.get("/api/apps").get_json()["items"]
        assert all(a["id"] != created["id"] for a in listed)

    def test_delete_then_get_returns_404(self, client, app_payload):
        created = client.post("/api/apps", json=app_payload).get_json()
        client.delete(f"/api/apps/{created['id']}")
        r = client.get(f"/api/apps/{created['id']}")
        assert r.status_code == 404

class TestApplicationHardDeleteReuseSlug:
    """Hard delete libera name+slug para re-crear."""

    def test_can_recreate_app_with_same_name_after_delete(self, client, app_payload):
        r1 = client.post("/api/apps", json=app_payload).get_json()
        d = client.delete(f"/api/apps/{r1['id']}")
        assert d.status_code == 200
        # Mismo name deberia poder crearse de nuevo (ya no hay unique conflict).
        r2 = client.post("/api/apps", json=app_payload)
        assert r2.status_code == 201, r2.get_json()
        new = r2.get_json()
        assert new["slug"] == r1["slug"]
        assert new["name"] == r1["name"]
