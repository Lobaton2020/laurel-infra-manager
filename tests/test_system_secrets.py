"""Tests del modulo System: edicion de los secretos del sistema."""

import base64
from types import SimpleNamespace

import pytest
from kubernetes.client.exceptions import ApiException

from app.core.k8s import reset_clients
from app.modules.system import service as system_service


# ---------- Fake del API server ----------

class _FakeSecret:
    def __init__(self, name, namespace, data, resource_version="1"):
        self.metadata = SimpleNamespace(
            name=name,
            namespace=namespace,
            resource_version=resource_version,
        )
        self.data = data


class _FakeCore:
    def __init__(self):
        # data guarda el contenido en base64, igual que k8s
        self.secrets: dict[tuple[str, str], _FakeSecret] = {}

    def _put(self, namespace, name, data):
        rv = str(len(self.secrets.get((namespace, name)).metadata.resource_version) + 1) \
            if (namespace, name) in self.secrets else "1"
        self.secrets[(namespace, name)] = _FakeSecret(name, namespace, data, resource_version=rv)

    def read_namespaced_secret(self, name, namespace):
        key = (namespace, name)
        if key not in self.secrets:
            raise ApiException(status=404, reason="NotFound")
        return self.secrets[key]

    def patch_namespaced_secret(self, name, namespace, body):
        key = (namespace, name)
        if key not in self.secrets:
            raise ApiException(status=404, reason="NotFound")
        new_data = self.secrets[key].data.copy()
        new_data.update(body.get("data") or {})
        self._put(namespace, name, new_data)
        return self.secrets[key]


class _FakeClients:
    def __init__(self, core):
        self.core = core
        self.apps = None
        self.networking = None
        self.configuration = SimpleNamespace(host="fake://test")

    @property
    def host(self) -> str:
        return self.configuration.host

    def serialize(self, obj):
        return obj


# ---------- Helpers ----------

def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _env_client(client):
    """Vuelve a parchar las referencias tras un reset."""
    pass


@pytest.fixture
def fake_cluster(monkeypatch):
    reset_clients()
    core = _FakeCore()
    fake = _FakeClients(core)
    # Pre-cargamos los dos secretos del sistema con contenido plausible.
    core._put("prod", "laurel-secrets", {
        ".env": _b64("DB_TYPE=mysql\nHOST=192.168.20.240\nPORT=3306\n"),
    })
    core._put("prod", "laurel-kubeconfig", {
        "k3s.yaml": _b64("apiVersion: v1\nclusters:\n- cluster:\n    server: https://127.0.0.1:6443\n"),
    })
    core._put("prod", "laurel-integrations", {
        "github-pat": _b64(""),
        "docker-pat": _b64(""),
        "jenkins-token": _b64(""),
    })

    monkeypatch.setattr("app.core.k8s.get_clients", lambda: fake)
    monkeypatch.setattr("app.modules.cluster.service.get_clients", lambda: fake)
    monkeypatch.setattr("app.modules.system.service.get_clients", lambda: fake)

    yield core
    reset_clients()


# ---------- Tests de whitelist / parsing ----------

class TestParsing:
    def test_parse_env_skips_comments_and_blanks(self):
        items = system_service._parse_env(
            "# comentario\n\nDB_URL=postgres\nDB_USER=admin\n"
        )
        assert [i["key"] for i in items] == ["DB_URL", "DB_USER"]
        assert items[0]["value"] == "postgres"

    def test_parse_env_rejects_invalid_key(self):
        with pytest.raises(Exception) as exc:
            system_service._parse_env("1BAD=1\n")
        assert "Nombre de variable invalido" in str(exc.value)

    def test_parse_env_rejects_line_without_equals(self):
        with pytest.raises(Exception):
            system_service._parse_env("no_equals_sign\n")

    def test_decode_handles_string_and_bytes(self):
        assert system_service._decode(_b64("hola")) == "hola"
        assert system_service._decode(b"adios") == "adios"


class TestWhitelist:
    def test_unknown_secret_id_is_rejected(self):
        with pytest.raises(Exception) as exc:
            system_service._resolve("tls-secrets")
        assert "whitelist" in str(exc.value)


