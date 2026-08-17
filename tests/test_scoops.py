"""Tests del catalogo de scoops (no tocan el cluster)."""
import pytest

from app.core.errors import AppError
from app.modules.scoops import service as scoops_service


def _create_app(client, name: str) -> dict:
    resp = client.post("/api/apps", json={"name": name})
    assert resp.status_code == 201
    return resp.get_json()


class TestCreate:
    def test_create_assigns_first_free_port(self, client, scoop_payload):
        response = client.post("/api/scoops", json=scoop_payload)
        assert response.status_code == 201
        data = response.get_json()
        assert data["port"] == 3000
        assert data["namespace"] == "prod"
        assert data["status"] == "pending"
        # status_label se elimino del response: el frontend debe mapearlo localmente.

    def test_ports_are_sequential(self, client, scoop_payload):
        client.post("/api/scoops", json=scoop_payload)
        second = client.post("/api/scoops", json={**scoop_payload, "name": "tomanotas"})
        assert second.get_json()["port"] == 3001

    def test_freed_port_is_reused(self, client, scoop_payload):
        first = client.post("/api/scoops", json=scoop_payload).get_json()
        client.post("/api/scoops", json={**scoop_payload, "name": "tomanotas"})
        client.delete(f"/api/scoops/{first['id']}?force=true")

        third = client.post("/api/scoops", json={**scoop_payload, "name": "finanzas"})
        assert third.get_json()["port"] == 3000

    def test_worker_gets_no_port(self, client, scoop_payload):
        response = client.post(
            "/api/scoops", json={**scoop_payload, "name": "mailer", "type": "worker"}
        )
        assert response.status_code == 201
        assert response.get_json()["port"] is None

    def test_duplicate_name_conflicts(self, client, scoop_payload):
        client.post("/api/scoops", json=scoop_payload)
        response = client.post("/api/scoops", json=scoop_payload)
        assert response.status_code == 409

    def test_port_exhaustion(self, client, scoop_payload):
        # El rango de test es 3000-3005: 6 puertos disponibles.
        for i in range(6):
            resp = client.post("/api/scoops", json={**scoop_payload, "name": f"svc-{i}"})
            assert resp.status_code == 201

        response = client.post("/api/scoops", json={**scoop_payload, "name": "svc-x"})
        assert response.status_code == 409
        assert "puertos libres" in response.get_json()["error"]


class TestNameDerivation:
    """El form del frontend solo pide `application`, sin `name`."""

    def test_name_derived_from_application(self, client, scoop_payload):
        payload = {k: v for k, v in scoop_payload.items() if k != "name"}
        response = client.post("/api/scoops", json={**payload, "application": "Portafolio Web"})
        assert response.status_code == 201
        assert response.get_json()["name"] == "portafolio-web"

    def test_explicit_name_wins(self, client, scoop_payload):
        response = client.post(
            "/api/scoops", json={**scoop_payload, "application": "Otra App"}
        )
        assert response.get_json()["name"] == "portafolio"

    def test_derived_name_collides_as_conflict(self, client, scoop_payload):
        payload = {k: v for k, v in scoop_payload.items() if k != "name"}
        assert client.post("/api/scoops", json=payload).status_code == 201
        # Misma application -> mismo nombre derivado -> 409, no un 500.
        assert client.post("/api/scoops", json=payload).status_code == 409

    def test_application_without_valid_chars_is_rejected(self, client, scoop_payload):
        payload = {k: v for k, v in scoop_payload.items() if k != "name"}
        response = client.post("/api/scoops", json={**payload, "application": "###"})
        assert response.status_code == 422
        assert "name" in response.get_json()["details"][0]["message"]


class TestValidation:
    def test_invalid_dns_name(self, client, scoop_payload):
        response = client.post("/api/scoops", json={**scoop_payload, "name": "Mi_App"})
        assert response.status_code == 422
        assert response.get_json()["details"][0]["field"] == "name"

    def test_invalid_cpu_quantity(self, client, scoop_payload):
        response = client.post(
            "/api/scoops", json={**scoop_payload, "requested_vcpu": "medio"}
        )
        assert response.status_code == 422

    def test_invalid_memory_quantity(self, client, scoop_payload):
        response = client.post(
            "/api/scoops", json={**scoop_payload, "limit_memory_value": -1}
        )
        assert response.status_code == 422

    def test_max_replicas_below_min(self, client, scoop_payload):
        response = client.post(
            "/api/scoops",
            json={**scoop_payload, "min_replicas": 5, "max_replicas": 2},
        )
        assert response.status_code == 422

    def test_cronjob_requires_schedule(self, client, scoop_payload):
        response = client.post(
            "/api/scoops", json={**scoop_payload, "name": "backup", "type": "cronjob"}
        )
        assert response.status_code == 422
        assert "schedule" in response.get_json()["details"][0]["message"]

    def test_cronjob_with_schedule_is_valid(self, client, scoop_payload):
        response = client.post(
            "/api/scoops",
            json={**scoop_payload, "name": "backup", "type": "cronjob",
                  "schedule": "0 3 * * *"},
        )
        assert response.status_code == 201

    def test_missing_body(self, client):
        response = client.post("/api/scoops")
        assert response.status_code == 400


