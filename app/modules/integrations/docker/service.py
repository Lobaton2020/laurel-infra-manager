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

    @staticmethod
    def delete_package(slug: str) -> dict:
        """Borra el paquete GHCR `ghcr.io/<owner>/<repo>`.

        Auth: el PAT se intercambia por un JWT bearer contra
        `ghcr.io/token?service=ghcr.io&scope=repository:<owner>/<repo>:delete`
        (Basic auth con owner+PAT). Requiere scope `delete:packages` en el PAT.
        404 si el paquete no existe.
        """
        import requests
        from app.modules.system.service import SystemSecretService
        from flask import current_app

        owner = _get_owner()
        name = _repo_name(slug)

        # Resolver PAT: prioridad .env, fallback system secret del cluster.
        pat = (current_app.config.get("GITHUB_PAT") or "").strip()
        if not pat:
            try:
                content = SystemSecretService.get_content("github_pat")["content"]
                pat = (content or "").strip()
            except Exception:
                pat = ""
        if not pat:
            logger.warning("ghcr_delete_package sin PAT; saltando")
            return {"deleted": False, "skipped": "pat_missing"}

        # Token exchange contra ghcr.io.
        try:
            tok = requests.get(
                "https://ghcr.io/token",
                params={
                    "service": "ghcr.io",
                    "scope": f"repository:{owner}/{name}:delete",
                },
                auth=(owner, pat),
                timeout=10,
            )
        except requests.RequestException as exc:
            logger.warning("ghcr_token_exchange fallo: %s", exc)
            return {"deleted": False, "error": str(exc)}
        if tok.status_code != 200:
            return {"deleted": False, "error": f"token exchange {tok.status_code}"}
        bearer = tok.json().get("token", "")

        try:
            r = requests.delete(
                f"https://ghcr.io/v2/{owner}/{name}/",
                headers={"Authorization": f"Bearer {bearer}"},
                timeout=10,
            )
        except requests.RequestException as exc:
            return {"deleted": False, "error": str(exc)}
        if r.status_code in (202, 204):
            return {"deleted": True, "name": name}
        if r.status_code == 404:
            return {"deleted": False, "name": name, "existed": False}
        return {"deleted": False, "error": f"ghcr {r.status_code}: {r.text[:200]}"}