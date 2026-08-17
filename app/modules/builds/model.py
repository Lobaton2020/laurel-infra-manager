"""Modelo de Builds de aplicacion (registro de cada build de Jenkins disparado
por un push a master).

Cada vez que llega un webhook push a master del repo de la app:
1. El backend lee `app.current_version` (set por la UI).
2. Dispara Jenkins con esa version como TAG.
3. Crea un registro `AppBuild` con status='pending'.
4. Hace polling a Jenkins para mantener el status actualizado
   (pending -> running -> success | failed | aborted).

El registro vive aunque la app se borre (FK con CASCADE pero el historical
queda via AppDeletionLog).
"""
from app.core.db import db
from app.core.utils import utcnow


class AppBuild(db.Model):
    """Un build de Jenkins disparado por un push a master del repo de la app.

    Atributos:
        version:        semver que se le paso a Jenkins como TAG.
        commit_sha:     SHA del head_commit del push que lo disparo.
        status:         'pending' | 'running' | 'success' | 'failed' | 'aborted'.
        jenkins_job:    nombre del job Jenkins (e.g. 'laurel_notas').
        jenkins_number: numero de build dentro del job (para poll).
        jenkins_url:    URL directa al build en Jenkins (para el link de la UI).
        error_message:  si status='failed', el mensaje de error de Jenkins.
    """

    __tablename__ = "app_builds"

    id = db.Column(db.Integer, primary_key=True)
    application_id = db.Column(
        db.Integer, db.ForeignKey("applications.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # El backref `application` en AppBuild lo crea SQLAlchemy automaticamente
    # via el FK; no hace falta declarar `db.relationship` aca tampoco.
    version = db.Column(db.String(50), nullable=False)
    commit_sha = db.Column(db.String(64))
    status = db.Column(
        db.String(20), default="pending", nullable=False, index=True,
    )
    jenkins_job = db.Column(db.String(100), nullable=False)
    jenkins_number = db.Column(db.Integer)
    jenkins_url = db.Column(db.String(500))
    error_message = db.Column(db.String(500))

    queued_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    started_at = db.Column(db.DateTime)
    finished_at = db.Column(db.DateTime)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "application_id": self.application_id,
            "version": self.version,
            "commit_sha": self.commit_sha,
            "status": self.status,
            "jenkins_job": self.jenkins_job,
            "jenkins_number": self.jenkins_number,
            "jenkins_url": self.jenkins_url,
            "error_message": self.error_message,
            "queued_at": self.queued_at.isoformat() if self.queued_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }
