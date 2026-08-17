"""Tests del modulo domain_pool: CRUD del catalogo de dominios de segundo nivel."""
import pytest


def _create(client, domain="andreslobaton.top", description="mi dominio"):
    return client.post("/api/domain-pool", json={
        "domain": domain, "description": description,
    })


class TestCreate:
    def test_create_ok(self, client):
        r = _create(client)
        assert r.status_code == 201
        data = r.get_json()
        assert data["domain"] == "andreslobaton.top"
        assert data["description"] == "mi dominio"
        assert data["id"] > 0

    def test_create_normalizes_lowercase(self, client):
        r = _create(client, domain="AndresLobaton.TOP")
        assert r.status_code == 201
        assert r.get_json()["domain"] == "andreslobaton.top"

    def test_create_invalid_domain(self, client):
        for bad in (
            "andreslobaton",             # sin punto (TLD)
            "not a domain",              # espacios
            "http://andreslobaton.top",  # protocolo
            "andres_lobaton.top",        # underscore
            "sub.andreslobaton.top",     # es valido como FQDN, se permite (no se rechaza)
        ):
            r = _create(client, domain=bad)
            if bad == "sub.andreslobaton.top":
                assert r.status_code == 201, bad
            else:
                assert r.status_code in (400, 422), bad

    def test_create_duplicate(self, client):
        _create(client)
        r = _create(client)
        assert r.status_code == 409


class TestList:
    def test_list_empty(self, client):
        r = client.get("/api/domain-pool")
        assert r.status_code == 200
        assert r.get_json()["items"] == []

    def test_list_items(self, client):
        _create(client, "andreslobaton.top")
        _create(client, "otro.top")
        r = client.get("/api/domain-pool")
        assert r.status_code == 200
        items = r.get_json()["items"]
        assert len(items) == 2
        assert {i["domain"] for i in items} == {"andreslobaton.top", "otro.top"}


class TestUpdate:
    def test_update_description(self, client):
        d = _create(client).get_json()
        r = client.put(f"/api/domain-pool/{d['id']}", json={"description": "nueva"})
        assert r.status_code == 200
        data = r.get_json()
        assert data["description"] == "nueva"
        assert data["domain"] == "andreslobaton.top"

    def test_update_not_found(self, client):
        r = client.put("/api/domain-pool/999", json={"description": "x"})
        assert r.status_code == 404


class TestDelete:
    def test_delete_ok(self, client):
        d = _create(client).get_json()
        r = client.delete(f"/api/domain-pool/{d['id']}")
        assert r.status_code == 200
        assert r.get_json() == {"deleted": True}
        listed = client.get("/api/domain-pool").get_json()["items"]
        assert all(x["id"] != d["id"] for x in listed)

    def test_delete_not_found(self, client):
        r = client.delete("/api/domain-pool/999")
        assert r.status_code == 404

    def test_delete_blocked_when_host_in_use(self, client):
        from app.core.db import db
        from app.modules.domains.model import Domain

        d = _create(client).get_json()
        db.session.add(Domain(
            application_id=1, scoop_id=1, host="notas.andreslobaton.top",
            secret_name="notas-andreslobaton-top",
        ))
        db.session.commit()
        r = client.delete(f"/api/domain-pool/{d['id']}")
        assert r.status_code == 409
        assert "andreslobaton.top" in r.get_json()["error"]
        # Sigue listado: no se borro.
        listed = client.get("/api/domain-pool").get_json()["items"]
        assert any(x["id"] == d["id"] for x in listed)
