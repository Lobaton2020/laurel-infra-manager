"""Tests del modulo ConfigStore (ConfigMaps y Secrets).

Verifica que:
- `GET /api/configstore/configmaps?app=<slug>` y el de secrets autoderivan
  el namespace a `user-apps-<slug>` y filtran por el label de la app
  (cada app maneja todo independiente).
- Sin `app`, se usa el DEFAULT_NAMESPACE (compatibilidad con power users).
- El namespace explicito del caller gana sobre la autoderivacion.
"""
from types import SimpleNamespace

import pytest
from kubernetes.client.exceptions import ApiException

from app.modules.configstore.service import ConfigStoreService

# El servicio usa esta label como filtro y para anotar los recursos.
APP_LABEL_KEY = ConfigStoreService.APP_LABEL  # "laurel.andrelobaton.top/app"


class _FakeCM:
    def __init__(self, name, namespace, app_label, data=None):
        self.metadata = SimpleNamespace(
            name=name,
            namespace=namespace,
            labels={APP_LABEL_KEY: app_label} if app_label else {},
            creation_timestamp=None,
        )
        self.data = data or {}


class _FakeSecret:
    def __init__(self, name, namespace, app_label, data=None):
        self.metadata = SimpleNamespace(
            name=name,
            namespace=namespace,
            labels={APP_LABEL_KEY: app_label} if app_label else {},
            creation_timestamp=None,
        )
        self.data = data or {}


class _FakeCore:
    """Fake del cliente K8s core: lleva registro de las llamadas y devuelve
    recursos preconfigurados segun el namespace."""

    def __init__(self):
        self.configmaps: dict[tuple[str, str], _FakeCM] = {}
        self.secrets: dict[tuple[str, str], _FakeSecret] = {}
        self.list_calls: list[dict] = []
        self.create_calls: list[dict] = []
        self.replace_calls: list[dict] = []

    def add_cm(self, name, namespace, app, data=None):
        self.configmaps[(namespace, name)] = _FakeCM(name, namespace, app, data)

    def add_secret(self, name, namespace, app, data=None):
        self.secrets[(namespace, name)] = _FakeSecret(name, namespace, app, data)

    def list_namespaced_config_map(self, namespace, label_selector=None):
        self.list_calls.append({"kind": "cm", "namespace": namespace, "selector": label_selector})
        items = [cm for (ns, _), cm in self.configmaps.items() if ns == namespace]
        if label_selector and "=" in label_selector:
            key, value = label_selector.split("=", 1)
            if key == APP_LABEL_KEY:
                items = [
                    cm for cm in items
                    if (cm.metadata.labels or {}).get(APP_LABEL_KEY) == value
                ]
        return SimpleNamespace(items=items)

    def list_namespaced_secret(self, namespace, label_selector=None):
        self.list_calls.append({"kind": "secret", "namespace": namespace, "selector": label_selector})
        items = [s for (ns, _), s in self.secrets.items() if ns == namespace]
        if label_selector and "=" in label_selector:
            key, value = label_selector.split("=", 1)
            if key == APP_LABEL_KEY:
                items = [
                    s for s in items
                    if (s.metadata.labels or {}).get(APP_LABEL_KEY) == value
                ]
        return SimpleNamespace(items=items)

    def create_namespaced_config_map(self, namespace, body):
        self.create_calls.append({"kind": "cm", "namespace": namespace, "name": body["metadata"]["name"]})
        self.configmaps[(namespace, body["metadata"]["name"])] = _FakeCM(
            body["metadata"]["name"], namespace,
            body["metadata"].get("labels", {}).get(APP_LABEL_KEY, ""),
            data=body.get("data") or {},
        )

    def create_namespaced_secret(self, namespace, body):
        self.create_calls.append({"kind": "secret", "namespace": namespace, "name": body["metadata"]["name"]})
        self.secrets[(namespace, body["metadata"]["name"])] = _FakeSecret(
            body["metadata"]["name"], namespace,
            body["metadata"].get("labels", {}).get(APP_LABEL_KEY, ""),
            data=body.get("data") or {},
        )

    def replace_namespaced_config_map(self, name, namespace, body):
        self.replace_calls.append({"kind": "cm", "namespace": namespace, "name": name})
        self.configmaps[(namespace, name)] = _FakeCM(
            name, namespace,
            body["metadata"].get("labels", {}).get(APP_LABEL_KEY, ""),
            data=body.get("data") or {},
        )

    def replace_namespaced_secret(self, name, namespace, body):
        self.replace_calls.append({"kind": "secret", "namespace": namespace, "name": name})
        self.secrets[(namespace, name)] = _FakeSecret(
            name, namespace,
            body["metadata"].get("labels", {}).get(APP_LABEL_KEY, ""),
            data=body.get("data") or {},
        )

    def read_namespaced_config_map(self, name, namespace):
        return self.configmaps[(namespace, name)]

    def read_namespaced_secret(self, name, namespace):
        return self.secrets[(namespace, name)]


