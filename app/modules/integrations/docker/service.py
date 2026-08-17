"""Cliente Docker Hub via JWT bearer.

Validacion y verificacion de existencia de imagenes. NO incluye push,
build, tag ni delete (YAGNI: lo que necesita la plataforma es validar
referencias al crear apps y verificar si una imagen existe).

Auth: Docker Hub NO acepta un PAT directamente como `Authorization: Bearer
<PAT>`. Hay que pasar el PAT por Basic auth contra `/v2/auth/token` para
recibir un JWT bearer de ~30s, y usar ese JWT en las llamadas siguientes.
El helper `_hub_bearer()` cachea el JWT en memoria hasta 30s antes de
volver a pedirlo. Si `/v2/auth/token` no esta disponible (algunas cuentas
personales antiguas), se hace fallback al PAT crudo.

Convencion por defecto:
- Namespace: configurable via `DOCKER_HUB_NAMESPACE` (default `aflobaton`).
- Prefijo de imagen: `laurel_<slug>`.
- Imagen: `<namespace>/laurel_<slug>:<tag>`.

El usuario puede sobreescribir `docker_image_base` en la Application
para excepciones al prefijo.
"""
import logging
import re
import time

import requests

from app.core.errors import AppError

logger = logging.getLogger(__name__)

DEFAULT_NAMESPACE = "aflobaton"
PREFIX = "laurel_"

# Imagen base sin tag: <algo>/<algo>
_BASE_RE = re.compile(r"^[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+$")
# Imagen completa con tag: <algo>/<algo>:<tag>
_FULL_RE = re.compile(r"^[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+:[a-zA-Z0-9._-]+$")

# DNS-1123 (igual patron que el resto del proyecto)
_DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")

# Cache modulo-nivel del JWT bearer de Docker Hub. Se invalida a los 30s
# (los tokens de Docker Hub expiran en ~60s; cortamos antes para evitar
# race conditions). Tests lo resetean.
_BEARER: dict | None = None
_BEARER_TTL_S = 30


def _validate_slug(slug: str) -> None:
    if not slug or not _DNS_LABEL.match(slug):
        raise AppError(
            f"slug '{slug}' no es DNS-1123 valido",
            status_code=400,
        )


def _repo_name(slug: str) -> str:
    return f"{PREFIX}{slug}"


def _get_namespace() -> str:
    from flask import current_app
    return current_app.config.get("DOCKER_HUB_NAMESPACE") or DEFAULT_NAMESPACE


def _get_pat() -> str | None:
    """Lee el token de `DOCKER_HUB_TOKEN` (.env / modo legacy). Si esta vacio,
    intenta el system secret `docker_pat` del cluster (prod). None si no hay."""
    from flask import current_app
    pat = (current_app.config.get("DOCKER_HUB_TOKEN") or "").strip()
    if pat:
        return pat
    from app.modules.system.service import SystemSecretService
    try:
        content = SystemSecretService.get_content("docker_pat")["content"]
    except AppError:
        return None
    pat = (content or "").strip()
    return pat or None


