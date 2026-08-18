"""Cliente REST para Jenkins (build triggers via build token, sin SDK)."""
import logging
import re
import traceback
from typing import Literal

import requests

from app.core.errors import AppError

JENKINS_CRUMB_URL = "/crumbIssuer/api/json"

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
        """Obtiene el crumb de CSRF de Jenkins para incluir en POSTs.

        Intenta obtener el crumb desde el endpoint /json de Jenkins.
        Si falla (crumb issuer no configurado, endpoint distinto, o error),
        retorna "" para que el request continúe sin crumb (funciona si
        Jenkins tiene seguridad desactivada).

        El response esperado es JSON:
        {"_class":"hudson.security.csrf.DefaultCrumbIssuer","crumb":"e2b5..."}
        """
        base = JenkinsService._base_url()
        url = f"{base}{JENKINS_CRUMB_URL}"
        try:
            resp = requests.get(url, timeout=JENKINS_TIMEOUT)
            # Si el endpoint no existe (404) o hay otro error, retornamos
            # vacío para no bloquear el build cuando Jenkins no tiene crumb issuer.
            if resp.status_code != 200:
                logger.warning(
                    "Crumb issuer status %s en %s, continuando sin crumb",
                    resp.status_code, url
                )
                return ""
            resp.raise_for_status()
            # Parsear JSON response
            import json as _json
            try:
                crumb_data = _json.loads(resp.text)
                crumb = crumb_data.get("crumb", "")
                crumb_field = crumb_data.get("crumbRequestField", "Jenkins-Crumb")
                logger.debug(
                    "Crumb obtenido de Jenkins JSON: crumb='%s', field='%s'",
                    crumb, crumb_field
                )
                # Retornar el crumb vacío si no tiene valor
                if not crumb:
                    logger.warning("Crumb vacío en response JSON de %s", url)
                    return ""
                return crumb
            except (_json.JSONDecodeError, ValueError) as exc:
                logger.warning(
                    "No se pudo parsear JSON del crumb issuer en %s: %s",
                    url, exc
                )
                # Fallback: intentar usar resp.text strippeado
                crumb_field = resp.text.strip()
                if not crumb_field:
                    logger.warning("Crumb text vacío después de fallo JSON en %s", url)
                    return ""
                logger.warning(
                    "Usando crumb text fallback de %s: %s",
                    url, crumb_field[:50] if len(crumb_field) > 50 else crumb_field
                )
                return crumb_field
        except requests.RequestException as exc:
            logger.warning(
                "No se pudo obtener crumb de %s: %s (continuando sin crumb)",
                url, exc
            )
            return ""

    @staticmethod
    def trigger_build(slug: str, tag: str, test_cmd: str | None = None) -> dict:
        """Dispara el build remoto de `laurel_<slug>` con `tag`.

        El job de Jenkins corre un pipeline real de 3 stages (clone ->
        tests -> kaniko build+push) con `set -e`, por lo que si el TEST_CMD
        falla, no se intenta buildear la imagen. El operador configura
        `test_cmd` por app (default en el model: `echo 'no tests
        configured'`); si llega vacio o None, se manda un placeholder para
        que Jenkins no falle por param faltante.

        Ademas del test_cmd, inyectamos las credenciales de Docker Hub del
        backend (system secret `docker_pat` o env vars
        `DOCKERHUB_USER`/`DOCKERHUB_PASSWORD`) como build parameters
        PasswordParameter (auto-masked en logs). El job las usa para
        `docker login` en docker.io via kaniko. El `git clone` de repos
        privados de GitHub sigue usando `GITHUB_PAT` como env en el pod.
        Si no hay credenciales de Docker Hub, llegan como `placeholder`
        y el job cae al path sin auth (repo publico de solo-lectura).

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

        # Si test_cmd viene vacio o None, mandamos un placeholder seguro.
        # Jenkins lo corre con `set -e` + `eval`, asi que cualquier string
        # que termine con exit 0 sirve. El operador deberia haberlo
        # configurado a algo real (pytest, npm test, etc).
        test_cmd = (test_cmd or "").strip() or "echo '[no test_cmd configured]'"

        # GITHUB_PAT: para `git clone` de repos privados (el job lo usa en
        # STAGE 0). Lo leemos igual que github/service.py (env primero,
        # fallback al system secret `github_pat`).
        github_pat = ""
        try:
            from app.modules.integrations.github.service import _get_pat
            github_pat = _get_pat()
        except AppError:
            github_pat = ""

        # Credenciales de Docker Hub: mismas fuentes que docker/service.py
        # (env primero, fallback al system secret `docker_pat`).
        # El job las necesita para hacer docker login y pushear a docker.io.
        dockerhub_user = ""
        dockerhub_pass = ""
        try:
            from flask import current_app
            dockerhub_user = (
                current_app.config.get("DOCKERHUB_USER") or ""
            ).strip()
            dockerhub_pass = (
                current_app.config.get("DOCKERHUB_PASSWORD")
                or current_app.config.get("DOCKERHUB_TOKEN")
                or ""
            ).strip()
            if not dockerhub_pass:
                from app.modules.system.service import SystemSecretService
                content = SystemSecretService.get_content(
                    "docker_pat"
                )["content"]
                dockerhub_pass = (content or "").strip()
            if not dockerhub_user:
                dockerhub_user = "aflobaton"
        except AppError:
            dockerhub_pass = ""

        # IMAGE: el job espera `owner/repo` sin registry ni tag (lo
        # completa con `docker.io/${IMAGE}:${TAG}`). El operator puede
        # haber puesto `docker.io/owner/repo:tag` en docker_image_base;
        # limpiamos.
        image_no_registry = job
        # Leemos el image_base real del modelo para soportar overrides
        # (operator pone otro registry/owner). Si la app no existe o el
        # lookup falla, caemos al default `laurel_<slug>`.
        try:
            from app.modules.apps.model import Application as _App
            from app.core.db import db as _db
            app_id = _slug_to_app_id(slug)
            if app_id is not None:
                app_row = _db.session.get(_App, app_id)
                if app_row and app_row.docker_image_base:
                    ib = app_row.docker_image_base
                    if "/" in ib:
                        parts = ib.split("/")
                        ib = "/".join(parts[-2:]) if len(parts) >= 2 else ib
                    image_no_registry = ib.split(":")[0]
        except Exception as exc:
            logger.warning(
                "trigger_build: no pude resolver image_base para %s, "
                "uso default '%s': %s",
                slug, image_no_registry, exc,
            )

        logger.info(
            "jenkins.trigger_build START slug=%s tag=%s test_cmd_len=%d "
            "image=%s has_dockerhub=%s url=%s",
            slug, tag, len(test_cmd), image_no_registry,
            bool(dockerhub_user and dockerhub_pass), url,
        )

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
                    "IMAGE": image_no_registry,
                    "TEST_CMD": test_cmd,
                    # PasswordParameters en el job: Jenkins los enmascara
                    # en el log del build. Viajan en el body del POST
                    # (form-encoded), NO en la URL.
                    "GITHUB_PAT": github_pat or "placeholder",
                    "DOCKERHUB_USER": dockerhub_user or "placeholder",
                    "DOCKERHUB_PASSWORD": dockerhub_pass or "placeholder",
                },
                headers=headers,
                timeout=JENKINS_TIMEOUT,
            )
        except requests.RequestException as exc:
            logger.error(
                "jenkins.trigger_build CONN_ERROR slug=%s url=%s err=%s",
                slug, url, exc,
            )
            raise AppError("Jenkins timeout", status_code=504) from exc

        resp_text = resp.text
        resp_headers = str(resp.headers)
        location = resp.headers.get("Location", "")

        logger.info(
            "jenkins.trigger_build RESP slug=%s status=%s location=%s body_len=%d",
            slug, resp.status_code, location, len(resp_text),
        )

        if resp.status_code in (200, 201):
            # Jenkins responde 201 con un header `Location: /job/<job>/<n>/`
            # del que extraemos el numero de build. Si no esta, devolvemos
            # solo la URL del job y number=None.
            number = _parse_build_number(location)
            build_url = (
                f"{base}/job/{job}/{number}" if number else f"{base}/job/{job}"
            )
            logger.info(
                "jenkins.trigger_build OK slug=%s job=%s number=%s url=%s",
                slug, job, number, build_url,
            )
            return {"job": job, "number": number, "url": build_url}
        if resp.status_code in (401, 403):
            logger.error(
                "jenkins.trigger_build AUTH_FAIL slug=%s status=%s body=%s headers=%s",
                slug, resp.status_code, resp_text[:200], resp_headers,
            )
            raise AppError("Jenkins authentication failed", status_code=502)
        if resp.status_code == 404:
            logger.error(
                "jenkins.trigger_build JOB_NOT_FOUND slug=%s status=%s body=%s",
                slug, resp.status_code, resp_text[:200],
            )
            raise AppError(f"Jenkins job '{job}' not found", status_code=404)
        logger.error(
            "jenkins.trigger_build UNEXPECTED slug=%s status=%s body=%s",
            slug, resp.status_code, resp_text[:200],
        )
        raise AppError(
            f"Jenkins API error {resp.status_code}: {resp_text[:200]}",
            status_code=502,
        )

    @staticmethod
    def create_job(
        slug: str,
        test_cmd: str,
        image_base: str,
        github_repo_url: str | None = None,
    ) -> bool:
        """Crea el job `laurel_<slug>` en Jenkins con el pipeline CI/CD
        de 3 stages (tests -> build -> push, con `set -e`).

        Llamado por `AppsService.create` cuando el operador da de alta una
        app: asi el job existe apenas el backend queda operativo y un
        push a master puede disparar el build sin intervencion manual.

        Parametros:
            slug: slug de la app (DNS-1123). El job se llamara `laurel_<slug>`.
            test_cmd: comando de STAGE 1 (tests). Si llega vacio, se usa el
                placeholder del model ("echo no tests configured").
            image_base: nombre base de la imagen (ej `aflobaton/laurel_notas`).
                El job default tag es `0.0.1`; el webhook lo overridea con
                `app.current_version` cuando dispara el build.
            github_repo_url: URL al repo GitHub. Solo se usa para construir
                el parametro REPO del job (informativo para el pipeline).

        Returns True si el job fue creado, False si ya existia
        (idempotente: AppsService.create puede llamarse en retry).
        Raises AppError si Jenkins responde algo distinto de 200/201/409
        o si la red se cae.
        """
        _validate_slug(slug)
        token = _get_build_token()
        crumb = JenkinsService._get_crumb()
        job = f"{PREFIX}{slug}"
        base = JenkinsService._base_url()
        url = f"{base}/createItem?name={job}"

        # Repo: si el operador lo dio, usamos la parte del path; si no, el
        # default `laurel-applications/laurel_<slug>`.
        if github_repo_url:
            # "https://github.com/owner/repo" -> "owner/repo"
            parts = github_repo_url.rstrip("/").split("/")
            repo = "/".join(parts[-2:]) if len(parts) >= 2 else f"laurel-applications/{job}"
        else:
            repo = f"laurel-applications/{job}"

        # test_cmd vacio -> placeholder. Mismo criterio que trigger_build.
        test_cmd = (test_cmd or "").strip() or "echo '[no test_cmd configured]'"

        # image_base puede llegar con tag (ej 'ghcr.io/owner/repo:0.0.1' o
        # 'owner/repo:0.0.1'); el pipeline espera SIN tag + SIN registry
        # porque lo completa con `ghcr.io/${IMAGE}:${TAG}`.
        # Si el operador ya puso el registry adelante, lo sacamos.
        if "/" in image_base:
            parts = image_base.split("/")
            image_no_registry = "/".join(parts[-2:]) if len(parts) >= 2 else image_base
        else:
            image_no_registry = image_base
        image_no_registry = image_no_registry.split(":")[0]

        # XML de la config. Pipeline real de 3 stages con set -e:
        # 0. git clone (con GITHUB_PAT si llega en el trigger).
        # 1. unit tests (eval $TEST_CMD) -> falla, corta antes del build.
        # 2+3. kaniko build + push a docker.io (atomico, sin docker daemon).
        # El `IMAGE` parametro es `owner/repo` sin registry/tag; el job
        # agrega `docker.io/` y `:${TAG}`. `DOCKERHUB_USER` /
        # `DOCKERHUB_PASSWORD` y `GITHUB_PAT` son PasswordParameters
        # auto-masked en logs.
        xml = (
            "<?xml version='1.0' encoding='UTF-8'?>\n"
            "<project>\n"
            f"  <description>Job para {job}. Pipeline CI/CD real: clone -> tests -> kaniko build+push a Docker Hub. "
            "Creado por AppsService.create.</description>\n"
            "  <keepDependencies>false</keepDependencies>\n"
            "  <properties>\n"
            "    <hudson.model.ParametersDefinitionProperty>\n"
            "      <parameterDefinitions>\n"
            f"        <hudson.model.StringParameterDefinition><name>SLUG</name><defaultValue>{slug}</defaultValue><description>slug de la app</description></hudson.model.StringParameterDefinition>\n"
            f"        <hudson.model.StringParameterDefinition><name>TAG</name><defaultValue>0.0.1</defaultValue><description>tag/version a buildear</description></hudson.model.StringParameterDefinition>\n"
            f"        <hudson.model.StringParameterDefinition><name>REPO</name><defaultValue>{repo}</defaultValue><description>repo GitHub (owner/name)</description></hudson.model.StringParameterDefinition>\n"
            f"        <hudson.model.StringParameterDefinition><name>IMAGE</name><defaultValue>{image_no_registry}</defaultValue><description>imagen destino SIN registry ni tag (e.g. owner/repo)</description></hudson.model.StringParameterDefinition>\n"
            f"        <hudson.model.StringParameterDefinition><name>TEST_CMD</name><defaultValue>{test_cmd}</defaultValue><description>comando a correr en STAGE 1 (unit tests)</description></hudson.model.StringParameterDefinition>\n"
            "        <hudson.model.PasswordParameterDefinition><name>GITHUB_PAT</name><defaultValue>placeholder</defaultValue><description>GitHub PAT para git clone de repos privados. Auto-masked en logs; el backend lo envia en cada trigger.</description></hudson.model.PasswordParameterDefinition>\n"
            "        <hudson.model.PasswordParameterDefinition><name>DOCKERHUB_USER</name><defaultValue>placeholder</defaultValue><description>User de Docker Hub (namespace del repo). Auto-masked en logs; el backend lo envia en cada trigger.</description></hudson.model.PasswordParameterDefinition>\n"
            "        <hudson.model.PasswordParameterDefinition><name>DOCKERHUB_PASSWORD</name><defaultValue>placeholder</defaultValue><description>Password/token de Docker Hub para push a docker.io. Auto-masked en logs; el backend lo envia en cada trigger.</description></hudson.model.PasswordParameterDefinition>\n"
            "      </parameterDefinitions>\n"
            "    </hudson.model.ParametersDefinitionProperty>\n"
            "    <hudson.model.BuildAuthorizationTokenProperty>\n"
            f"      <authToken>{token}</authToken>\n"
            "    </hudson.model.BuildAuthorizationTokenProperty>\n"
            "  </properties>\n"
            "  <scm class=\"hudson.scm.NullSCM\"/>\n"
            "  <canRoam>true</canRoam>\n"
            "  <disabled>false</disabled>\n"
            "  <blockBuildWhenDownstreamBuilding>false</blockBuildWhenDownstreamBuilding>\n"
            "  <blockBuildWhenUpstreamBuilding>false</blockBuildWhenUpstreamBuilding>\n"
            "  <triggers/>\n"
            "  <concurrentBuild>false</concurrentBuild>\n"
            "  <builders>\n"
            "    <hudson.tasks.Shell>\n"
            "      <command>set -e\n"
            "echo &quot;=========================================&quot;\n"
            "echo &quot;STAGE 0/3: Clone repo&quot;\n"
            "echo &quot;REPO=${REPO}  TAG=${TAG}  IMAGE=${IMAGE}&quot;\n"
            "echo &quot;=========================================&quot;\n"
            "rm -rf /workspace/* /workspace/.[!.]* 2&gt;/dev/null || true\n"
            "cd /workspace\n"
            "if [ -n &quot;${GITHUB_PAT}&quot; ] &amp;&amp; [ &quot;${GITHUB_PAT}&quot; != &quot;placeholder&quot; ]; then\n"
            "  git clone &quot;https://x-access-token:${GITHUB_PAT}@github.com/${REPO}.git&quot; .\n"
            "else\n"
            "  git clone &quot;https://github.com/${REPO}.git&quot; .\n"
            "fi\n"
            "git checkout -q &quot;${TAG}&quot; 2&gt;/dev/null || git checkout -q master\n"
            "ls -la\n"
            "echo &quot;=========================================&quot;\n"
            "echo &quot;STAGE 1/3: Unit tests&quot;\n"
            "echo &quot;TEST_CMD: ${TEST_CMD}&quot;\n"
            "echo &quot;=========================================&quot;\n"
            "eval &quot;${TEST_CMD}&quot;\n"
            "echo &quot;TESTS OK&quot;\n"
            "echo &quot;=========================================&quot;\n"
            "echo &quot;STAGE 2/3 + 3/3: Build &amp; push image (kaniko)&quot;\n"
            "echo &quot;Destination: docker.io/${IMAGE}:${TAG}&quot;\n"
            "echo &quot;=========================================&quot;\n"
            "export DOCKER_CONFIG=/workspace/.docker\n"
            "mkdir -p &quot;$DOCKER_CONFIG&quot;\n"
            "if [ -n &quot;${DOCKERHUB_USER}&quot; ] &amp;&amp; [ &quot;${DOCKERHUB_USER}&quot; != &quot;placeholder&quot; ] &amp;&amp; [ -n &quot;${DOCKERHUB_PASSWORD}&quot; ] &amp;&amp; [ &quot;${DOCKERHUB_PASSWORD}&quot; != &quot;placeholder&quot; ]; then\n"
            "  AUTH=$(printf &quot;%s:%s&quot; &quot;${DOCKERHUB_USER}&quot; &quot;${DOCKERHUB_PASSWORD}&quot; | base64 -w 0)\n"
            "  printf '{&quot;auths&quot;:{&quot;docker.io&quot;:{&quot;auth&quot;:&quot;%s&quot;},&quot;https://index.docker.io/v1/&quot;:{&quot;auth&quot;:&quot;%s&quot;}}}\n' &quot;$AUTH&quot; &quot;$AUTH&quot; &gt; &quot;$DOCKER_CONFIG/config.json&quot;\n"
            "fi\n"
            "/usr/local/kaniko/executor \\\n"
            "  --context=/workspace \\\n"
            "  --dockerfile=Dockerfile \\\n"
            "  --destination=&quot;docker.io/${IMAGE}:${TAG}&quot; \\\n"
            "  --cache=true \\\n"
            "  --cache-repo=&quot;docker.io/${DOCKERHUB_USER}/kaniko-cache&quot; \\\n"
            "  --snapshot-mode=time\n"
            "echo &quot;BUILD+PUSH OK&quot;\n"
            "echo &quot;PIPELINE COMPLETE&quot;\n"
            "exit 0\n"
            "</command>\n"
            "    </hudson.tasks.Shell>\n"
            "  </builders>\n"
            "  <publishers/>\n"
            "  <buildWrappers/>\n"
            "</project>"
        )

        headers = {"Content-Type": "application/xml"}
        if crumb:
            headers["Jenkins-Crumb"] = crumb

        logger.info(
            "jenkins.create_job START slug=%s job=%s url=%s xml_len=%d",
            slug, job, url, len(xml),
        )
        try:
            resp = requests.post(url, data=xml.encode("utf-8"), headers=headers,
                                 timeout=JENKINS_TIMEOUT)
        except requests.RequestException as exc:
            logger.error(
                "jenkins.create_job CONN_ERROR slug=%s err=%s",
                slug, exc,
            )
            raise AppError(
                f"Jenkins no respondio al crear el job: {exc}",
                status_code=504,
            ) from exc

        if resp.status_code in (200, 201):
            logger.info("jenkins.create_job OK slug=%s job=%s", slug, job)
            return True
        # 409 = ya existe: idempotente.
        if resp.status_code == 409 or "already exists" in resp.text.lower():
            logger.info("jenkins.create_job EXISTS slug=%s job=%s (409, skip)", slug, job)
            return False
        # 403 con crumb invalido: lo logueamos y reintentamos sin crumb una vez.
        if resp.status_code == 403 and crumb:
            logger.warning("jenkins.create_job CRUMB_RETRY slug=%s (403 con crumb)", slug)
            try:
                resp = requests.post(url, data=xml.encode("utf-8"),
                                     headers={"Content-Type": "application/xml"},
                                     timeout=JENKINS_TIMEOUT)
            except requests.RequestException as exc:
                raise AppError(f"Jenkins no respondio: {exc}", status_code=504) from exc
            if resp.status_code in (200, 201):
                logger.info("jenkins.create_job OK_AFTER_RETRY slug=%s", slug)
                return True
            if resp.status_code == 409 or "already exists" in resp.text.lower():
                logger.info("jenkins.create_job EXISTS_AFTER_RETRY slug=%s", slug)
                return False
        logger.error(
            "jenkins.create_job REJECTED slug=%s status=%s body=%s",
            slug, resp.status_code, resp.text[:200],
        )
        raise AppError(
            f"Jenkins rechazo createItem: {resp.status_code} {resp.text[:200]}",
            status_code=502,
        )

    @staticmethod
    def delete_job(slug: str) -> bool:
        """Borra el job `laurel_<slug>`. Usado por rollback de create.

        Devuelve True si lo borro, False si no existia. Nunca lanza:
        un fallo se loguea y devuelve False (el caller ya tiene su propia
        excepcion original para propagar).
        """
        _validate_slug(slug)
        base = JenkinsService._base_url()
        crumb = JenkinsService._get_crumb()
        url = f"{base}/job/{PREFIX}{slug}/doDelete"
        headers = {}
        if crumb:
            headers["Jenkins-Crumb"] = crumb
        logger.info("jenkins.delete_job START slug=%s url=%s", slug, url)
        try:
            resp = requests.post(url, headers=headers, timeout=JENKINS_TIMEOUT)
        except requests.RequestException as exc:
            logger.warning("jenkins.delete_job CONN_ERROR slug=%s err=%s", slug, exc)
            return False
        if resp.status_code in (200, 302, 404):
            deleted = resp.status_code != 404
            logger.info(
                "jenkins.delete_job OK slug=%s status=%s deleted=%s",
                slug, resp.status_code, deleted,
            )
            return deleted
        logger.warning(
            "jenkins.delete_job UNEXPECTED slug=%s status=%s body=%s",
            slug, resp.status_code, resp.text[:200],
        )
        return False

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
        logger.info(
            "jenkins.get_build_status START slug=%s number=%s url=%s",
            slug, build_number, url,
        )
        try:
            resp = requests.get(url, timeout=JENKINS_TIMEOUT)
        except requests.RequestException as exc:
            logger.error(
                "jenkins.get_build_status CONN_ERROR slug=%s number=%s err=%s",
                slug, build_number, exc,
            )
            raise AppError("Jenkins timeout", status_code=504) from exc
        if resp.status_code == 404:
            logger.warning(
                "jenkins.get_build_status NOT_FOUND slug=%s number=%s",
                slug, build_number,
            )
            raise AppError(
                f"Jenkins build {PREFIX}{slug}/{build_number} not found",
                status_code=404,
            )
        if resp.status_code != 200:
            logger.error(
                "jenkins.get_build_status ERROR slug=%s number=%s status=%s body=%s",
                slug, build_number, resp.status_code, resp.text[:200],
            )
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
        logger.info(
            "jenkins.get_build_status OK slug=%s number=%s status=%s building=%s result=%s",
            slug, build_number, status, building, result,
        )
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


def _slug_to_app_id(slug: str) -> int | None:
    """Resuelve el `id` de la Application por slug. None si no existe
    o ya fue borrada (soft delete). Llamado en puntos frios (webhook,
    trigger) — no vale la pena cachear."""
    from app.core.db import db
    from app.modules.apps.model import Application
    row = (
        db.session.query(Application.id)
        .filter(Application.slug == slug, Application.deleted_at.is_(None))
        .first()
    )
    return row[0] if row else None
