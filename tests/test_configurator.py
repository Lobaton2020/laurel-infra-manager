"""Tests del modulo Configurator (schemas/columns/records/stats) en laurel."""
import pytest

_seeded_schemas = ("app-settings", "feature-flags")


@pytest.fixture
def schema_id(client):
    """Crea un schema limpio para ejercitar columnas y records."""
    r = client.post("/api/schemas", json={"name": "test-schema", "description": "para tests"})
    assert r.status_code == 201
    return r.get_json()["id"]


def test_seed_creates_demo_schemas(client):
    """Al arrancar la app se siembran los schemas de ejemplo (idempotente)."""
    r = client.get("/api/schemas")
    assert r.status_code == 200
    names = [s["name"] for s in r.get_json()]
    assert len(names) == 2
    assert set(names) == set(_seeded_schemas)


def test_create_and_get_schema(client):
    r = client.post("/api/schemas", json={"name": "mi-app", "description": "cfg"})
    assert r.status_code == 201
    data = r.get_json()
    assert data["name"] == "mi-app"

    r = client.get(f"/api/schemas/{data['id']}")
    assert r.status_code == 200
    body = r.get_json()
    assert body["name"] == "mi-app"
    assert body["columns"] == []


def test_update_and_delete_schema(client, schema_id):
    r = client.put(f"/api/schemas/{schema_id}", json={"name": "renombrado"})
    assert r.status_code == 200
    assert r.get_json()["name"] == "renombrado"

    assert client.delete(f"/api/schemas/{schema_id}").status_code == 200
    assert client.get(f"/api/schemas/{schema_id}").status_code == 404


def test_schema_not_found(client):
    assert client.get("/api/schemas/9999").status_code == 404
    assert client.get("/api/schemas/9999/records").status_code == 404


def test_column_crud(client, schema_id):
    r = client.post(
        f"/api/schemas/{schema_id}/columns",
        json={"name": "Key", "data_type": "string", "is_filterable": True, "order": 0},
    )
    assert r.status_code == 201
    col = r.get_json()
    assert col["name"] == "Key"

    cols = client.get(f"/api/schemas/{schema_id}/columns").get_json()
    assert len(cols) == 1

    r = client.put(
        f"/api/schemas/{schema_id}/columns/{col['id']}",
        json={"name": "Clave", "data_type": "string", "is_filterable": False, "order": 1},
    )
    assert r.status_code == 200
    assert r.get_json()["name"] == "Clave"

    assert client.delete(f"/api/schemas/{schema_id}/columns/{col['id']}").status_code == 200
    assert client.get(f"/api/schemas/{schema_id}/columns").get_json() == []


def test_column_invalid_type(client, schema_id):
    r = client.post(
        f"/api/schemas/{schema_id}/columns",
        json={"name": "Key", "data_type": "mono"},  # no es un DataType valido
    )
    assert r.status_code == 422


def _add_columns(client, schema_id):
    for name, data_type in [("Key", "string"), ("Value", "json"), ("Environment", "string")]:
        r = client.post(
            f"/api/schemas/{schema_id}/columns",
            json={"name": name, "data_type": data_type,
                  "is_filterable": name != "Value", "order": 0},
        )
        assert r.status_code == 201


def test_record_crud(client, schema_id):
    _add_columns(client, schema_id)
    payload = {"Key": "theme", "Value": {"mode": "dark"}, "Environment": "prod"}
    r = client.post(f"/api/schemas/{schema_id}/records", json={"data": payload})
    assert r.status_code == 201
    rec = r.get_json()
    assert rec["data"] == payload

    r = client.get(f"/api/schemas/{schema_id}/records")
    assert r.status_code == 200
    body = r.get_json()
    assert body["total"] == 1
    assert body["pages"] == 1

    updated = dict(payload, Environment="dev")
    r = client.put(f"/api/schemas/{schema_id}/records/{rec['id']}", json={"data": updated})
    assert r.status_code == 200
    assert r.get_json()["data"]["Environment"] == "dev"

    assert client.delete(f"/api/schemas/{schema_id}/records/{rec['id']}").status_code == 200
    assert client.get(f"/api/schemas/{schema_id}/records").get_json()["total"] == 0


def test_record_validation_errors(client, schema_id):
    _add_columns(client, schema_id)
    # Falta la clave 'Value' -> 422 por pydantic
    r = client.post(f"/api/schemas/{schema_id}/records", json={"data": {"Key": "x"}})
    assert r.status_code == 422
    # Columna desconocida -> 400 por el servicio
    r = client.post(
        f"/api/schemas/{schema_id}/records",
        json={"data": {"Key": "x", "Value": {}, "Bogus": 1}},
    )
    assert r.status_code == 400


def test_record_search_wildcards(client, schema_id):
    _add_columns(client, schema_id)
    data = [
        {"Key": "theme", "Value": {"mode": "dark"}, "Environment": "prod"},
        {"Key": "api_url", "Value": {"url": "https://x"}, "Environment": "prod"},
        {"Key": "max_retries", "Value": {"n": 3}, "Environment": "dev"},
    ]
    for d in data:
        assert client.post(f"/api/schemas/{schema_id}/records", json={"data": d}).status_code == 201

    search = lambda **kw: client.post(  # noqa: E731
        f"/api/schemas/{schema_id}/records/search", json={"filters": kw}
    ).get_json()

    assert search(Key="theme")["total"] == 1
    assert search(Key="t*")["total"] == 1
    assert search(Key="max*")["total"] == 1
    assert search(Key="*_url")["total"] == 1
    assert search(Key="*e*")["total"] == 2  # theme + max_retries


def test_stats(client):
    r = client.get("/api/stats")
    assert r.status_code == 200
    body = r.get_json()
    assert body["total_schemas"] == 2
    assert body["total_columns"] == 6
    assert body["total_records"] == 6
    assert len(body["schemas"]) == 2


def test_configurator_logs_into_unified_audits(client):
    """Las mutaciones del configurator quedan en la misma tabla audits que laurel."""
    r = client.post("/api/schemas", json={"name": "auditable"})
    new_id = r.get_json()["id"]

    by_entity = client.get(f"/api/audits/schema/{new_id}").get_json()
    assert len(by_entity) == 1
    assert by_entity[0]["action"] == "create"
    assert by_entity[0]["entity_type"] == "schema"

    all_audits = client.get("/api/audits").get_json()
    assert all_audits["total"] >= 1
    assert any(a["entity_type"] == "schema" for a in all_audits["items"])