def _hub_bearer(force_refresh: bool = False) -> str | None:
    """Devuelve un JWT bearer de Docker Hub valido para `Authorization`.

    Flujo:
      1. Si no hay namespace o PAT -> None.
      2. Si hay un JWT cacheado y aun no expira -> devolverlo.
      3. `GET https://hub.docker.com/v2/auth/token` con Basic auth
         (username=namespace, password=PAT). Docker Hub intercambia el PAT
         por un JWT de ~60s.
      4. Cachear `(token, expires_at)` modulo-nivel con TTL conservador
         (30s) para evitar expiracion en vuelo.
      5. Si la llamada a `/v2/auth/token` falla -> log warning + fallback
         al PAT crudo (compatibilidad con cuentas que aun aceptan bearer
         directo, ya no es la norma).

    Devuelve None solo si no hay credenciales configuradas.
    """
    global _BEARER
    namespace = _get_namespace()
    pat = _get_pat()
    if not namespace or not pat:
        return None

    now = time.time()
    if not force_refresh and _BEARER and _BEARER["expires"] > now:
        return _BEARER["token"]

    try:
        resp = requests.get(
            "https://hub.docker.com/v2/auth/token",
            auth=(namespace, pat),
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()
        token = data.get("token") or data.get("access_token")
        if not token:
            raise ValueError("auth/token response sin 'token'")
        _BEARER = {"token": token, "expires": now + _BEARER_TTL_S}
        return token
    except (requests.RequestException, ValueError) as exc:
        logger.warning(
            "DockerHub /v2/auth/token fallo (%s); fallback a PAT crudo",
            exc,
        )
        return pat


def reset_bearer_cache() -> None:
    """Limpia el cache del JWT. Tests de aislamiento."""
    global _BEARER
    _BEARER = None


class DockerHubService:

    @staticmethod
    def validate_image_ref(image_ref: str) -> bool:
        """True si la imagen tiene formato valido. No consulta Docker Hub."""
        if not image_ref or not _FULL_RE.match(image_ref):
            return False
        return True

    @staticmethod
    def validate_image_base(image_base: str) -> bool:
        """True si el image_base (sin tag) tiene formato valido."""
        if not image_base or not _BASE_RE.match(image_base):
            return False
        return True

    @staticmethod
    def suggested_base(slug: str) -> str:
        """Genera el image_base sugerido: `<namespace>/laurel_<slug>`."""
        return f"{_get_namespace()}/{PREFIX}{slug}"

    @staticmethod
    def create_empty_repo(slug: str, description: str | None = None, private: bool = False) -> dict:
        """Crea un repo vacio `laurel_<slug>` en Docker Hub.

        Docker Hub NO crea repos automaticamente en el primer push como hace
        GitHub: el repo debe existir para que `docker push` funcione. Por eso
        lo creamos aqui al crear la Application, igual que GitHubService.

        Returns: `{"namespace", "name", "full_name", "is_private"}`.
        Raises:
            AppError 400 si el slug es invalido o el nombre resultante es largo.
            AppError 503 si el PAT no esta configurado.
            AppError 502 si Docker Hub responde 401/403 (PAT invalido) u otro error.
            AppError 409 si el repo ya existe (Docker Hub 409).
        """
        _validate_slug(slug)
        namespace = _get_namespace()
        name = _repo_name(slug)
        bearer = _hub_bearer()
        if bearer is None:
            raise AppError(
                "Docker Hub PAT no configurado. "
                "Configurelo en PUT /api/system/secrets/docker_pat",
                status_code=503,
            )

        resp = requests.post(
            f"https://hub.docker.com/v2/repositories/{namespace}/",
            headers={"Authorization": f"Bearer {bearer}"},
            json={
                "name": name,
                "description": description or f"Laurel platform app: {slug}",
                "is_private": private,
            },
            timeout=10,
        )

        if resp.status_code == 201:
            data = resp.json()
            return {
                "namespace": data.get("namespace"),
                "name": data.get("name"),
                "full_name": data.get("full_name"),
                "is_private": data.get("is_private"),
            }
        if resp.status_code == 409:
            raise AppError(
                f"Docker Hub repo '{namespace}/{name}' already exists",
                status_code=409,
            )
        raise AppError(
            f"Docker Hub API error {resp.status_code}: {resp.text[:200]}",
            status_code=502,
        )

    @staticmethod
    def image_exists(image_ref: str) -> bool | None:
        """Consulta Docker Hub. Devuelve:
            - True si la imagen existe
            - False si 404
            - None si PAT no configurado, timeout, o error de red
        """
        if not DockerHubService.validate_image_ref(image_ref):
            return None
        bearer = _hub_bearer()
        if bearer is None:
            return None

        namespace, _, rest = image_ref.partition("/")
        repo, _, tag = rest.partition(":")
        # API v2 de Docker Hub
        url = (
            f"https://hub.docker.com/v2/repositories/{namespace}/{repo}/"
            f"tags/{tag}/"
        )
        try:
            resp = requests.get(
                url, headers={"Authorization": f"Bearer {bearer}"}, timeout=5,
            )
        except requests.RequestException as exc:
            logger.warning("DockerHub image_exists fallo de red: %s", exc)
            return None

        if resp.status_code == 200:
            return True
        if resp.status_code == 404:
            return False
        logger.warning(
            "DockerHub image_exists status=%s body=%s",
            resp.status_code, resp.text[:200],
        )
        return None