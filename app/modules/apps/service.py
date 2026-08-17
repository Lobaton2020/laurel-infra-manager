"""CRUD + lifecycle de Application."""
import logging

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app.core.db import db
from app.core.errors import AppError, ConflictError, NotFoundError
from app.core.utils import utcnow
from app.modules.apps.model import Application
from app.modules.audits.service import AuditService
from app.modules.scoops.schema import slugify
from app.modules.workspaces.model import Workspace

logger = logging.getLogger(__name__)


class AppsService:

    @staticmethod
    def _to_slug(name: str) -> str:
        s = slugify(name)
        if not s:
            raise AppError(
                f"No se pudo derivar un slug DNS-1123 valido de '{name}'. "
                "Use un nombre con letras/numeros.",
                status_code=400,
            )
        return s

    @staticmethod
    def list(page: int = 1, limit: int = 20, workspace_id: int | None = None) -> dict:
        """Lista apps no soft-deleted con paginacion.

        Si `workspace_id` viene, solo apps de ese workspace (las apps sin
        workspace solo aparecen cuando no se filtra)."""
        query = Application.query.filter(Application.deleted_at.is_(None))
        if workspace_id is not None:
            query = query.filter(Application.workspace_id == workspace_id)
        total = query.count()
        items = (
            query.order_by(Application.created_at.desc())
            .limit(limit).offset((page - 1) * limit).all()
        )
        pages = max(1, (total + limit - 1) // limit) if total else 0
        return {"items": items, "total": total, "page": page, "limit": limit, "pages": pages}

    @staticmethod
    def get(app_id: int) -> Application:
        app = Application.query.filter(
            Application.id == app_id,
            Application.deleted_at.is_(None),
        ).first()
        if app is None:
            raise NotFoundError(f"Application {app_id} no encontrada")
        return app

    @staticmethod
    def create(data: dict) -> Application:
        """Crea una Application nueva.

        Hooks externos (opcionales):
        - Si `create_github_repo=true` y no se pasa `github_repo_url`,
          intenta crear el repo en GitHub via `GitHubService`.
        """
        from app.modules.integrations.docker.service import DockerHubService
        from app.modules.integrations.github.service import GitHubService

        name = data["name"]
        slug = AppsService._to_slug(name)
        github_url = data.get("github_repo_url")
        docker_base = data.get("docker_image_base")
        create_repo_flag = data.get("create_github_repo", False)
        workspace_id = data.get("workspace_id")
        if workspace_id is not None and not db.session.get(Workspace, workspace_id):
            raise NotFoundError(f"Workspace {workspace_id} no encontrado")

        if create_repo_flag and not github_url:
            # Solo intentamos crear si el caller no paso una URL custom.
            try:
                result = GitHubService.create_empty_repo(slug)
                github_url = result["html_url"]
                AuditService.log(
                    "github_repo_created", "application", None,
                    {"slug": slug, "url": github_url},
                )
            except AppError as exc:
                if exc.status_code == 503:
                    # PAT no configurado: skip silencioso, auditamos.
                    logger.info("github_repo_skipped para %s: PAT no configurado", slug)
                    AuditService.log(
                        "app_create", "application", None,
                        {"slug": slug, "github_repo_skipped": "pat_missing"},
                    )
                else:
                    raise

        # Repo vacio en Docker Hub (el push de Jenkins lo necesita para existir).
        # Se crea siempre salvo que ya se indico docker_image_base manualmente.
        if docker_base is None:
            try:
                DockerHubService.create_empty_repo(slug)
                AuditService.log(
                    "dockerhub_repo_created", "application", None,
                    {"slug": slug},
                )
            except AppError as exc:
                if exc.status_code in (503, 409):
                    # 503: PAT no configurado (skip silencioso); 409: ya existe.
                    logger.info("dockerhub_repo_skipped para %s: %s", slug, exc.message)
                    AuditService.log(
                        "app_create", "application", None,
                        {"slug": slug, "dockerhub_repo_skipped": exc.message},
                    )
                else:
                    raise

        app = Application(
            name=name,
            slug=slug,
            description=data.get("description"),
            github_repo_url=github_url,
            docker_image_base=docker_base,
            workspace_id=workspace_id,
        )
        try:
            db.session.add(app)
            db.session.commit()
        except IntegrityError as exc:
            db.session.rollback()
            raise ConflictError(
                f"Ya existe una Application con name='{name}' o slug='{slug}'"
            ) from exc

        AuditService.log(
            "app_create", "application", app.id,
            {"slug": slug, "name": name, "github_repo_url": github_url},
        )
        return app

    @staticmethod
    def update(app_id: int, data: dict) -> Application:
        app = AppsService.get(app_id)
        if "workspace_id" in data:
            wid = data["workspace_id"]
            if wid is not None and not db.session.get(Workspace, wid):
                raise NotFoundError(f"Workspace {wid} no encontrado")
            app.workspace_id = wid
        for field in ("description", "github_repo_url", "docker_image_base"):
            if field in data:
                setattr(app, field, data[field])
        db.session.commit()
        AuditService.log(
            "app_update", "application", app.id,
            {k: v for k, v in data.items() if k in (
                "description", "github_repo_url", "docker_image_base", "workspace_id"
            )},
        )
        return app

    @staticmethod
    def soft_delete(app_id: int) -> Application:
        """Soft-delete: marca `deleted_at`. NO toca el cluster.

        Para borrar el namespace del cluster, usar `force_delete` (futura
        implementacion en otra fase; por ahora solo se hace soft-delete).
        """
        app = AppsService.get(app_id)
        app.deleted_at = utcnow()
        db.session.commit()
        AuditService.log(
            "app_soft_delete", "application", app.id,
            {"slug": app.slug},
        )
        return app

    @staticmethod
    def archive_for_app(app_id: int) -> int:
        """Marca todos los scoops de una app como `archived`. Usado por
        force-delete en una fase futura. Hoy no se llama desde ningun
        endpoint; se incluye para que DomainService y AppsService puedan
        compartir la utilidad."""
        from app.modules.scoops.model import STATUS_ARCHIVED
        from app.modules.scoops.service import ScoopService

        app = AppsService.get(app_id)
        count = ScoopService.archive_for_application(app.slug)
        AuditService.log(
            "app_archive_scoops", "application", app.id,
            {"slug": app.slug, "count": count},
        )
        return count