class TestReadUpdateDelete:
    def test_list_and_filter(self, client, scoop_payload):
        client.post("/api/scoops", json=scoop_payload)
        client.post(
            "/api/scoops",
            json={**scoop_payload, "name": "mailer", "type": "worker",
                  "application": "otra-app"},
        )

        assert client.get("/api/scoops").get_json()["total"] == 2
        assert client.get("/api/scoops?type=worker").get_json()["total"] == 1
        assert client.get("/api/scoops?application=otra-app").get_json()["total"] == 1
        assert client.get("/api/scoops?is_productive=true").get_json()["total"] == 0

    def test_get_by_id(self, client, scoop_payload):
        created = client.post("/api/scoops", json=scoop_payload).get_json()
        response = client.get(f"/api/scoops/{created['id']}")
        assert response.status_code == 200
        assert response.get_json()["name"] == "portafolio"

    def test_get_missing_returns_404(self, client):
        assert client.get("/api/scoops/999").status_code == 404

    def test_partial_update_keeps_other_fields(self, client, scoop_payload):
        created = client.post("/api/scoops", json=scoop_payload).get_json()
        response = client.put(
            f"/api/scoops/{created['id']}", json={"version": "2.0.0", "max_replicas": 5}
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data["version"] == "2.0.0"
        assert data["max_replicas"] == 5
        assert data["application"] == "portafolio-web"
        assert data["port"] == created["port"]

    def test_update_memory_quantity_string(self, client, scoop_payload):
        created = client.post("/api/scoops", json=scoop_payload).get_json()
        response = client.put(
            f"/api/scoops/{created['id']}",
            json={"requested_memory": "256Mi", "limit_memory": "1Gi"},
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data["requested_memory"] == "256Mi"
        assert data["limit_memory"] == "1Gi"
        # value+unit siguen siendo el soporte legacy para editar memoria.
        response = client.put(
            f"/api/scoops/{created['id']}",
            json={"requested_memory_value": 512, "requested_memory_unit": "M"},
        )
        assert response.status_code == 200
        assert response.get_json()["requested_memory"] == "512M"

    def test_delete(self, client, scoop_payload):
        created = client.post("/api/scoops", json=scoop_payload).get_json()
        # `force=true` salta el check de "deploy activo" (el cluster puede tener un
        # Deployment/Service con el mismo nombre de sesiones anteriores de pruebas).
        assert client.delete(f"/api/scoops/{created['id']}?force=true").status_code == 200
        assert client.get(f"/api/scoops/{created['id']}").status_code == 404


class TestApplicationBinding:
    """Vinculacion Scoop <-> Application via application_id."""

    def test_create_with_valid_application_id(self, client, scoop_payload):
        app_record = _create_app(client, "Mi App")
        response = client.post(
            "/api/scoops", json={**scoop_payload, "application_id": app_record["id"]}
        )
        assert response.status_code == 201
        data = response.get_json()
        assert data["application_id"] == app_record["id"]
        assert data["application_slug"] == app_record["slug"]

    def test_create_with_missing_application_id_conflicts(self, client, scoop_payload):
        response = client.post("/api/scoops", json={**scoop_payload, "application_id": 999})
        assert response.status_code == 404
        assert "aplicacion" in response.get_json()["error"]

    def test_update_changes_application_id(self, client, scoop_payload):
        created = client.post("/api/scoops", json=scoop_payload).get_json()
        app_record = _create_app(client, "Otra App")

        response = client.put(
            f"/api/scoops/{created['id']}", json={"application_id": app_record["id"]}
        )
        assert response.status_code == 200
        assert response.get_json()["application_slug"] == app_record["slug"]

    def test_update_to_missing_application_id_conflicts(self, client, scoop_payload):
        created = client.post("/api/scoops", json=scoop_payload).get_json()
        response = client.put(
            f"/api/scoops/{created['id']}", json={"application_id": 999}
        )
        assert response.status_code == 404


class TestAudit:
    """Los audits de scoops ahora se consultan via /api/audits.
    El endpoint /api/scoops/<id>/audits fue removido por peticion del usuario.
    """

    def test_mutations_are_audited_via_global_endpoint(self, client, scoop_payload):
        created = client.post("/api/scoops", json=scoop_payload).get_json()
        client.put(f"/api/scoops/{created['id']}", json={"version": "2.0.0"})

        audits = client.get(
            f"/api/audits?entity_type=scoop&entity_id={created['id']}"
        ).get_json()["items"]
        actions = [a["action"] for a in audits]
        assert "create" in actions
        assert "update" in actions

        update = next(a for a in audits if a["action"] == "update")
        assert update["old_data"]["version"] == "1.4.2"
        assert update["new_data"]["version"] == "2.0.0"

    def test_scoop_audits_endpoint_returns_410(self, client, scoop_payload):
        created = client.post("/api/scoops", json=scoop_payload).get_json()
        resp = client.get(f"/api/scoops/{created['id']}/audits")
        assert resp.status_code == 410


class TestEnvFromHelper:
    """Cobertura del helper _normalize_env_from."""

    def test_none_returns_empty_list(self):
        assert scoops_service._normalize_env_from(None) == []

    def test_empty_list_returns_empty_list(self):
        assert scoops_service._normalize_env_from([]) == []

    def test_keeps_valid_entries_in_order(self):
        out = scoops_service._normalize_env_from([
            {"type": "config_map", "name": "cm-a"},
            {"type": "secret", "name": "secret-a"},
            {"type": "secret", "name": "secret-a", "namespace": "user-apps"},
        ])
        assert len(out) == 3
        assert (out[0]["type"], out[0]["name"], out[0]["namespace"]) == (
            "config_map", "cm-a", None,
        )
        assert out[1]["namespace"] is None
        assert out[2]["namespace"] == "user-apps"

    def test_dedupes_same_kind_and_name(self):
        out = scoops_service._normalize_env_from([
            {"type": "secret", "name": "tok"},
            {"type": "secret", "name": "tok"},
            {"type": "secret", "name": "tok", "namespace": "prod"},
        ])
        # name+kind unico; el mismo name en otro namespace NO se dedupea
        assert len(out) == 2
        assert {o["namespace"] for o in out} == {None, "prod"}

    def test_strips_whitespace_in_name(self):
        out = scoops_service._normalize_env_from([
            {"type": "config_map", "name": "  shared-vars  "},
        ])
        assert out[0]["name"] == "shared-vars"

    def test_rejects_non_list(self):
        with pytest.raises(AppError):
            scoops_service._normalize_env_from({"type": "config_map", "name": "x"})

    def test_rejects_unknown_kind(self):
        with pytest.raises(AppError):
            scoops_service._normalize_env_from([
                {"type": "service", "name": "x"},
            ])

    def test_rejects_missing_name(self):
        with pytest.raises(AppError):
            scoops_service._normalize_env_from([
                {"type": "config_map", "name": ""},
            ])

    def test_rejects_too_many_entries(self):
        with pytest.raises(AppError):
            scoops_service._normalize_env_from(
                [{"type": "config_map", "name": f"cm-{i}"} for i in range(51)]
            )


class TestEnvFromCrud:
    def test_create_persists_env_from(self, client, scoop_payload):
        body = {
            **scoop_payload,
            "env_from": [
                {"type": "config_map", "name": "shared-vars"},
                {"type": "secret", "name": "shared-tokens", "namespace": "prod"},
            ],
        }
        response = client.post("/api/scoops", json=body)
        assert response.status_code == 201
        data = response.get_json()
        kinds = [(r["type"], r["name"]) for r in data["env_from"]]
        assert ("config_map", "shared-vars") in kinds
        assert ("secret", "shared-tokens") in kinds

    def test_create_without_env_from_defaults_to_empty(self, client, scoop_payload):
        response = client.post("/api/scoops", json=scoop_payload)
        assert response.status_code == 201
        assert response.get_json()["env_from"] == []

    def test_update_replaces_env_from(self, client, scoop_payload):
        created = client.post(
            "/api/scoops",
            json={**scoop_payload, "env_from": [
                {"type": "config_map", "name": "old-cm"},
            ]},
        ).get_json()
        assert client.put(
            f"/api/scoops/{created['id']}",
            json={"env_from": [{"type": "secret", "name": "new-tok"}]},
        ).status_code == 200
        body = client.get(f"/api/scoops/{created['id']}").get_json()
        # La BD guarda `namespace=None` y Pydantic V2 lo emite como `null`;
        # comparamos por tipo+name para ser robustos.
        assert [(r["type"], r["name"]) for r in body["env_from"]] == \
            [("secret", "new-tok")]

    def test_update_omitting_env_from_keeps_existing(self, client, scoop_payload):
        created = client.post(
            "/api/scoops",
            json={**scoop_payload, "env_from": [
                {"type": "config_map", "name": "shared-vars"},
            ]},
        ).get_json()
        client.put(f"/api/scoops/{created['id']}", json={"version": "2.0.0"})
        body = client.get(f"/api/scoops/{created['id']}").get_json()
        # Sin 'env_from' en el payload no se toca.
        assert [(r["type"], r["name"]) for r in body["env_from"]] == \
            [("config_map", "shared-vars")]

    def test_create_rejects_malformed_env_from(self, client, scoop_payload):
        body = {**scoop_payload, "env_from": [{"type": "config_map"}]}
        response = client.post("/api/scoops", json=body)
        assert response.status_code in (400, 422)