"""Cliente PAT-based para GitHub.

Permite crear repositorios vacios en una organizacion y verificar
existencia. NO incluye OAuth, webhooks ni operaciones de push.

Convencion por defecto:
- Org: `laurel-applications` (configurable via `GITHUB_ORG`).
- Prefijo de repo: `laurel_` (pegamos el slug).
- Nombre resultante: `laurel_<slug>`.
- El usuario puede sobreescribir `github_repo_url` al crear/editar la
  Application para apuntar a un repo con nombre distinto (excepcion).
"""
import logging
import re

import requests

from app.core.errors import AppError

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
DEFAULT_ORG = "laurel-applications"
REPO_NAME_MAX = 100  # GitHub hard limit
PREFIX = "laurel_"

# DNS-1123 (igual patron que el resto del proyecto)
_DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


def _get_pat() -> str:
    """Lee el PAT de `GITHUB_PAT` (.env / modo legacy). Si esta vacio,
    intenta el system secret `github_pat` del cluster (prod). 503 si no hay."""
    from flask import current_app
    pat = (current_app.config.get("GITHUB_PAT") or "").strip()
    if pat:
        return pat
    from app.modules.system.service import SystemSecretService
    try:
        content = SystemSecretService.get_content("github_pat")["content"]
    except AppError as exc:
        if exc.status_code == 404:
            raise AppError(
                "GitHub PAT no configurado. "
                "Configurelo en .env (GITHUB_PAT) o en PUT /api/system/secrets/github_pat",
                status_code=503,
            )
        raise
    pat = (content or "").strip()
    if not pat:
        raise AppError(
            "GitHub PAT vacio. Configurelo en .env (GITHUB_PAT)",
            status_code=503,
        )
    return pat


def _get_org() -> str:
    from flask import current_app
    return current_app.config.get("GITHUB_ORG") or DEFAULT_ORG


def _repo_name(slug: str) -> str:
    name = f"{PREFIX}{slug}"
    if len(name) > REPO_NAME_MAX:
        raise AppError(
            f"GitHub repo name too long: '{name}' exceeds {REPO_NAME_MAX} chars",
            status_code=400,
        )
    return name


def _validate_slug(slug: str) -> None:
    if not slug or not _DNS_LABEL.match(slug):
        raise AppError(
            f"slug '{slug}' no es DNS-1123 valido",
            status_code=400,
        )


class GitHubService:

    @staticmethod
    def create_empty_repo(slug: str, private: bool = False) -> dict:
        """Crea un repo vacio `<org>/laurel_<slug>`.

        Returns: `{"name", "full_name", "html_url", "private"}`.
        Raises:
            AppError 400 si el slug es invalido o el nombre resultante es largo.
            AppError 503 si el PAT no esta configurado.
            AppError 502 si GitHub responde 401 (PAT invalido).
            AppError 409 si el repo ya existe (GitHub 422 name_already_exists).
        """
        _validate_slug(slug)
        org = _get_org()
        name = _repo_name(slug)
        pat = _get_pat()

        resp = requests.post(
            f"{GITHUB_API}/orgs/{org}/repos",
            headers={
                "Authorization": f"token {pat}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={
                "name": name,
                "private": private,
                "auto_init": False,
                "description": f"Laurel platform app: {slug}",
            },
            timeout=10,
        )

        if resp.status_code == 201:
            data = resp.json()
            return {
                "name": data["name"],
                "full_name": data["full_name"],
                "html_url": data["html_url"],
                "private": data["private"],
            }
        if resp.status_code == 401:
            raise AppError("GitHub authentication failed", status_code=502)
        if resp.status_code == 422:
            errors = (resp.json() or {}).get("errors") or []
            if any(e.get("code") == "name_already_exists" for e in errors):
                raise AppError(
                    f"GitHub repo '{org}/{name}' already exists",
                    status_code=409,
                )
            raise AppError(
                f"GitHub rejected the request: {resp.text[:200]}",
                status_code=502,
            )
        if resp.status_code == 404:
            raise AppError(
                f"GitHub org '{org}' not found or PAT lacks admin:org scope",
                status_code=502,
            )
        raise AppError(
            f"GitHub API error {resp.status_code}: {resp.text[:200]}",
            status_code=502,
        )

    @staticmethod
    def repo_exists(slug: str) -> bool:
        """Devuelve True si el repo `<org>/laurel_<slug>` existe."""
        _validate_slug(slug)
        org = _get_org()
        name = _repo_name(slug)
        pat = _get_pat()

        resp = requests.get(
            f"{GITHUB_API}/repos/{org}/{name}",
            headers={
                "Authorization": f"token {pat}",
                "Accept": "application/vnd.github+json",
            },
            timeout=10,
        )
        if resp.status_code == 200:
            return True
        if resp.status_code == 404:
            return False
        # Cualquier otro error: no rompemos el flujo, devolvemos False.
        logger.warning(
            "GitHub repo_exists fallo: status=%s body=%s",
            resp.status_code, resp.text[:200],
        )
        return False

    @staticmethod
    def delete_repo(slug: str) -> dict:
        """Borra el repo `<org>/laurel_<slug>`. 404 si no existe."""
        _validate_slug(slug)
        org = _get_org()
        name = _repo_name(slug)
        pat = _get_pat()
        resp = requests.delete(
            f"{GITHUB_API}/repos/{org}/{name}",
            headers={
                "Authorization": f"token {pat}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=10,
        )
        if resp.status_code in (204, 404):
            return {"deleted": resp.status_code == 204, "name": name}
        raise AppError(
            f"GitHub delete_repo error {resp.status_code}: {resp.text[:200]}",
            status_code=502,
        )