"""Cliente PAT-based para Docker Hub.

Validacion y verificacion de existencia de imagenes. NO incluye push,
build, tag ni delete (YAGNI: lo que necesita la plataforma es validar
referencias al crear apps y verificar si una imagen existe).

Convencion por defecto:
- Namespace: configurable via `DOCKER_HUB_NAMESPACE` (default `aflobaton`).
- Prefijo de imagen: `laurel_<slug>`.
- Imagen: `<namespace>/laurel_<slug>:<tag>`.

El usuario puede sobreescribir `docker_image_base` en la Application
para excepciones al prefijo.
"""
import logging
import re

import requests

from app.core.errors import AppError

logger = logging.getLogger(__name__)

DEFAULT_NAMESPACE = "aflobaton"
PREFIX = "laurel_"

# Imagen base sin tag: <algo>/<algo>
_BASE_RE = re.compile(r"^[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+$")
# Imagen completa con tag: <algo>/<algo>:<tag>
_FULL_RE = re.compile(r"^[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+:[a-zA-Z0-9._-]+$")


def _get_namespace() -> str:
    from flask import current_app
    return current_app.config.get("DOCKER_HUB_NAMESPACE", DEFAULT_NAMESPACE)


def _get_pat() -> str | None:
    """Lee el PAT del system secret `docker_pat`. None si no esta."""
    from app.modules.system.service import SystemSecretService
    try:
        content = SystemSecretService.get_content("docker_pat")["content"]
    except AppError:
        return None
    pat = (content or "").strip()
    return pat or None


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
    def image_exists(image_ref: str) -> bool | None:
        """Consulta Docker Hub. Devuelve:
            - True si la imagen existe
            - False si 404
            - None si PAT no configurado, timeout, o error de red
        """
        if not DockerHubService.validate_image_ref(image_ref):
            return None
        pat = _get_pat()
        if pat is None:
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
                url, headers={"Authorization": f"Bearer {pat}"}, timeout=5,
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