class _FakeClients:
    def __init__(self, core):
        self.core = core


@pytest.fixture
def fake_k8s(monkeypatch):
    from app.core.k8s import reset_clients
    reset_clients()
    core = _FakeCore()
    clients = _FakeClients(core)
    monkeypatch.setattr("app.core.k8s.get_clients", lambda: clients)
    monkeypatch.setattr("app.modules.cluster.service.get_clients", lambda: clients, raising=False)
    monkeypatch.setattr(
        "app.modules.configstore.service.get_clients", lambda: clients, raising=False,
    )
    # Evitamos tocar el cluster real: el namespace "ya existe".
    monkeypatch.setattr(
        "app.modules.cluster.service.K8sService.namespace_exists",
        staticmethod(lambda _ns: True),
        raising=False,
    )
    monkeypatch.setattr(
        "app.modules.cluster.service.K8sService.create_namespace",
        staticmethod(lambda _ns: None),
        raising=False,
    )
    yield core
    reset_clients()


@pytest.fixture
def fake_apps(monkeypatch):
    """Mockea el check `Application.query.filter_by(slug=..., deleted_at=None).first()`
    para que crea que la app existe sin tocar la BD."""
    class _Q:
        def filter_by(self, **_kwargs):
            return self
        def first(self):
            return SimpleNamespace(slug="alpha")
    monkeypatch.setattr(
        "app.modules.apps.model.Application.query",
        _Q(),
    )
    yield


# --------------------------- ConfigMaps ---------------------------

class TestListConfigmapsFiltering:
    def test_list_without_app_uses_default_namespace(self, client, fake_k8s):
        fake_k8s.add_cm("shared", "prod", "")
        r = client.get("/api/configstore/configmaps")
        assert r.status_code == 200
        names = [c["name"] for c in r.get_json()]
        assert names == ["shared"]
        # La llamada a K8s fue al namespace default.
        assert fake_k8s.list_calls[-1]["namespace"] == "prod"

    def test_list_with_app_uses_app_namespace_and_filters(self, client, fake_k8s):
        # App A tiene un CM; app B tiene otro CM distinto. El namespace del
        # backend los aisla: cada uno vive en user-apps-<slug>.
        fake_k8s.add_cm("a-config", "user-apps-alpha", "alpha", data={"k": "v"})
        fake_k8s.add_cm("b-config", "user-apps-beta", "beta", data={"k": "v"})

        r = client.get("/api/configstore/configmaps?app=alpha")
        assert r.status_code == 200
        items = r.get_json()
        # Solo se ve el CM de alpha, en su namespace.
        assert [c["name"] for c in items] == ["a-config"]
        assert items[0]["namespace"] == "user-apps-alpha"
        assert items[0]["app"] == "alpha"
        # Verificamos que el backend pidio `user-apps-alpha` con el label filter.
        call = fake_k8s.list_calls[-1]
        assert call["namespace"] == "user-apps-alpha"
        assert call["selector"] == f"{APP_LABEL_KEY}=alpha"

    def test_list_namespace_override_wins_over_app(self, client, fake_k8s):
        """Si el caller pasa `namespace` explicito, gana sobre la autoderivacion
        de la app. Esto es util para debug o migraciones."""
        fake_k8s.add_cm("shared", "staging", "")
        r = client.get("/api/configstore/configmaps?app=alpha&namespace=staging")
        assert r.status_code == 200
        assert fake_k8s.list_calls[-1]["namespace"] == "staging"


# --------------------------- Secrets ---------------------------

