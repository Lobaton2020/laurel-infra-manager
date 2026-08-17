"""Secrets del sistema: edicion de los secretos que monta el propio backend.

Se limita por WHITELIST a unos cuantos pares (namespace, name) gestionados por la
propia aplicacion, asi un endpoint mal apuntado no puede tocar secretos ajenos
(SA tokens, tls secrets, dockerconfigjson de los deploys de los scoops, etc.).
"""
from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from kubernetes.client.exceptions import ApiException

from app.core.errors import AppError
from app.modules.cluster.service import K8sService, get_clients


@dataclass(frozen=True)
class ManagedSecret:
    """Pareja (namespace, name) y la clave concreta que contiene el contenido
    editable. La whitelist es codigo, no config del usuario: si el cluster
    cambia, toca tocar el codigo y desplegar."""
    namespace: str
    name: str
    key: str
    # 'env': el contenido se trata como KEY=VALUE lines y se valida.
    # 'text': texto crudo (p.ej. kubeconfig yaml).
    kind: str
    # deployment que se reinicia tras PUT para recargar el secreto montado.
    deployment: str


MANAGED: dict[str, ManagedSecret] = {
    "laurel-secrets": ManagedSecret(
        namespace="prod",
        name="laurel-secrets",
        key=".env",
        kind="env",
        deployment="laurel-infra-manager",
    ),
    "laurel-kubeconfig": ManagedSecret(
        namespace="prod",
        name="laurel-kubeconfig",
        key="k3s.yaml",
        kind="text",
        deployment="laurel-infra-manager",
    ),
    # Secrets para integraciones externas. Se exponen al backend como
    # variables de entorno (no como archivos) y se montan en el deployment
    # del sistema via `envFrom`/Secret-key en deploy/base/deployment.yml.
    "github_pat": ManagedSecret(
        namespace="prod",
        name="laurel-integrations",
        key="github-pat",
        kind="text",
        deployment="laurel-infra-manager",
    ),
    "docker_pat": ManagedSecret(
        namespace="prod",
        name="laurel-integrations",
        key="docker-pat",
        kind="text",
        deployment="laurel-infra-manager",
    ),
}


def _resolve(name: str) -> ManagedSecret:
    if name not in MANAGED:
        raise AppError(
            f"El secreto '{name}' no esta en la whitelist de gestion. "
            f"Permitidos: {sorted(MANAGED)}",
            status_code=403,
        )
    return MANAGED[name]


def _decode(value_b64: str | bytes | None) -> str:
    if value_b64 is None:
        return ""
    if isinstance(value_b64, str):
        raw = base64.b64decode(value_b64)
    else:
        raw = bytes(value_b64)
    return raw.decode("utf-8", errors="replace")


