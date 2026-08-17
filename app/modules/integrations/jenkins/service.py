"""Cliente REST para Jenkins (build triggers via build token, sin SDK)."""
import logging
import re

import requests

from app.core.errors import AppError

logger = logging.getLogger(__name__)

PREFIX = "laurel_"
JENKINS_TIMEOUT = 10

# DNS-1123 (igual patron que el resto del proyecto)
_DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


def _get_build_token() -> str:
    """Lee el build token del system secret `jenkins_token`. Lanza 503 si no esta."""
    from app.modules.system.service import SystemSecretService
    try:
        content = SystemSecretService.get_content("jenkins_token")["content"]
    except AppError as exc:
        if exc.status_code == 404:
            raise AppError(
                "Jenkins token not configured; "
                "configure it in /api/system/secrets/jenkins_token",
                status_code=503,
            )
        raise
    token = (content or "").strip()
    if not token:
        raise AppError(
            "Jenkins token not configured; "
            "configure it in /api/system/secrets/jenkins_token",
            status_code=503,
        )
    return token


def _validate_slug(slug: str) -> None:
    if not slug or not _DNS_LABEL.match(slug):
        raise AppError(f"slug '{slug}' no es DNS-1123 valido", status_code=400)


class JenkinsService:

    @staticmethod
    def _base_url() -> str:
        from flask import current_app
        return current_app.config.get("JENKINS_URL", "http://jenkins:8080").rstrip("/")

    @staticmethod
    def trigger_build(slug: str, tag: str) -> dict:
        """Dispara el build remoto de `laurel_<slug>` con `tag`.

        Auth por Jenkins build token (trigger remoto): el token va en el
        query param. Returns `{"job", "url"}`.
        Raises:
            AppError 503 si el build token no esta configurado.
            AppError 502 si Jenkins responde 401/403 (token invalido).
            AppError 404 si el job no existe.
            AppError 504 si Jenkins no responde a tiempo.
        """
        _validate_slug(slug)
        token = _get_build_token()
        job = f"{PREFIX}{slug}"
        base = JenkinsService._base_url()
        url = f"{base}/job/{job}/buildWithParameters"

        try:
            resp = requests.post(
                url,
                params={"token": token},
                data={
                    "SLUG": slug,
                    "TAG": tag,
                    "REPO": f"laurel-applications/{job}",
                    "IMAGE": f"aflobaton/{job}:{tag}",
                },
                timeout=JENKINS_TIMEOUT,
            )
        except requests.RequestException as exc:
            logger.warning("Jenkins timeout en %s: %s", url, exc)
            raise AppError("Jenkins timeout", status_code=504) from exc

        if resp.status_code in (200, 201):
            return {"job": job, "url": f"{base}/job/{job}"}
        if resp.status_code in (401, 403):
            raise AppError("Jenkins authentication failed", status_code=502)
        if resp.status_code == 404:
            raise AppError(f"Jenkins job '{job}' not found", status_code=404)
        raise AppError(
            f"Jenkins API error {resp.status_code}: {resp.text[:200]}",
            status_code=502,
        )

    @staticmethod
    def job_exists(slug: str) -> bool:
        """True si el job `laurel_<slug>` existe en Jenkins. Nunca lanza."""
        _validate_slug(slug)
        base = JenkinsService._base_url()
        try:
            resp = requests.get(
                f"{base}/job/{PREFIX}{slug}/api/json",
                timeout=JENKINS_TIMEOUT,
            )
        except requests.RequestException as exc:
            logger.warning("Jenkins job_exists fallo para %s: %s", slug, exc)
            return False
        return resp.status_code == 200
