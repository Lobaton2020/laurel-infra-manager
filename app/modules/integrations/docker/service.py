"""Cliente de Docker Hub (API v2: login + CRUD de repos).

Convencion por defecto:
- Registry: `docker.io` (Docker Hub). El `docker push` desde Jenkins
  va a `docker.io/<user>/<repo>:<tag>`.
- User: configurable via `DOCKERHUB_USER` (default `aflobaton`).
- Password: `DOCKERHUB_PASSWORD` (literal) o `DOCKERHUB_TOKEN` (alias).
  Fallback al system secret `docker_pat` (gestionado por la
  plataforma en el cluster).
- Prefijo de repo: `laurel_<slug>` (alineado con el resto del proyecto).
- Repo publico por defecto. El operador puede pasar `is_private=True`
  al crear si quiere privado (no se usa en el flujo actual).

Auth flow:
1. `POST /v2/users/login/` con {username, password} -> JWT.
2. Todas las llamadas siguientes llevan `Authorization: JWT <token>`.
3. El JWT dura ~5 min; lo cacheamos en proceso con TTL para no
   reloginear en cada request.
"""
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request

from flask import current_app

from app.core.errors import AppError

logger = logging.getLogger(__name__)

DOCKERHUB_API = "https://hub.docker.com"
DEFAULT_USER = "aflobaton"
PREFIX = "laurel_"
JWT_TTL_SECONDS = 300  # 5 min (Docker Hub docs)
HUB_TIMEOUT = 10

# image_base: `owner/repo` o `docker.io/owner/repo`.
_BASE_RE = re.compile(r"^[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+(/[a-zA-Z0-9._-]+)?$")
# image_ref: `owner/repo:tag` o `docker.io/owner/repo:tag`.
_FULL_RE = re.compile(
    r"^[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+(/[a-zA-Z0-9._-]+)?:[a-zA-Z0-9._-]+$"
)

# Cache de JWT en proceso (5 min de vida). El lock es para no hacer 2
# logins en paralelo si la primera llamada expira justo cuando entra
# otra (poco probable, pero barato).
_JWT_CACHE: dict = {"token": None, "expires_at": 0.0}
_JWT_LOCK = threading.Lock()


def _get_user() -> str:
    user = (current_app.config.get("DOCKERHUB_USER") or "").strip()
    if user:
        return user
    return DEFAULT_USER


def _get_creds() -> tuple[str, str]:
    """Lee DOCKERHUB_USER y DOCKERHUB_PASSWORD (o DOCKERHUB_TOKEN).
    Fallback al system secret `docker_pat` del cluster.
    503 si no esta configurado en ningun lado.
    """
    user = (current_app.config.get("DOCKERHUB_USER") or "").strip()
    pwd = (
        current_app.config.get("DOCKERHUB_PASSWORD")
        or current_app.config.get("DOCKERHUB_TOKEN")
        or ""
    ).strip()
    if user and pwd:
        return user, pwd

    from app.modules.system.service import SystemSecretService
    try:
        content = SystemSecretService.get_content("docker_pat")["content"]
    except AppError as exc:
        if exc.status_code in (404, 403):
            raise AppError(
                "Docker Hub credentials not configured. "
                "Set DOCKERHUB_PASSWORD in .env or "
                "PUT /api/system/secrets/docker_pat",
                status_code=503,
            )
        raise
    pwd = (content or "").strip()
    if not pwd:
        raise AppError(
            "Docker Hub password/secret vacio. "
            "Configurelo en .env (DOCKERHUB_PASSWORD) o en el system secret.",
            status_code=503,
        )
    if not user:
        user = DEFAULT_USER
    return user, pwd