def _encode(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _last_modified_index(secret_dict: dict) -> str | None:
    """Lee metadata.managedFields o fallback al anno de la ultima mutacion."""
    meta = secret_dict.get("metadata") or {}
    fields = meta.get("managedFields") or []
    last = None
    for f in fields:
        t = f.get("time")
        if t and (last is None or t > last):
            last = t
    if last:
        return last
    # Fallback (no siempre presente): resourceVersion/annotations
    return meta.get("resourceVersion")


def _read_data(ms: ManagedSecret) -> dict:
    """Lee el campo `data` de un Secret sin pasar por `serialize`. Devuelve
    un dict {key: base64str} listo para nuestro _decode/_encode."""
    try:
        secret = get_clients().core.read_namespaced_secret(ms.name, ms.namespace)
    except ApiException as exc:
        if exc.status == 404:
            raise AppError(
                f"No existe el secreto {ms.namespace}/{ms.name}",
                status_code=404,
            ) from exc
        raise
    data = getattr(secret, "data", None) or {}
    out = {}
    for k, v in data.items():
        if isinstance(v, (bytes, bytearray)):
            out[k] = base64.b64encode(bytes(v)).decode("ascii")
        else:
            out[k] = str(v)
    return out


def _read_rv(ms: ManagedSecret) -> str | None:
    try:
        secret = get_clients().core.read_namespaced_secret(ms.name, ms.namespace)
    except ApiException:
        return None
    md = getattr(secret, "metadata", None)
    return getattr(md, "resource_version", None) if md else None


def _parse_env(text: str) -> list[dict]:
    """Parsea lineas KEY=VALUE a [{key, value}], ignorando comentarios y vacias.
    No interpreta \n ni caracteres especiales dentro del valor: solo KEY=VALUE
    plano, una linea por clave (asi se monta como /app/.env que python-dotenv
    entiende con cada linea siendo una variable)."""
    items = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise AppError(f"Linea invalida en .env: {raw!r}", status_code=400)
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not _KEY_RE.match(key):
            raise AppError(
                f"Nombre de variable invalido: {key!r}. Usa [A-Za-z_][A-Za-z0-9_]*",
                status_code=400,
            )
        items.append({"key": key, "value": value})
    return items


_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SystemSecretService:

    @staticmethod
    def list_managed() -> list[dict]:
        out = []
        for key, ms in MANAGED.items():
            data = _read_data(ms)
            content = _decode(data.get(ms.key))
            if ms.kind == "env":
                parsed = _parse_env(content)
                keys_count = len(parsed)
            else:
                parsed = None
                keys_count = len(content.splitlines()) if content else 0
            out.append({
                "id": key,
                "namespace": ms.namespace,
                "name": ms.name,
                "key": ms.key,
                "kind": ms.kind,
                "keys_count": keys_count,
                "env_keys": [p["key"] for p in parsed] if parsed else None,
"size_bytes": len(content),
        })
        return out

    @staticmethod
    def get_content(secret_id: str) -> dict:
        ms = _resolve(secret_id)
        data = _read_data(ms)
        if ms.key not in data:
            raise AppError(
                f"La clave '{ms.key}' no esta en el secreto {ms.namespace}/{ms.name}",
                status_code=500,
            )
        content = _decode(data[ms.key])
        if ms.kind == "env":
            parsed = _parse_env(content)
        else:
            parsed = None
        return {
            "id": secret_id,
            "namespace": ms.namespace,
            "name": ms.name,
            "key": ms.key,
            "kind": ms.kind,
            "content": content,
            "entries": parsed,
        }

    @staticmethod
    def update_content(secret_id: str, content: str) -> dict:
        """Reemplaza la clave editable, hace rollout del deployment del sistema
        y devuelve un resumen."""
        ms = _resolve(secret_id)
        if content is None:
            raise AppError("El campo 'content' es obligatorio", status_code=400)
        if not isinstance(content, str):
            raise AppError("El campo 'content' debe ser texto", status_code=400)
        # Validacion semantica basica antes de escribir al cluster
        if ms.kind == "env":
            _parse_env(content)  # levanta AppError si hay lineas invalidas
        elif ms.kind == "text":
            if len(content.strip()) == 0:
                raise AppError("El contenido esta vacio", status_code=400)

        clients = get_clients()
        new_data_b64 = _encode(content)
        # patch parcial: solo 'data' para no pelearnos con resourceVersion
        body = {"data": {ms.key: new_data_b64}}
        try:
            old_resource_version = _read_rv(ms)
            clients.core.patch_namespaced_secret(ms.name, ms.namespace, body)
        except ApiException as exc:
            if exc.status == 404:
                raise AppError(
                    f"No existe el secreto {ms.namespace}/{ms.name}",
                    status_code=404,
                ) from exc
            raise

        # Rollout del deployment que monta este secreto
        restarted = False
        restart_error = None
        try:
            K8sService.restart_deployment(ms.namespace, ms.deployment)
            restarted = True
        except Exception as exc:
            # El secreto YA quedo actualizado; el rollout fallo. Avisamos al front.
            restart_error = str(exc)

        return {
            "id": secret_id,
            "saved": True,
            "restarted": restarted,
            "restart_error": restart_error,
            "patched_at": _ts(),
            "size_bytes": len(content),
            "old_resource_version": old_resource_version,
        }


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()
