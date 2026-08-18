"""Service del modulo Builds: persistencia + polling a Jenkins.

El webhook crea un AppBuild en estado 'pending' justo despues de
disparar Jenkins. El status se actualiza de dos formas:
- Sincronico, en cada GET de un build: si esta pending/running, hace
  una llamada a Jenkins para actualizar.
- (futuro) background poll, si se quiere refrescar sin request del cliente.
"""
import logging
from datetime import datetime

from app.core.db import db
from app.core.errors import AppError
from app.core.utils import utcnow
from app.modules.apps.model import Application
from app.modules.builds.model import AppBuild
from app.modules.integrations.jenkins.service import JenkinsService

logger = logging.getLogger(__name__)

# Estados terminales: no vale la pena volver a pegarle a Jenkins.
_TERMINAL_STATUSES = {"success", "failed", "aborted"}


class BuildsService:

    @staticmethod
    def list_for_app(app_id: int, limit: int = 20) -> list[AppBuild]:
        """Lista los ultimos N builds de la app (mas recientes primero)."""
        app = db.session.get(Application, app_id)
        if app is None or app.deleted_at is not None:
            raise AppError(f"Application {app_id} no encontrada", status_code=404)
        return (
            AppBuild.query
            .filter_by(application_id=app_id)
            .order_by(AppBuild.id.desc())
            .limit(limit)
            .all()
        )

    @staticmethod
    def get(app_id: int, build_id: int, *, poll: bool = True) -> AppBuild:
        """Obtiene un build. Si `poll=True` y esta pending/running,
        consulta Jenkins y actualiza el registro en BD antes de devolver.
        """
        app = db.session.get(Application, app_id)
        if app is None or app.deleted_at is not None:
            raise AppError(f"Application {app_id} no encontrada", status_code=404)
        build = db.session.get(AppBuild, build_id)
        if build is None or build.application_id != app_id:
            raise AppError(
                f"Build {build_id} no encontrado para la app {app_id}",
                status_code=404,
            )
        if poll and build.status not in _TERMINAL_STATUSES and build.jenkins_number:
            BuildsService._poll_one(build)
        return build

    @staticmethod
    def _poll_one(build: AppBuild) -> None:
        """Hace una llamada a Jenkins para actualizar el status del build.
        Errores de red o Jenkins se loguean y se descartan: el caller
        no debe romperse porque Jenkins tenga un blip.
        """
        from app.modules.apps.model import Application  # noqa: F401
        logger.info(
            "builds.poll START build_id=%s job=%s number=%s current_status=%s",
            build.id, build.jenkins_job, build.jenkins_number, build.status,
        )
        try:
            status = JenkinsService.get_build_status(
                # jenkins_job es `laurel_<slug>`; el service agrega el prefix,
                # asi que le pasamos solo el slug.
                slug=_slug_from_job(build.jenkins_job),
                build_number=build.jenkins_number,
            )
        except AppError as exc:
            logger.warning(
                "builds.poll FAIL build_id=%s job=%s n=%s err=%s",
                build.id, build.jenkins_job, build.jenkins_number, exc.message,
            )
            return

        old_status = build.status
        new_status = status["status"]
        if new_status == build.status and build.started_at and status.get("timestamp"):
            # Sin cambio de estado, no reseteamos started_at.
            logger.info(
                "builds.poll NOOP build_id=%s status_unchanged=%s",
                build.id, new_status,
            )
            return
        if new_status != "pending" and build.started_at is None and status.get("timestamp"):
            build.started_at = datetime.fromtimestamp(status["timestamp"] / 1000)
        if new_status in _TERMINAL_STATUSES and build.finished_at is None:
            build.finished_at = utcnow()
            if new_status == "failed":
                # Si result=FAILURE, Jenkins ya nos dio el detalle en result;
                # un mensaje explicito ayuda al operador a debuggear.
                build.error_message = (
                    f"Jenkins result: {status.get('result') or 'unknown'}"
                )
        build.status = new_status
        db.session.commit()
        logger.info(
            "builds.poll TRANSITION build_id=%s %s -> %s",
            build.id, old_status, new_status,
        )

    @staticmethod
    def create_pending(
        app_id: int,
        version: str,
        commit_sha: str | None,
        jenkins_job: str,
        jenkins_number: int | None,
        jenkins_url: str | None,
    ) -> AppBuild:
        """Crea un AppBuild en estado 'pending' y devuelve el registro.
        Usado por el webhook despues de disparar Jenkins.
        """
        build = AppBuild(
            application_id=app_id,
            version=version,
            commit_sha=commit_sha,
            status="pending",
            jenkins_job=jenkins_job,
            jenkins_number=jenkins_number,
            jenkins_url=jenkins_url,
        )
        db.session.add(build)
        db.session.commit()
        logger.info(
            "builds.create_pending id=%s app_id=%s version=%s commit=%s "
            "jenkins_job=%s jenkins_number=%s jenkins_url=%s",
            build.id, app_id, version,
            (commit_sha or "")[:12], jenkins_job, jenkins_number, jenkins_url,
        )
        return build

    @staticmethod
    def set_current_version(app_id: int, version: str) -> Application:
        """Setea `app.current_version`. La UI usa esto para fijar la version
        que se usara en el proximo build (push a master).
        """
        from app.modules.audits.service import AuditService
        app = db.session.get(Application, app_id)
        if app is None or app.deleted_at is not None:
            raise AppError(f"Application {app_id} no encontrada", status_code=404)
        version = (version or "").strip()
        if not version:
            raise AppError("version no puede estar vacia", status_code=400)
        if len(version) > 50:
            raise AppError("version demasiado larga (max 50 chars)", status_code=400)
        old = app.current_version
        app.current_version = version
        db.session.commit()
        AuditService.log(
            "app_version_set", "application", app.id,
            {"version": version}, {"version": old},
        )
        return app


def _slug_from_job(job: str) -> str:
    """Convierte `laurel_<slug>` -> `<slug>`. Si no tiene prefijo, lo devuelve igual."""
    if job.startswith("laurel_"):
        return job[len("laurel_"):]
    return job
