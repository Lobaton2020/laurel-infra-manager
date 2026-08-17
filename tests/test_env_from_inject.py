"""Tests del helper _inject_app_env_from del modulo scoops/manifest.

Verifica que:
- Si no hay ConfigMap/Secret auto-del `application`, devuelve vacio.
- Si existe el `<app>-config` y `<app>-secret`, los incluye.
- Las refs explicitas en `Scoop.env_from` se inyectan ademas.
- Una ref explicita duplicada con la auto-detectada NO se duplica.
- Una ref explicita a un recurso que no existe en el cluster se omite
  (best-effort) sin romper la generacion.
"""
import sys
import types
from types import SimpleNamespace

import pytest
from kubernetes.client.exceptions import ApiException

from app.modules.scoops.manifest import ManifestService


class _FakeApi:
    """Suficiente para los metodos que toca `_inject_app_env_from`.

    Los recursos preexistentes se cargan via ``set_cm`` / ``set_secret``.
    El resto devuelve 404 (no existe -> se omite en el manifiesto).
    """

    def __init__(self):
        self._cms: dict[tuple[str, str], bool] = {}
        self._secrets: dict[tuple[str, str], bool] = {}

    def set_cm(self, namespace: str, name: str):
        self._cms[(namespace, name)] = True

    def set_secret(self, namespace: str, name: str):
        self._secrets[(namespace, name)] = True

    def read_namespaced_config_map(self, name, namespace):
        if not self._cms.get((namespace, name)):
            raise ApiException(status=404, reason="NotFound")
        return SimpleNamespace(metadata=SimpleNamespace(name=name, namespace=namespace))

    def read_namespaced_secret(self, name, namespace):
        if not self._secrets.get((namespace, name)):
            raise ApiException(status=404, reason="NotFound")
        return SimpleNamespace(metadata=SimpleNamespace(name=name, namespace=namespace))


class _FakeClients:
    def __init__(self, core):
        self.core = core


@pytest.fixture
def fake_core(monkeypatch):
    """Parchea `app.core.k8s.get_clients` con un fake que sabe de CMs/Secrets."""
    from app.core.k8s import reset_clients
    reset_clients()
    core = _FakeApi()
    clients = _FakeClients(core)
    # ManifestService importa `get_clients` localmente: parchear ese binding.
    monkeypatch.setattr("app.core.k8s.get_clients", lambda: clients)
    monkeypatch.setattr(
        "app.modules.scoops.manifest.get_clients", lambda: clients, raising=False,
    )
    yield core
    reset_clients()


def make_scoop(**overrides):
    defaults = dict(
        application="demo",
        env_from=[],
        name="demo",
        type="api",
        namespace="prod",
    )
    defaults.update(overrides)
    # El helper solo mira `application` y `env_from`; un SimpleNamespace basta.
    return SimpleNamespace(**defaults)


def _invoke(scoop, namespace: str = "prod") -> list[dict]:
    """Llama al helper interno. Esto es un atajo para no armar un scoop completo."""
    return ManifestService._inject_app_env_from(scoop, namespace)


class TestNoAutoConfig:
    def test_empty_when_nothing_exists(self, fake_core):
        assert _invoke(make_scoop()) == []

    def test_empty_when_application_is_blank(self, fake_core):
        assert _invoke(make_scoop(application="")) == []


class TestAutoConfig:
    def test_only_configmap(self, fake_core):
        fake_core.set_cm("prod", "demo-config")
        out = _invoke(make_scoop())
        assert out == [{"configMapRef": {"name": "demo-config"}}]

    def test_only_secret(self, fake_core):
        fake_core.set_secret("prod", "demo-secret")
        out = _invoke(make_scoop())
        assert out == [{"secretRef": {"name": "demo-secret"}}]

    def test_both_present_in_order(self, fake_core):
        fake_core.set_cm("prod", "demo-config")
        fake_core.set_secret("prod", "demo-secret")
        out = _invoke(make_scoop())
        # Auto-detect: config_map antes que secret (orden estable).
        assert out == [
            {"configMapRef": {"name": "demo-config"}},
            {"secretRef": {"name": "demo-secret"}},
        ]