class TestListSecretsFiltering:
    def test_list_without_app_uses_default_namespace(self, client, fake_k8s):
        fake_k8s.add_secret("shared", "prod", "")
        r = client.get("/api/configstore/secrets")
        assert r.status_code == 200
        names = [s["name"] for s in r.get_json()]
        assert names == ["shared"]
        assert fake_k8s.list_calls[-1]["namespace"] == "prod"

    def test_list_with_app_uses_app_namespace_and_filters(self, client, fake_k8s):
        fake_k8s.add_secret("alpha-secret", "user-apps-alpha", "alpha", data={"t": "x"})
        fake_k8s.add_secret("beta-secret", "user-apps-beta", "beta", data={"t": "x"})

        r = client.get("/api/configstore/secrets?app=alpha")
        assert r.status_code == 200
        items = r.get_json()
        assert [s["name"] for s in items] == ["alpha-secret"]
        assert items[0]["app"] == "alpha"
        call = fake_k8s.list_calls[-1]
        assert call["namespace"] == "user-apps-alpha"
        assert call["selector"] == f"{APP_LABEL_KEY}=alpha"

    def test_list_secrets_does_not_leak_across_apps(self, client, fake_k8s):
        """Aunque por algun error hubiera un secret de otra app en el mismo
        namespace, el label filter lo deja fuera."""
        fake_k8s.add_secret("alpha-secret", "user-apps-alpha", "alpha")
        # Intruso: secret con label de OTRA app metido en el namespace equivocado.
        fake_k8s.add_secret("intruder", "user-apps-alpha", "beta")

        r = client.get("/api/configstore/secrets?app=alpha")
        assert r.status_code == 200
        names = [s["name"] for s in r.get_json()]
        assert "alpha-secret" in names
        assert "intruder" not in names


# --------------------------- Create (regresion: namespace=None) ---------------------------

class TestCreateResolvesNamespaceFromApp:
    """Regresion: cuando el caller NO envia `namespace`, el service debe
    autoderivarlo a `user-apps-<app>` antes de hablar con K8s.

    Bug historico: la funcion usaba la variable `namespace` (que podia ser
    None) en vez de `ns` (la ya resuelta) en metadata y en las llamadas al
    cliente K8s. Resultado: 500 con `create_namespaced_secret(None, ...)`.
    """

    def test_post_secret_without_namespace_creates_in_app_ns(
        self, client, fake_k8s, fake_apps,
    ):
        r = client.post(
            "/api/configstore/secrets",
            json={"app": "alpha", "data": {"k": "dmFsdWU="}},
        )
        assert r.status_code == 200, r.get_json()
        # La llamada a K8s fue al namespace derivado, NO None.
        assert len(fake_k8s.create_calls) == 1
        call = fake_k8s.create_calls[0]
        assert call["kind"] == "secret"
        assert call["namespace"] == "user-apps-alpha"
        assert call["name"] == "alpha-secret"
        # El recurso quedo guardado en el namespace correcto.
        assert ("user-apps-alpha", "alpha-secret") in fake_k8s.secrets

    def test_post_configmap_without_namespace_creates_in_app_ns(
        self, client, fake_k8s, fake_apps,
    ):
        r = client.post(
            "/api/configstore/configmaps",
            json={"app": "alpha", "data": {"k": "v"}},
        )
        assert r.status_code == 200, r.get_json()
        assert len(fake_k8s.create_calls) == 1
        call = fake_k8s.create_calls[0]
        assert call["kind"] == "cm"
        assert call["namespace"] == "user-apps-alpha"
        assert ("user-apps-alpha", "alpha-config") in fake_k8s.configmaps

    def test_post_secret_with_explicit_namespace_uses_it(
        self, client, fake_k8s, fake_apps,
    ):
        """El override explicito del caller gana sobre la autoderivacion."""
        r = client.post(
            "/api/configstore/secrets",
            json={"app": "alpha", "namespace": "staging", "data": {"k": "dg=="}},
        )
        assert r.status_code == 200, r.get_json()
        assert fake_k8s.create_calls[0]["namespace"] == "staging"

    def test_post_secret_unknown_app_returns_404(self, client, fake_k8s):
        """No se debe crear un secret huerfano en cluster para una app
        inexistente en la plataforma."""
        r = client.post(
            "/api/configstore/secrets",
            json={"app": "ghost", "data": {"k": "dg=="}},
        )
        assert r.status_code == 404
        assert fake_k8s.create_calls == []

