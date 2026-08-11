"""Acceso al API de Kubernetes.

Centraliza la carga del kubeconfig y expone los clientes tipados. Los clientes se
cachean por proceso: construirlos implica leer y parsear certificados en disco.
"""
import logging
import threading

import yaml
from flask import current_app
from kubernetes import client as k8s_client
from kubernetes import config as k8s_config
from kubernetes.config.config_exception import ConfigException

from app.core.errors import ClusterError

logger = logging.getLogger(__name__)

_LOCAL_HOSTS = ("127.0.0.1", "localhost")

_lock = threading.Lock()
_cache: dict[tuple, "K8sClients"] = {}


class K8sClients:
    """Agrupa los clientes de las API groups que usa el proyecto."""

    def __init__(self, configuration: k8s_client.Configuration):
        self.configuration = configuration
        self.api_client = k8s_client.ApiClient(configuration)
        self.core = k8s_client.CoreV1Api(self.api_client)
        self.apps = k8s_client.AppsV1Api(self.api_client)
        self.networking = k8s_client.NetworkingV1Api(self.api_client)
        self.autoscaling = k8s_client.AutoscalingV2Api(self.api_client)
        self.batch = k8s_client.BatchV1Api(self.api_client)
        self.version = k8s_client.VersionApi(self.api_client)

    @property
    def host(self) -> str:
        return self.configuration.host

    def serialize(self, obj):
        """Convierte objetos del cliente de K8s a dicts JSON-serializables."""
        return self.api_client.sanitize_for_serialization(obj)


def _normalize_kubeconfig(kubeconfig_path: str) -> dict | None:
    """Corrige en memoria referencias de usuario rotas en el kubeconfig.

    Si el contexto activo apunta a un usuario que no existe (ej: `user: defaults`
    cuando el definido es `default`), el cliente no envia el certificado y el API
    server responde 401. Como K3s regenera este fichero en cada reinicio, lo
    normalizamos al vuelo en vez de depender de que este bien en disco.

    Devuelve el dict corregido, o None si no hizo falta tocar nada.
    """
    try:
        with open(kubeconfig_path) as fh:
            cfg = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError):
        return None  # load_kube_config reportara el error real

    if not isinstance(cfg, dict):
        return None

    defined = [u.get("name") for u in cfg.get("users") or []]
    if len(defined) != 1:
        return None  # con 0 o varios usuarios no podemos inferir cual es el bueno

    changed = False
    for context in cfg.get("contexts") or []:
        referenced = context.get("context", {}).get("user")
        if referenced and referenced not in defined:
            logger.warning(
                "kubeconfig: el contexto '%s' apunta al usuario inexistente '%s'; "
                "usando '%s'. Corrige el fichero para evitar un 401.",
                context.get("name"), referenced, defined[0],
            )
            context["context"]["user"] = defined[0]
            changed = True

    return cfg if changed else None


def _build_configuration(kubeconfig_path: str, api_server: str, verify_ssl: bool):
    configuration = k8s_client.Configuration()
    normalized = _normalize_kubeconfig(kubeconfig_path)
    try:
        if normalized is not None:
            k8s_config.load_kube_config_from_dict(
                config_dict=normalized,
                client_configuration=configuration,
            )
        else:
            k8s_config.load_kube_config(
                config_file=kubeconfig_path,
                client_configuration=configuration,
            )
    except (ConfigException, FileNotFoundError) as exc:
        raise ClusterError(
            f"No se pudo cargar el kubeconfig desde '{kubeconfig_path}': {exc}",
            status_code=503,
        ) from exc

    # K3s reescribe el server a 127.0.0.1 en cada reinicio (ver K3S_CONTEXT.md).
    # Desde fuera del cluster eso apunta a la maquina equivocada, asi que lo corregimos.
    if any(h in (configuration.host or "") for h in _LOCAL_HOSTS):
        logger.warning(
            "kubeconfig apunta a %s, redirigiendo a %s", configuration.host, api_server
        )
        configuration.host = api_server

    configuration.verify_ssl = verify_ssl
    if not verify_ssl:
        configuration.ssl_ca_cert = None

    return configuration


def get_clients() -> K8sClients:
    """Devuelve los clientes de K8s para la configuracion activa de la app."""
    kubeconfig_path = current_app.config["KUBECONFIG_PATH"]
    api_server = current_app.config["K8S_API_SERVER"]
    verify_ssl = current_app.config["K8S_VERIFY_SSL"]
    key = (kubeconfig_path, api_server, verify_ssl)

    clients = _cache.get(key)
    if clients is not None:
        return clients

    with _lock:
        # Otro hilo pudo construirlo mientras esperabamos el lock.
        if key not in _cache:
            _cache[key] = K8sClients(
                _build_configuration(kubeconfig_path, api_server, verify_ssl)
            )
        return _cache[key]


def reset_clients():
    """Invalida el cache. Util tras rotar el kubeconfig y en tests."""
    with _lock:
        _cache.clear()