# ---------- Tests de endpoints ----------

class TestListManaged:
    def test_returns_all_with_meta_no_values(self, client, fake_cluster):
        r = client.get("/api/system/secrets")
        assert r.status_code == 200
        items = r.get_json()["items"]
        ids = {it["id"] for it in items}
        # 5 secretos: 2 originales + github_pat + docker_pat + jenkins_token
        assert ids == {"laurel-secrets", "laurel-kubeconfig", "github_pat", "docker_pat", "jenkins_token"}
        # Solo campos meta, no exponemos los valores de las claves.
        for item in items:
            assert "content" not in item
            if item["kind"] == "env":
                assert isinstance(item["env_keys"], list)


class TestGetContent:
    def test_get_env_secret_returns_entries(self, client, fake_cluster):
        r = client.get("/api/system/secrets/laurel-secrets")
        assert r.status_code == 200
        data = r.get_json()
        assert data["kind"] == "env"
        assert data["content"].startswith("DB_TYPE=mysql")
        keys = {e["key"] for e in data["entries"]}
        assert "DB_TYPE" in keys

    def test_get_kubeconfig_returns_text(self, client, fake_cluster):
        r = client.get("/api/system/secrets/laurel-kubeconfig")
        assert r.status_code == 200
        data = r.get_json()
        assert data["kind"] == "text"
        assert "apiVersion: v1" in data["content"]

    def test_unknown_id_returns_403(self, client, fake_cluster):
        r = client.get("/api/system/secrets/tls-secrets")
        assert r.status_code == 403


class TestUpdate:
    def test_put_env_secret_patches_and_restarts(self, client, fake_cluster, monkeypatch):
        new_content = "DB_TYPE=postgres\nHOST=10.0.0.1\n"
        restarted_with = []
        def _fake_restart(ns, name):
            restarted_with.append((ns, name))
            return {"name": name, "namespace": ns}
        monkeypatch.setattr(
            "app.modules.system.service.K8sService.restart_deployment",
            _fake_restart,
        )
        r = client.put(
            "/api/system/secrets/laurel-secrets",
            json={"content": new_content},
        )
        assert r.status_code == 200
        body = r.get_json()
        assert body["saved"] is True
        assert body["restarted"] is True
        assert fake_cluster.secrets[("prod", "laurel-secrets")].data[".env"] == _b64(new_content)
        assert restarted_with == [("prod", "laurel-infra-manager")]

    def test_put_restart_failure_still_returns_saved(self, client, fake_cluster, monkeypatch):
        # Forzamos un error de rollout tras patch exitoso
        def _explode(ns, name):
            raise RuntimeError("kube unreachable")
        monkeypatch.setattr(
            "app.modules.system.service.K8sService.restart_deployment",
            _explode,
        )
        r = client.put(
            "/api/system/secrets/laurel-secrets",
            json={"content": "DB_TYPE=mysql\n"},
        )
        assert r.status_code == 200
        body = r.get_json()
        assert body["saved"] is True
        assert body["restarted"] is False
        assert "kube unreachable" in (body.get("restart_error") or "")

    def test_put_invalid_env_line_returns_400(self, client, fake_cluster):
        r = client.put(
            "/api/system/secrets/laurel-secrets",
            json={"content": "1invalid-key=val\n"},
        )
        assert r.status_code == 400

    def test_put_unknown_id_returns_403(self, client, fake_cluster):
        r = client.put(
            "/api/system/secrets/tls-secrets",
            json={"content": "x=y\n"},
        )
        assert r.status_code == 403

    def test_put_missing_content_returns_400(self, client, fake_cluster):
        r = client.put(
            "/api/system/secrets/laurel-secrets",
            json={},
        )
        assert r.status_code == 400

    def test_put_kubeconfig_empty_rejected(self, client, fake_cluster):
        r = client.put(
            "/api/system/secrets/laurel-kubeconfig",
            json={"content": "   \n   \n"},
        )
        assert r.status_code == 400