def _login() -> str:
    """POST /v2/users/login/ -> JWT. Cachea 5 min en proceso."""
    now = time.time()
    with _JWT_LOCK:
        if _JWT_CACHE["token"] and _JWT_CACHE["expires_at"] > now:
            return _JWT_CACHE["token"]
        user, pwd = _get_creds()
        body = json.dumps({"username": user, "password": pwd}).encode("utf-8")
        req = urllib.request.Request(
            f"{DOCKERHUB_API}/v2/users/login/",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        logger.info("dockerhub.login START user=%s url=%s", user, req.full_url)
        try:
            with urllib.request.urlopen(req, timeout=HUB_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body_excerpt = exc.read()[:200].decode("utf-8", errors="ignore")
            logger.error(
                "dockerhub.login AUTH_FAIL user=%s status=%s body=%s",
                user, exc.code, body_excerpt,
            )
            raise AppError(
                f"Docker Hub login rejected: {exc.code}", status_code=502
            ) from exc
        except urllib.error.URLError as exc:
            logger.error("dockerhub.login CONN_ERROR user=%s err=%s", user, exc)
            raise AppError("Docker Hub connection error", status_code=504) from exc
        token = data.get("token", "")
        if not token:
            logger.error("dockerhub.login EMPTY_TOKEN user=%s resp=%s", user, data)
            raise AppError("Docker Hub returned empty token", status_code=502)
        _JWT_CACHE["token"] = token
        _JWT_CACHE["expires_at"] = now + JWT_TTL_SECONDS
        logger.info(
            "dockerhub.login OK user=%s ttl=%ds", user, JWT_TTL_SECONDS
        )
        return token


def _auth_headers() -> dict:
    return {
        "Authorization": f"JWT {_login()}",
        "Content-Type": "application/json",
    }


def _repo_name(slug: str) -> str:
    return f"{PREFIX}{slug}"


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
        """Genera el image_base sugerido: `docker.io/<user>/laurel_<slug>`."""
        return f"docker.io/{_get_user()}/{PREFIX}{slug}"

    @staticmethod
    def create_repo(
        slug: str,
        description: str = "",
        is_private: bool = False,
    ) -> dict:
        """Crea el repo `<user>/laurel_<slug>` en Docker Hub.

        Returns: `{"name", "namespace", "existed"}` donde `existed=True`
        significa que ya estaba (Docker Hub respondio 409): el caller
        puede tratarlo como exito idempotente.

        Raises:
            AppError 503 si las credenciales no estan configuradas.
            AppError 502 si Docker Hub rechaza la creacion (no 409).
            AppError 504 si hay timeout/red caida.
        """
        name = _repo_name(slug)
        user = _get_user()
        body = {
            "name": name,
            "namespace": user,
            "description": (description or "")[:100],
            "full_description": description or "",
            "is_private": is_private,
        }
        req = urllib.request.Request(
            f"{DOCKERHUB_API}/v2/repositories/",
            data=json.dumps(body).encode("utf-8"),
            headers=_auth_headers(),
            method="POST",
        )
        logger.info(
            "dockerhub.create_repo START slug=%s repo=%s/%s is_private=%s url=%s",
            slug, user, name, is_private, req.full_url,
        )
        try:
            with urllib.request.urlopen(req, timeout=HUB_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                logger.info(
                    "dockerhub.create_repo OK slug=%s repo=%s/%s",
                    slug, data.get("namespace", user), data.get("name", name),
                )
                return {
                    "name": data.get("name", name),
                    "namespace": data.get("namespace", user),
                    "user": data.get("user", user),
                    "existed": False,
                }
        except urllib.error.HTTPError as exc:
            body_excerpt = exc.read()[:200].decode("utf-8", errors="ignore")
            # 409: ya existe -> idempotente. Docker Hub ademas responde
            # 401 con un body parecido si el repo es privado y no sos
            # dueno; ese caso NO lo tratamos como exito.
            if exc.code == 409 or "already exists" in body_excerpt.lower():
                logger.info(
                    "dockerhub.create_repo EXISTS slug=%s repo=%s/%s (409)",
                    slug, user, name,
                )
                return {
                    "name": name,
                    "namespace": user,
                    "user": user,
                    "existed": True,
                }
            logger.error(
                "dockerhub.create_repo FAIL slug=%s status=%s body=%s",
                slug, exc.code, body_excerpt,
            )
            raise AppError(
                f"Docker Hub rejected create_repo: {exc.code} {body_excerpt[:100]}",
                status_code=502,
            ) from exc
        except urllib.error.URLError as exc:
            logger.error(
                "dockerhub.create_repo CONN_ERROR slug=%s err=%s", slug, exc
            )
            raise AppError("Docker Hub connection error", status_code=504) from exc

    @staticmethod
    def repo_exists(slug: str) -> bool:
        """True si el repo `<user>/laurel_<slug>` existe en Docker Hub.
        Nunca lanza: cualquier error (incluido credenciales faltantes) se
        loguea y devuelve False."""
        user = _get_user()
        name = _repo_name(slug)
        logger.info("dockerhub.repo_exists slug=%s repo=%s/%s", slug, user, name)
        try:
            req = urllib.request.Request(
                f"{DOCKERHUB_API}/v2/repositories/{user}/{name}/",
                headers=_auth_headers(),
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=HUB_TIMEOUT) as resp:
                return resp.status == 200
        except AppError as exc:
            logger.warning("dockerhub.repo_exists slug=%s err=%s", slug, exc.message)
            return False
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return False
            logger.warning(
                "dockerhub.repo_exists slug=%s status=%s", slug, exc.code
            )
            return False
        except urllib.error.URLError as exc:
            logger.warning("dockerhub.repo_exists slug=%s err=%s", slug, exc)
            return False

    @staticmethod
    def delete_repo(slug: str) -> dict:
        """Borra el repo `<user>/laurel_<slug>` en Docker Hub.

        Returns: `{"deleted", "existed", "name", "namespace"}`.
        404 si no existia: devuelve `deleted=False, existed=False` sin
        lanzar. Cualquier otro 4xx/5xx lanza AppError 502.
        """
        user = _get_user()
        name = _repo_name(slug)
        req = urllib.request.Request(
            f"{DOCKERHUB_API}/v2/repositories/{user}/{name}/",
            headers=_auth_headers(),
            method="DELETE",
        )
        logger.info(
            "dockerhub.delete_repo START slug=%s repo=%s/%s", slug, user, name
        )
        try:
            with urllib.request.urlopen(req, timeout=HUB_TIMEOUT) as resp:
                logger.info(
                    "dockerhub.delete_repo OK slug=%s status=%s",
                    slug, resp.status,
                )
                return {
                    "deleted": True,
                    "existed": True,
                    "name": name,
                    "namespace": user,
                }
        except urllib.error.HTTPError as exc:
            body_excerpt = exc.read()[:200].decode("utf-8", errors="ignore")
            if exc.code == 404:
                logger.info(
                    "dockerhub.delete_repo NOT_FOUND slug=%s repo=%s/%s",
                    slug, user, name,
                )
                return {
                    "deleted": False,
                    "existed": False,
                    "name": name,
                    "namespace": user,
                }
            logger.error(
                "dockerhub.delete_repo FAIL slug=%s status=%s body=%s",
                slug, exc.code, body_excerpt,
            )
            raise AppError(
                f"Docker Hub delete_repo error {exc.code} {body_excerpt[:100]}",
                status_code=502,
            ) from exc
        except urllib.error.URLError as exc:
            logger.error(
                "dockerhub.delete_repo CONN_ERROR slug=%s err=%s", slug, exc
            )
            raise AppError("Docker Hub connection error", status_code=504) from exc
