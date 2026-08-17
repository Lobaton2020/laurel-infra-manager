"""Cliente REST para Jenkins (build triggers via build token, sin SDK)."""
import logging
import re
import traceback
from typing import Literal

import requests

from app.core.errors import AppError

JENKINS_CRUMB_URL = "/crumbIssuer/api/xml?xpath=concat(//crumbRequestField,%20//crumb)"

logger = logging.getLogger(__name__)

PREFIX = "laurel_"
JENKINS_TIMEOUT = 10

# Estados de Jenkins segun el JSON de /job/<job>/<n>/api/json.
# result: "SUCCESS" | "FAILURE" | "UNSTABLE" | "ABORTED" | "NOT_BUILT" | None
# building: True mientras corre, False cuando termino.
BuildStatus = Literal["pending", "running", "success", "failed", "aborted"]

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
    def _get_crumb() -> str:
        """Obtiene el crumb de CSRF de Jenkins para incluir en POSTs."""
        base = JenkinsService._base_url()
        url = f"{base}{JENKINS_CRUMB_URL}"
        try:
            resp = requests.get(url, timeout=JENKINS_TIMEOUT)
            resp.raise_for_status()
            # Formato: "Jenkins-Crumb: abc123"
            crumb_field = resp.text.strip()
            return crumb_field
        except requests.RequestException as exc:
            logger.warning("No se pudo obtener crumb de Jenkins: %s", exc)
            return ""

    @staticmethod
    def trigger_build(slug: str, tag: str) -> dict:
        """Dispara el build remoto de `laurel_<slug>` con `tag`.

        Auth por Jenkins build token (trigger remoto): el token va en el
        query param. Returns `{"job", "number", "url"}` donde `url` es
        la URL directa al build (con su numero) si se pudo extraer del
        header `Location`; si no, cae a la URL del job.
        Raises:
            AppError 503 si el build token no esta configurado.
            AppError 502 si Jenkins responde 401/403 (token invalido).
            AppError 404 si el job no existe.
            AppError 504 si Jenkins no responde a tiempo.
        """
        _validate_slug(slug)
        token = _get_build_token()
        crumb = JenkinsService._get_crumb()
        job = f"{PREFIX}{slug}"
        base = JenkinsService._base_url()
        url = f"{base}/job/{job}/buildWithParameters"

        headers = {}
        if crumb:
            headers["Jenkins-Crumb"] = crumb

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
                headers=headers,
                timeout=JENKINS_TIMEOUT,
            )
        except requests.RequestException as exc:
            logger.error(
                "Jenkins timeout excepcion en %s: %s\n%s",
                url, exc, traceback.format_exc()
            )
            raise AppError("Jenkins timeout", status_code=504) from exc

        resp_text = resp.text
        resp_headers = str(resp.headers)

        if resp.status_code in (200, 201):
            # Jenkins responde 201 con un header `Location: /job/<job>/<n>/`
            # del que extraemos el numero de build. Si no esta, devolvemos
            # solo la URL del job y number=None.
            number = _parse_build_number(resp.headers.get("Location"))
            build_url = (
                f"{base}/job/{job}/{number}" if number else f"{base}/job/{job}"
            )
            return {"job": job, "number": number, "url": build_url}
        if resp.status_code in (401, 403):
            logger.error(
                "Jenkins authentication failed (status=%s)\n"
                "Body: %s\n"
                "Headers: %s",
                resp.status_code, resp_text, resp_headers
            )
            raise AppError("Jenkins authentication failed", status_code=502)
        if resp.status_code == 404:
            logger.error(
                "Jenkins job not found (status=%s)\n"
                "Job: %s\n"
                "Body: %s",
                resp.status_code, job, resp_text
            )
            raise AppError(f"Jenkins job '{job}' not found", status_code=404)
        raise AppError(
            f"Jenkins API error {resp.status_code}: {resp_text[:200]}",
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
            logger.warning(
                "Jenkins job_exists fallo para %s: %s\n%s",
                slug, exc, traceback.format_exc()
            )
            return False
        return resp.status_code == 200

    @staticmethod
    def get_build_status(slug: str, build_number: int) -> dict:
        """Consulta el status actual de un build de Jenkins.

        Returns: `{"status", "building", "result", "timestamp"}` mapeado
        a los valores del modelo AppBuild:
        - building=True  -> 'running'
        - building=False -> result mapeado: SUCCESS=success, FAILURE=failed,
                            UNSTABLE=failed, ABORTED=aborted, NOT_BUILT=failed
        - 404/otro: lanza AppError
        """
        _validate_slug(slug)
        base = JenkinsService._base_url()
        url = f"{base}/job/{PREFIX}{slug}/{build_number}/api/json"
        try:
            resp = requests.get(url, timeout=JENKINS_TIMEOUT)
        except requests.RequestException as exc:
            logger.error(
                "Jenkins get_build_status fallo: %s\n%s",
                exc, traceback.format_exc()
            )
            raise AppError("Jenkins timeout", status_code=504) from exc
        if resp.status_code == 404:
            raise AppError(
                f"Jenkins build {PREFIX}{slug}/{build_number} not found",
                status_code=404,
            )
        if resp.status_code != 200:
            raise AppError(
                f"Jenkins API error {resp.status_code}: {resp.text[:200]}",
                status_code=502,
            )
        data = resp.json()
        building = bool(data.get("building"))
        result = data.get("result")
        if building:
            status: BuildStatus = "running"
        else:
            status = _map_jenkins_result(result)
        return {
            "status": status,
            "building": building,
            "result": result,
            "timestamp": data.get("timestamp"),
        }


def _parse_build_number(location_header: str | None) -> int | None:
    """Extrae el build number del header `Location` de un POST a buildWithParameters.

    Formato esperado: 'http://jenkins/job/<job>/<n>/' o '/job/<job>/<n>/'.
    Devuelve None si el header no se puede parsear.
    """
    if not location_header:
        return None
    # Tomamos el ultimo segmento con digitos.
    parts = [p for p in location_header.rstrip("/").split("/") if p]
    for p in reversed(parts):
        if p.isdigit():
            return int(p)
    return None


def _map_jenkins_result(result: str | None) -> BuildStatus:
    """Traduce el `result` de Jenkins al enum de AppBuild."""
    if result == "SUCCESS":
        return "success"
    if result == "ABORTED":
        return "aborted"
    if result in ("FAILURE", "UNSTABLE", "NOT_BUILT"):
        return "failed"
    # result=None con building=False es raro; lo marcamos como failed
    # para no dejarlo colgado en 'pending' para siempre.
    return "failed"