class TestExplicitRefs:
    def test_extra_configmap_injected(self, fake_core):
        fake_core.set_cm("prod", "demo-config")  # auto
        fake_core.set_cm("prod", "shared-vars")  # explicito
        out = _invoke(make_scoop(env_from=[
            {"type": "config_map", "name": "shared-vars"},
        ]))
        kinds = [(it.get("configMapRef") or it.get("secretRef")).get("name") for it in out]
        assert kinds == ["demo-config", "shared-vars"]

    def test_extra_secret_injected(self, fake_core):
        fake_core.set_secret("prod", "shared-tokens")
        out = _invoke(make_scoop(env_from=[
            {"type": "secret", "name": "shared-tokens"},
        ]))
        assert out == [{"secretRef": {"name": "shared-tokens"}}]

    def test_explicit_ref_missing_in_cluster_is_skipped(self, fake_core):
        """Una ref que NO existe en el cluster no debe aparecer ni romper."""
        # Solo auto existe; el explicito apunta a un recurso inexistente.
        fake_core.set_cm("prod", "demo-config")
        out = _invoke(make_scoop(env_from=[
            {"type": "config_map", "name": "does-not-exist"},
        ]))
        assert out == [{"configMapRef": {"name": "demo-config"}}]

    def test_explicit_and_auto_dedup(self, fake_core):
        """Si la ref explicita coincide con la auto-detectada, no se duplica."""
        fake_core.set_cm("prod", "demo-config")
        out = _invoke(make_scoop(env_from=[
            {"type": "config_map", "name": "demo-config"},  # mismo que auto
        ]))
        # Solo una entrada, en el orden del auto.
        assert out == [{"configMapRef": {"name": "demo-config"}}]

    def test_unknown_kind_in_explicit_ref_is_skipped(self, fake_core):
        """Una entrada con type='service' (no soportado) se ignora sin error."""
        fake_core.set_cm("prod", "demo-config")
        out = _invoke(make_scoop(env_from=[
            {"type": "service", "name": "kube-dns"},  # type no soportado
        ]))
        assert out == [{"configMapRef": {"name": "demo-config"}}]

    def test_explicit_ref_in_other_namespace(self, fake_core):
        """La ref explicita puede apuntar a otro namespace."""
        fake_core.set_cm("user-apps", "tenant-vars")
        out = _invoke(make_scoop(env_from=[
            {"type": "config_map", "name": "tenant-vars", "namespace": "user-apps"},
        ]))
        assert out == [{"configMapRef": {"name": "tenant-vars"}}]

    def test_explicit_ref_in_other_namespace_missing_is_skipped(self, fake_core):
        """Si la ref de otro namespace no existe, no rompe ni aparece."""
        out = _invoke(make_scoop(env_from=[
            {"type": "config_map", "name": "nope", "namespace": "user-apps"},
        ]))
        assert out == []


class TestClusterFailure:
    """Best-effort: si el API server falla con un error NO-404, devolvemos []."""

    def test_non_404_api_error_returns_empty(self, monkeypatch):
        from app.core.k8s import reset_clients

        class _BoomApi:
            def read_namespaced_config_map(self, *a, **k):
                raise ApiException(status=500, reason="ServerError")
            def read_namespaced_secret(self, *a, **k):
                raise ApiException(status=500, reason="ServerError")

        reset_clients()
        monkeypatch.setattr("app.core.k8s.get_clients", lambda: _FakeClients(_BoomApi()))
        monkeypatch.setattr(
            "app.modules.scoops.manifest.get_clients",
            lambda: _FakeClients(_BoomApi()), raising=False,
        )
        out = _invoke(make_scoop())
        assert out == []
        reset_clients()