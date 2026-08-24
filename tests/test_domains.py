"""Tests del modulo domains: Domain CRUD + lifecycle basico."""
import pytest
from unittest.mock import patch


@pytest.fixture
def fake_cluster():
    """Mock de K8sService para que domain deploy no toque cluster real."""
    from contextlib import contextmanager

    @contextmanager
    def _patch():
        # Mockeamos las operaciones K8s que toca domain deploy
        with patch("app.modules.cluster.service.K8sService.namespace_exists", return_value=True), \
             patch("app.modules.cluster.service.K8sService.exists", return_value=False), \
             patch("app.modules.cluster.service.K8sService.create", return_value={}), \
             patch("app.modules.cluster.service.K8sService.replace", return_value={}), \
             patch("app.modules.cluster.service.K8sService.delete", return_value={"deleted": True}), \
             patch("app.modules.dns.service.ClusterDNSService.add", return_value="added"), \
             patch("app.modules.dns.service.ClusterDNSService.remove", return_value="removed"):
            yield
    return _patch


@pytest.fixture
def app_and_scoop(client):
    """Crea una Application + Scoop tipo api para usar como base."""
    from app.modules.apps.model import Application
    from app.core.db import db

    app_obj = Application(name="Notas", slug="notas", description="test")
    db.session.add(app_obj)
    db.session.commit()

    # Solo `application_id` es obligatorio: ScoopService.create deriva `application`
    # (slug) desde app_record si el caller no lo manda. Sin hack SQL ni string redundante.
    payload = {
        "name": "webapp", "type": "api",
        "url_registry": "aflobaton/notas:latest",
        "application_id": app_obj.id,
    }
    created = client.post("/api/scoops", json=payload).get_json()

    return app_obj.id, created["id"]


class TestDomainCreate:
    def test_create_ok(self, client, app_and_scoop):
        app_id, scoop_id = app_and_scoop
        r = client.post("/api/domains", json={
            "application_id": app_id,
            "scoop_id": scoop_id,
            "host": "notas.resto.com",
        })
        assert r.status_code == 201
        data = r.get_json()
        assert data["host"] == "notas.resto.com"
        assert data["secret_name"] == "notas-resto-com"
        assert data["namespace"] == "user-apps-notas"

    def test_create_invalid_host(self, client, app_and_scoop):
        app_id, scoop_id = app_and_scoop
        r = client.post("/api/domains", json={
            "application_id": app_id,
            "scoop_id": scoop_id,
            "host": "notas resto com",
        })
        assert r.status_code in (400, 422)

    def test_create_duplicate_host(self, client, app_and_scoop):
        app_id, scoop_id = app_and_scoop
        client.post("/api/domains", json={
            "application_id": app_id, "scoop_id": scoop_id,
            "host": "notas.resto.com",
        })
        r = client.post("/api/domains", json={
            "application_id": app_id, "scoop_id": scoop_id,
            "host": "notas.resto.com",
        })
        assert r.status_code == 409


class TestDomainScoopValidation:
    def test_worker_scoop_rejected(self, client, app_and_scoop):
        """Crea un worker scoop vinculado al app_id via SQL directo y
        verifica que Domain service rechace por tipo != api."""
        from app.modules.scoops.model import Scoop
        from app.core.db import db

        app_id, _ = app_and_scoop
        worker = Scoop(
            name="mcp", application="notas", type="worker",
            url_registry="aflobaton/mcp:latest",
            application_id=app_id,
        )
        db.session.add(worker)
        db.session.commit()
        r = client.post("/api/domains", json={
            "application_id": app_id,
            "scoop_id": worker.id,
            "host": "worker.resto.com",
        })
        assert r.status_code == 400
        assert "api" in r.get_json()["error"].lower()


class TestDomainListGet:
    def test_list_empty(self, client):
        r = client.get("/api/domains")
        assert r.status_code == 200
        assert r.get_json()["total"] == 0

    def test_list_filter_by_application(self, client, app_and_scoop):
        app_id, scoop_id = app_and_scoop
        client.post("/api/domains", json={
            "application_id": app_id, "scoop_id": scoop_id,
            "host": "notas.resto.com",
        })
        r = client.get(f"/api/domains?application_id={app_id}")
        assert r.status_code == 200
        assert r.get_json()["total"] == 1


class TestDomainUpdateDelete:
    def test_update_host(self, client, app_and_scoop):
        app_id, scoop_id = app_and_scoop
        d = client.post("/api/domains", json={
            "application_id": app_id, "scoop_id": scoop_id,
            "host": "old.resto.com",
        }).get_json()
        r = client.put(f"/api/domains/{d['id']}", json={"host": "new.resto.com"})
        assert r.status_code == 200
        data = r.get_json()
        assert data["host"] == "new.resto.com"
        assert data["secret_name"] == "new-resto-com"

    def test_update_tls(self, client, app_and_scoop):
        app_id, scoop_id = app_and_scoop
        d = client.post("/api/domains", json={
            "application_id": app_id, "scoop_id": scoop_id,
            "host": "tls.resto.com",
        }).get_json()
        r = client.put(f"/api/domains/{d['id']}", json={"tls": False})
        assert r.status_code == 200
        assert r.get_json()["tls"] is False

    def test_delete_soft(self, client, app_and_scoop, fake_cluster):
        app_id, scoop_id = app_and_scoop
        d = client.post("/api/domains", json={
            "application_id": app_id, "scoop_id": scoop_id,
            "host": "gone.resto.com",
        }).get_json()
        with fake_cluster():
            r = client.delete(f"/api/domains/{d['id']}")
        assert r.status_code == 200
        # Verifica que no aparece en el listado
        listed = client.get("/api/domains").get_json()["items"]
        assert all(x["id"] != d["id"] for x in listed)


class TestDomainDeploy:
    def test_deploy_creates_resources(self, client, app_and_scoop, fake_cluster):
        app_id, scoop_id = app_and_scoop
        d = client.post("/api/domains", json={
            "application_id": app_id, "scoop_id": scoop_id,
            "host": "deploy.resto.com",
        }).get_json()
        with fake_cluster():
            r = client.post(f"/api/domains/{d['id']}/deploy")
        assert r.status_code == 200
        data = r.get_json()
        assert data["host"] == "deploy.resto.com"
        assert data["namespace"] == "user-apps-notas"
        kinds = [res["kind"] for res in data["resources"]]
        assert "Ingress" in kinds
        assert "Certificate" in kinds
        assert data["dns_override"] == "added"

    def test_undeploy_removes_resources(self, client, app_and_scoop, fake_cluster):
        app_id, scoop_id = app_and_scoop
        d = client.post("/api/domains", json={
            "application_id": app_id, "scoop_id": scoop_id,
            "host": "undeploy.resto.com",
        }).get_json()
        with fake_cluster():
            client.post(f"/api/domains/{d['id']}/deploy")
            r = client.delete(f"/api/domains/{d['id']}/deploy")
        assert r.status_code == 200
        data = r.get_json()
        kinds = [res["kind"] for res in data["resources"]]
        assert "Certificate" in kinds
        assert "Ingress" in kinds
        assert data["dns_cleanup"] == "removed"