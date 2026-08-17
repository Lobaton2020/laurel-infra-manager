"""Cliente de registro de contenedores (GitHub Container Registry - GHCR).

Validacion y construccion de referencias a imagenes. NO incluye push,
build, tag ni delete (YAGNI: lo que necesita la plataforma es validar
referencias al crear apps y saber la imagen base por defecto).

Convencion:
- Registry: `ghcr.io` (GitHub Container Registry). El backend ya no hace
  llamadas HTTP para crear el repo: GHCR crea el paquete en el primer
  `docker push`, igual que Docker Hub no creaba nada antes.
- Owner: configurable via `GHCR_OWNER` (default `laurel-applications`,
  misma org donde se crean los repos de codigo).
- Prefijo de imagen: `laurel_<slug>`.
- Imagen base sin tag: `ghcr.io/<owner>/laurel_<slug>`.
- Imagen completa con tag: `ghcr.io/<owner>/laurel_<slug>:<tag>`.

El usuario puede sobreescribir `docker_image_base` en la Application
para excepciones al prefijo.
"""
import logging
import re

from app.core.errors import AppError

logger = logging.getLogger(__name__)

DEFAULT_OWNER = "laurel-applications"
PREFIX = "laurel_"

# Imagen base sin tag: acepta 2 segmentos (`owner/repo`) o 3 segmentos
# (`ghcr.io/owner/repo`).
_BASE_RE = re.compile(r"^[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+(/[a-zA-Z0-9._-]+)?$")
# Imagen completa con tag: igual formato que la base + :tag al final.
_FULL_RE = re.compile(
    r"^[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+(/[a-zA-Z0-9._-]+)?:[a-zA-Z0-9._-]+$"
)

# DNS-1123 (igual patron que el resto del proyecto)
_DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


def _validate_slug(slug: str) -> None:
    if not slug or not _DNS_LABEL.match(slug):
        raise AppError(
            f"slug '{slug}' no es DNS-1123 valido",
            status_code=400,
        )


def _repo_name(slug: str) -> str:
    return f"{PREFIX}{slug}"


def _get_owner() -> str:
    from flask import current_app
    return current_app.config.get("GHCR_OWNER") or DEFAULT_OWNER


class ContainerRegistryService:

    @staticmethod
    def validate_image_ref(image_ref: str) -> bool:
        """True si la imagen tiene formato valido. No consulta el registry."""
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
        """Genera el image_base sugerido: `ghcr.io/<owner>/laurel_<slug>`."""
        return f"ghcr.io/{_get_owner()}/{PREFIX}{slug}"