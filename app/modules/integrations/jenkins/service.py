"""Cliente REST para Jenkins (build triggers via build token, sin SDK)."""
import logging
import os
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
    def trigger_build(slug: str, tag: str) -> dict:
        """Dispara el build remoto de `laurel_<slug>` con `tag`.

        Este metodo SOLO EJECUTA el build. NO crea ni modifica la
        definicion del job en Jenkins: eso es responsabilidad de
        `ensure_job_config`, que `AppsService.create` llama al dar de
        alta la app. Asumimos que el job ya existe; si no, Jenkins
        responde 404 y propagamos `AppError(404)`.

        Separacion (patron del MVP en `./mvp/jenkins/pipeline.py`):
          - `ensure_job_config`  -> toca config.xml (idempotente)
          - `trigger_build`       -> POST /buildWithParameters (ejecuta)

        El job de Jenkins es un pipeline declarativo de 4 stages (Init ->
        Clone -> Test -> Build+Push) generado por `ensure_job_config`. El
        Test stage autodetecta el framework del repo clonado; si no hay
        tests, el stage sale con exit 0 (no rompe el build).

        Parametros enviados al job:
          - TAG (string): la version a buildear (auto-increment desde Docker
            Hub tags, calculada por el webhook).
          - REPO (string): `owner/name` del repo GitHub (publico, no requiere
            PAT — todos los repos de laurel-applications son publicos).
          - IMAGE (string): `owner/repo` sin registry ni tag (lo completa
            el pipeline con `docker.io/${IMAGE}:${TAG}`).
          - DOCKERHUB_USER / DOCKERHUB_PASSWORD (PasswordParameter,
            masked): credenciales de Docker Hub para el push via kaniko.
            Llegan como `placeholder` si no estan configuradas; el job
            cae al path sin auth (repo publico de solo-lectura).

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
            "jenkins.trigger_build START slug=%s tag=%s image=%s has_dockerhub=%s url=%s",
            slug, tag, image_no_registry,
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
                    "TAG": tag,
                    "REPO": f"laurel-applications/{job}",
                    "IMAGE": image_no_registry,
                    # PasswordParameters en el job: Jenkins los enmascara
                    # en el log del build. Viajan en el body del POST
                    # (form-encoded), NO en la URL.
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
            # Jenkins responde 201 con un header `Location` que apunta
            # al build. El formato varia segun el estado:
            #   - /job/<job>/<n>/          -> build ya tiene numero asignado
            #   - /queue/item/<id>/         -> build encolado, numero pendiente
            # Extraemos la URL canonica (lo que Jenkins considera su
            # identificador oficial) y, si la URL es de queue, resolvemos
            # el numero con un GET al queue. Asi nunca guardamos un
            # `jenkins_number` que el polling despues no pueda usar.
            build_url, number = _resolve_build_location(base, location, job)
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
    def ensure_job_config(
        slug: str,
        image_base: str,
        github_repo_url: str | None = None,
    ) -> bool:
        """Asegura que el job `laurel_<slug>` exista en Jenkins con la config
        actual (Pipeline declarativo de 4 stages: Init -> Clone -> Test ->
        Build+Push). Idempotente: si ya existe, lo BORRA y lo RECREA con
        el XML nuevo (asi un job creado con codigo viejo se actualiza
        al pipeline nuevo automaticamente).

        Llamado por `AppsService.create` cuando el operador da de alta
        una app. Tambien puede llamarse manualmente desde un endpoint
        admin para refrescar la config de un job viejo al nuevo formato
        sin recrear la app (migracion).

        Conceptualmente separado de `trigger_build`: este metodo solo
        toca la DEFINICION del job en Jenkins (config.xml). No dispara
        builds. `trigger_build` solo lanza builds; asume que el job ya
        existe y NO lo crea ni lo modifica.

        Parametros:
            slug: slug de la app (DNS-1123). El job se llamara `laurel_<slug>`.
            image_base: nombre base de la imagen (ej `aflobaton/laurel_notas`).
                Se sanitiza a `owner/repo` sin registry ni tag; el pipeline
                lo completa con `docker.io/${IMAGE}:${TAG}`.
            github_repo_url: URL al repo GitHub. Solo se usa para construir
                el parametro REPO del job (informativo para el pipeline).

        Returns True si el job quedo creado/refrescado, False si fallo
        (logueado). Raises AppError si Jenkins no responde a tiempo.
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
            parts = github_repo_url.rstrip("/").split("/")
            repo = "/".join(parts[-2:]) if len(parts) >= 2 else f"laurel-applications/{job}"
        else:
            repo = f"laurel-applications/{job}"

        # image_base puede llegar con tag (ej 'ghcr.io/owner/repo:0.0.1' o
        # 'owner/repo:0.0.1'); el pipeline espera SIN tag + SIN registry
        # porque lo completa con `docker.io/${IMAGE}:${TAG}`.
        if "/" in image_base:
            parts = image_base.split("/")
            image_no_registry = "/".join(parts[-2:]) if len(parts) >= 2 else image_base
        else:
            image_no_registry = image_base
        image_no_registry = image_no_registry.split(":")[0]

        # Groovy pipeline (CpsFlowDefinition). Sandbox=true: corre seguro,
        # sin acceso a Jenkins internals. Las PasswordParameters (GITHUB_PAT,
        # DOCKERHUB_USER, DOCKERHUB_PASSWORD) son auto-masked en logs por
        # Jenkins.
        groovy_script = JenkinsService._build_pipeline_groovy()
        # xml.sax.saxutils no estaba importado arriba; usamos un escape
        # inline minimo (& -> &amp; y " -> &quot;) para no traer otra dep.
        # El script Groovy no deberia contener estos caracteres problematicos
        # en practica, pero el escape es defensivo.
        from xml.sax.saxutils import escape as _xml_escape
        groovy_escaped = _xml_escape(groovy_script, {'"': "&quot;"})

        xml = (
            "<?xml version='1.0' encoding='UTF-8'?>\n"
            '<flow-definition plugin="workflow-job@2.40">\n'
            f'  <description>Job para {job}. Pipeline CI/CD declarativo (Blue Ocean): '
            'Init -> Clone -> Test (autodetect) -> Build+Push (kaniko). '
            'Creado por AppsService.create.</description>\n'
            "  <keepDependencies>false</keepDependencies>\n"
            "  <properties>\n"
            "    <hudson.model.ParametersDefinitionProperty>\n"
            "      <parameterDefinitions>\n"
            f"        <hudson.model.StringParameterDefinition><name>TAG</name><defaultValue>0.0.1</defaultValue><description>tag/version a buildear (auto-increment calculado por el webhook desde Docker Hub tags)</description></hudson.model.StringParameterDefinition>\n"
            f"        <hudson.model.StringParameterDefinition><name>REPO</name><defaultValue>{repo}</defaultValue><description>repo GitHub (owner/name)</description></hudson.model.StringParameterDefinition>\n"
            f"        <hudson.model.StringParameterDefinition><name>IMAGE</name><defaultValue>{image_no_registry}</defaultValue><description>imagen destino SIN registry ni tag (e.g. owner/repo). El pipeline agrega docker.io/ y :${{TAG}}</description></hudson.model.StringParameterDefinition>\n"
            "        <hudson.model.PasswordParameterDefinition><name>DOCKERHUB_USER</name><defaultValue>placeholder</defaultValue><description>User de Docker Hub (namespace del repo). Auto-masked en logs; el backend lo envia en cada trigger.</description></hudson.model.PasswordParameterDefinition>\n"
            "        <hudson.model.PasswordParameterDefinition><name>DOCKERHUB_PASSWORD</name><defaultValue>placeholder</defaultValue><description>Password/token de Docker Hub para push a docker.io. Auto-masked en logs; el backend lo envia en cada trigger.</description></hudson.model.PasswordParameterDefinition>\n"
            "      </parameterDefinitions>\n"
            "    </hudson.model.ParametersDefinitionProperty>\n"
            "    <hudson.model.BuildAuthorizationTokenProperty>\n"
            f"      <authToken>{token}</authToken>\n"
            "    </hudson.model.BuildAuthorizationTokenProperty>\n"
            "  </properties>\n"
            '  <definition class="org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition" '
            'plugin="workflow-cps@2.94">\n'
            f"    <script>{groovy_escaped}</script>\n"
            "    <sandbox>true</sandbox>\n"
            "  </definition>\n"
            "  <triggers/>\n"
            "</flow-definition>"
        )

        headers = {"Content-Type": "application/xml"}
        if crumb:
            headers["Jenkins-Crumb"] = crumb

        logger.info(
            "jenkins.ensure_job_config START slug=%s job=%s url=%s xml_len=%d",
            slug, job, url, len(xml),
        )
        try:
            resp = requests.post(url, data=xml.encode("utf-8"), headers=headers,
                                 timeout=JENKINS_TIMEOUT)
        except requests.RequestException as exc:
            logger.error(
                "jenkins.ensure_job_config CONN_ERROR slug=%s err=%s",
                slug, exc,
            )
            raise AppError(
                f"Jenkins no respondio al crear el job: {exc}",
                status_code=504,
            ) from exc



        headers = {"Content-Type": "application/xml"}
        if crumb:
            headers["Jenkins-Crumb"] = crumb

        logger.info(
            "jenkins.ensure_job_config START slug=%s job=%s url=%s xml_len=%d",
            slug, job, url, len(xml),
        )
        try:
            resp = requests.post(url, data=xml.encode("utf-8"), headers=headers,
                                 timeout=JENKINS_TIMEOUT)
        except requests.RequestException as exc:
            logger.error(
                "jenkins.ensure_job_config CONN_ERROR slug=%s err=%s",
                slug, exc,
            )
            raise AppError(
                f"Jenkins no respondio al crear el job: {exc}",
                status_code=504,
            ) from exc

        if resp.status_code in (200, 201):
            logger.info("jenkins.ensure_job_config OK slug=%s job=%s", slug, job)
            return True
        # 409 = ya existe: REFRESCAMOS el config del job existente
        # (POST /job/<name>/config.xml). Esto es importante porque un
        # job creado con una version vieja del codigo no se actualiza
        # solo: al re-llamar ensure_job_config (p.ej. al recrear la app) el
        # job queda con el pipeline nuevo y el build token actualizado.
        if resp.status_code == 409 or "already exists" in resp.text.lower():
            return JenkinsService._refresh_job_config(slug, job, xml, crumb)
        # 403 con crumb invalido: lo logueamos y reintentamos sin crumb una vez.
        if resp.status_code == 403 and crumb:
            logger.warning("jenkins.ensure_job_config CRUMB_RETRY slug=%s (403 con crumb)", slug)
            try:
                resp = requests.post(url, data=xml.encode("utf-8"),
                                     headers={"Content-Type": "application/xml"},
                                     timeout=JENKINS_TIMEOUT)
            except requests.RequestException as exc:
                raise AppError(f"Jenkins no respondio: {exc}", status_code=504) from exc
            if resp.status_code in (200, 201):
                logger.info("jenkins.ensure_job_config OK_AFTER_RETRY slug=%s", slug)
                return True
            if resp.status_code == 409 or "already exists" in resp.text.lower():
                return JenkinsService._refresh_job_config(slug, job, xml, crumb=None)
        logger.error(
            "jenkins.ensure_job_config REJECTED slug=%s status=%s body=%s",
            slug, resp.status_code, resp.text[:200],
        )
        raise AppError(
            f"Jenkins rechazo createItem: {resp.status_code} {resp.text[:200]}",
            status_code=502,
        )

    @staticmethod
    def _refresh_job_config(
        slug: str, job: str, xml: str, crumb: str | None
    ) -> bool:
        """Actualiza el config de un job existente con el XML nuevo.

        Llamado desde ensure_job_config cuando Jenkins responde 409 (job ya
        existe). Hace POST /job/<job>/config.xml con el mismo XML que
        usariamos para crearlo. Asi un job viejo se refresca al pipeline
        nuevo + build token actualizado en una sola llamada, sin que el
        operador tenga que borrarlo a mano.

        Returns True si se actualizo, False si fallo (logueado).
        """
        _validate_slug(slug)
        base = JenkinsService._base_url()
        url = f"{base}/job/{job}/config.xml"
        headers = {"Content-Type": "application/xml"}
        if crumb:
            headers["Jenkins-Crumb"] = crumb
        logger.info(
            "jenkins.refresh_job_config START slug=%s job=%s url=%s xml_len=%d",
            slug, job, url, len(xml),
        )
        try:
            resp = requests.post(
                url, data=xml.encode("utf-8"), headers=headers,
                timeout=JENKINS_TIMEOUT,
            )
        except requests.RequestException as exc:
            logger.error(
                "jenkins.refresh_job_config CONN_ERROR slug=%s err=%s",
                slug, exc,
            )
            return False
        if resp.status_code in (200, 201, 204):
            logger.info(
                "jenkins.refresh_job_config OK slug=%s job=%s status=%s",
                slug, job, resp.status_code,
            )
            return True
        logger.warning(
            "jenkins.refresh_job_config FAIL slug=%s status=%s body=%s",
            slug, resp.status_code, resp.text[:200],
        )
        return False

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
    def _build_pipeline_groovy() -> str:
        """Devuelve el script Groovy (CpsFlowDefinition) para el job.

        Stages:
          - Init: log de params.
          - Clone: git clone (con GITHUB_PAT si llega).
          - Test: autodeteccion de framework.
          - Build+Push: kaniko contra docker.io.

        Las PasswordParameters (GITHUB_PAT, DOCKERHUB_USER, DOCKERHUB_PASSWORD)
        son auto-masked en logs por Jenkins. El IMAGE llega como param
        `owner/repo` sin registry ni tag; el stage Build+Push agrega
        `docker.io/` y `:${TAG}`.
        """
        # Triple-quoted Groovy. Los ${...} son interpolaciones de Jenkins
        # en runtime (no de Python). Las acciones de autodeteccion son
        # best-effort: si no encuentran nada, exit 0 (skip no falla el build).
        # Usamos r""" (no r''') porque el Groovy contiene `sh '''...'''`
        # y eso cerraria el string Python prematuramente.
        #
        # NOTA: GITHUB_PAT no esta en el job. Todos los repos de
        # laurel-applications son publicos, asi que el clone no
        # requiere autenticacion. Si en el futuro se necesitan repos
        # privados, agregar GITHUB_PAT como PasswordParameter y volver
        # al condicional en el Clone stage.
        return r"""pipeline {
    agent any
    environment {
        IMAGE_FULL = "docker.io/${IMAGE}"
    }
    stages {
        stage('Init') {
            steps {
                echo "=========================================="
                echo "STAGE Init"
                echo "TAG=${params.TAG}  REPO=${params.REPO}  IMAGE=${params.IMAGE}"
                echo "=========================================="
            }
        }
        stage('Clone') {
            steps {
                sh '''
                    set -e
                    echo "=========================================="
                    echo "STAGE Clone: ${REPO} (tag ${TAG})"
                    echo "=========================================="
                    cd ${WORKSPACE}
                    rm -rf ${WORKSPACE}/* ${WORKSPACE}/.git ${WORKSPACE}/.[!.]* 2>/dev/null || true
                    # Repos publicos: clone sin auth. Si en el futuro se
                    # necesitan repos privados, agregar GITHUB_PAT como
                    # PasswordParameter en el job y volver al condicional.
                    git clone "https://github.com/${REPO}.git" .
                    if ! git rev-parse --verify HEAD >/dev/null 2>&1; then
                        echo "ERROR: el repo ${REPO} no tiene commits en la default branch. Haz un push inicial antes de disparar el build." >&2
                        exit 2
                    fi
                    if [ -n "${TAG}" ] && git rev-parse --verify "refs/tags/${TAG}" >/dev/null 2>&1; then
                        echo "Checkout tag ${TAG}"
                        git checkout -q "${TAG}"
                    else
                        echo "WARN: tag ${TAG} no existe en el repo; usando HEAD"
                        git checkout -q HEAD
                    fi
                    ls -la
                '''
            }
        }
        stage('Test') {
            steps {
                sh '''
                    set +e  # stage Test no debe romper el pipeline si autodetect falla
                    echo "=========================================="
                    echo "STAGE Test (autodetect)"
                    echo "=========================================="
                    cd ${WORKSPACE}

                    if [ -f composer.json ]; then
                        echo "[test] composer.json detectado"
                        if grep -q '"test"' composer.json 2>/dev/null; then
                            if command -v composer >/dev/null 2>&1; then
                                composer install --no-interaction --prefer-dist 2>&1 | tail -20
                                composer test
                                TEST_RESULT=$?
                            else
                                echo "[test] SKIP: composer no instalado"
                                TEST_RESULT=0
                            fi
                        elif [ -f phpunit.xml ] || [ -f phpunit.xml.dist ]; then
                            if [ ! -d vendor ] && command -v composer >/dev/null 2>&1; then
                                composer install --no-interaction --prefer-dist 2>&1 | tail -10
                            fi
                            if [ -x vendor/bin/phpunit ]; then
                                vendor/bin/phpunit
                                TEST_RESULT=$?
                            else
                                echo "[test] SKIP: phpunit no instalado"
                                TEST_RESULT=0
                            fi
                        else
                            echo "[test] SKIP: composer.json sin script 'test' ni phpunit.xml"
                            TEST_RESULT=0
                        fi
                    elif [ -f package.json ]; then
                        echo "[test] package.json detectado"
                        if grep -q '"test"' package.json 2>/dev/null; then
                            if command -v npm >/dev/null 2>&1; then
                                npm test
                                TEST_RESULT=$?
                            else
                                echo "[test] SKIP: npm no instalado"
                                TEST_RESULT=0
                            fi
                        else
                            echo "[test] SKIP: package.json sin script 'test'"
                            TEST_RESULT=0
                        fi
                    elif [ -f pytest.ini ] || [ -f pyproject.toml ]; then
                        echo "[test] proyecto Python detectado"
                        if command -v pytest >/dev/null 2>&1; then
                            pytest
                            TEST_RESULT=$?
                        else
                            echo "[test] SKIP: pytest no instalado"
                            TEST_RESULT=0
                        fi
                    else
                        echo "[test] NO TESTS FOUND - saltando stage"
                        TEST_RESULT=0
                    fi

                    echo "=========================================="
                    if [ "$TEST_RESULT" -eq 0 ]; then
                        echo "TESTS: PASSED o SKIPPED (rc=0)"
                    else
                        echo "TESTS: FAILED (rc=$TEST_RESULT)"
                    fi
                    echo "=========================================="
                    exit $TEST_RESULT
                '''
            }
        }
        stage('Build+Push') {
            steps {
                sh '''
                    set -e
                    echo "=========================================="
                    echo "STAGE Build+Push (kaniko)"
                    echo "Destination: ${IMAGE_FULL}:${TAG} + :latest"
                    echo "=========================================="
                    export DOCKER_CONFIG=${WORKSPACE}/.docker
                    mkdir -p "$DOCKER_CONFIG"
                    if [ -n "${DOCKERHUB_USER}" ] && [ "${DOCKERHUB_USER}" != "placeholder" ] \
                       && [ -n "${DOCKERHUB_PASSWORD}" ] && [ "${DOCKERHUB_PASSWORD}" != "placeholder" ]; then
                        AUTH=$(printf '%s:%s' "${DOCKERHUB_USER}" "${DOCKERHUB_PASSWORD}" | base64 -w 0)
                        printf '{"auths":{"docker.io":{"auth":"%s"},"https://index.docker.io/v1/":{"auth":"%s"}}}\n' \
                            "$AUTH" "$AUTH" > "$DOCKER_CONFIG/config.json"
                    fi
                    if [ -x /usr/local/kaniko/kaniko ]; then
                        KANIKO_BIN=/usr/local/kaniko/kaniko
                    elif [ -x /usr/local/kaniko/executor ]; then
                        KANIKO_BIN=/usr/local/kaniko/executor
                    else
                        echo "ERROR: kaniko no instalado en /usr/local/kaniko/" >&2
                        exit 3
                    fi
                    mkdir -p ${WORKSPACE}/.kaniko
                    "${KANIKO_BIN}" \
                      --context=${WORKSPACE} \
                      --dockerfile=Dockerfile \
                      --destination="${IMAGE_FULL}:${TAG}" \
                      --destination="${IMAGE_FULL}:latest" \
                      --cache=true \
                      --cache-repo="docker.io/${DOCKERHUB_USER}/kaniko-cache" \
                      --snapshot-mode=time
                    echo "BUILD+PUSH OK"
                '''
            }
        }
    }
    post {
        success { echo "PIPELINE COMPLETE" }
        failure { echo "PIPELINE FAILED" }
    }
}
"""

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
    def get_build_status(
        slug: str,
        build_number: int | None = None,
        *,
        build_url: str | None = None,
    ) -> dict:
        """Consulta el status actual de un build de Jenkins.

        La forma recomendada es pasar `build_url` (la URL canonica que
        Jenkins devolvio en el header `Location` del trigger). Esto
        evita depender de un `build_number` que podria estar desincronizado
        o ser None (build encolada). Si solo se pasa `build_number`, se
        reconstruye la URL como `{base}/job/{PREFIX}{slug}/{n}` (modo
        legacy, mantenido para compatibilidad).

        Returns: `{"status", "building", "result", "timestamp"}` mapeado
        a los valores del modelo AppBuild:
        - building=True  -> 'running'
        - building=False -> result mapeado: SUCCESS=success, FAILURE=failed,
                            UNSTABLE=failed, ABORTED=aborted, NOT_BUILT=failed
        - 404/otro: lanza AppError
        """
        _validate_slug(slug)
        if build_url:
            # Normalizo: el caller puede haber guardado con o sin /api/json.
            url = build_url.rstrip("/")
            if not url.endswith("/api/json"):
                url = f"{url}/api/json"
        elif build_number is not None:
            base = JenkinsService._base_url()
            url = f"{base}/job/{PREFIX}{slug}/{build_number}/api/json"
        else:
            raise AppError(
                "get_build_status requiere build_url o build_number",
                status_code=500,
            )
        logger.info(
            "jenkins.get_build_status START slug=%s url=%s",
            slug, url,
        )
        try:
            resp = requests.get(url, timeout=JENKINS_TIMEOUT)
        except requests.RequestException as exc:
            logger.error(
                "jenkins.get_build_status CONN_ERROR slug=%s err=%s",
                slug, exc,
            )
            raise AppError("Jenkins timeout", status_code=504) from exc
        if resp.status_code == 404:
            logger.warning(
                "jenkins.get_build_status NOT_FOUND slug=%s url=%s",
                slug, url,
            )
            raise AppError(
                f"Jenkins build not found: {url}",
                status_code=404,
            )
        if resp.status_code != 200:
            logger.error(
                "jenkins.get_build_status ERROR slug=%s status=%s body=%s",
                slug, resp.status_code, resp.text[:200],
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
        number = data.get("number")
        logger.info(
            "jenkins.get_build_status OK slug=%s number=%s status=%s building=%s result=%s",
            slug, number, status, building, result,
        )
        return {
            "status": status,
            "building": building,
            "result": result,
            "timestamp": data.get("timestamp"),
            "number": number,
        }


def _parse_build_number(location_header: str | None) -> int | None:
    """Extrae el build number del header `Location` de un POST a buildWithParameters.

    Formatos soportados:
      - '/job/<job>/<n>/'        -> devuelve n
      - 'http://.../job/<job>/<n>/' -> idem
      - '/queue/item/<id>/'      -> devuelve None (build encolada, sin nro)
    Devuelve None si el header no se puede parsear.
    """
    if not location_header:
        return None
    if "/queue/item/" in location_header:
        return None
    parts = [p for p in location_header.rstrip("/").split("/") if p]
    for p in reversed(parts):
        if p.isdigit():
            return int(p)
    return None


def _resolve_build_location(
    base: str, location_header: str, job: str
) -> tuple[str, int | None]:
    """Resuelve la URL canonica y el numero de build desde el header
    `Location` de un POST a buildWithParameters.

    Si el header apunta a `/job/<job>/<n>/`, devuelve (url, n) directo.
    Si apunta a `/queue/item/<id>/` (build encolada), consulta a Jenkins
    para resolver el numero via la API de queue (`executable.url`).
    Si la queue API falla o devuelve algo inesperado, devuelve
    (url_de_queue, None) y el polling posterior se hara con esa URL
    (Jenkins redirige /queue/item/<id>/ al /job/<job>/<n>/ real una
    vez que arranca el build).

    Esto es la fuente de verdad para "donde esta mi build" segun
    Jenkins: el caller debe usar SIEMPRE la URL canonica devuelta
    aca, nunca rearmar la URL a partir de un numero que podria estar
    desincronizado.
    """
    if not location_header:
        # Sin Location: caemos al lastBuild de la job. Es mejor que
        # devolver None: la proxima build que se cree podria no ser
        # la nuestra si hay builds encoladas.
        url = f"{base}/job/{job}/lastBuild/api/json"
        try:
            resp = requests.get(url, timeout=JENKINS_TIMEOUT, auth=_auth())
        except requests.RequestException:
            return f"{base}/job/{job}", None
        if resp.status_code != 200:
            return f"{base}/job/{job}", None
        data = resp.json()
        n = data.get("number")
        return (f"{base}/job/{job}/{n}" if n else f"{base}/job/{job}"), n

    # Normalizar a URL absoluta.
    if location_header.startswith("/"):
        loc = f"{base}{location_header}"
    elif location_header.startswith("http"):
        loc = location_header
    else:
        loc = f"{base}/{location_header}"
    loc = loc.rstrip("/")

    # Caso 1: location ya tiene numero de build.
    n = _parse_build_number(loc)
    if n is not None:
        return loc, n

    # Caso 2: location apunta a /queue/item/<id>/. Resolvemos via queue API.
    if "/queue/item/" in loc:
        try:
            resp = requests.get(
                f"{loc}/api/json",
                timeout=JENKINS_TIMEOUT,
                auth=_auth(),
            )
        except requests.RequestException as exc:
            logger.warning(
                "jenkins.resolve_build_location QUEUE_NET_ERROR location=%s err=%s",
                loc, exc,
            )
            return loc, None
        if resp.status_code != 200:
            logger.warning(
                "jenkins.resolve_build_location QUEUE_HTTP_ERROR location=%s status=%s",
                loc, resp.status_code,
            )
            return loc, None
        try:
            data = resp.json()
        except ValueError:
            return loc, None
        executable = data.get("executable") or {}
        executable_url = executable.get("url")
        executable_number = executable.get("number")
        if executable_url:
            # executable.url es absoluta y ya tiene /job/<job>/<n>/ al final.
            return executable_url.rstrip("/"), executable_number
        # Build todavia encolada (jenkins no la promovio a ejecutable
        # todavia). Devolvemos la URL de queue; el polling posterior
        # funcionara: GET /queue/item/<id>/api/json -> 200 con executable
        # una vez que arranque.
        return loc, None

    # Caso 3: location con un formato raro que no pudimos parsear.
    return loc, None


def _auth() -> tuple | None:
    """Auth opcional para endpoints de Jenkins cuando CSRF esta off.

    Reutiliza el mismo patron que el resto del modulo: si JENKINS_USER/
    JENKINS_TOKEN estan configurados, los usa. Si no, sin auth.
    """
    user = os.environ.get("JENKINS_USER") or ""
    token = os.environ.get("JENKINS_TOKEN") or ""
    if user and token:
        return (user, token)
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
