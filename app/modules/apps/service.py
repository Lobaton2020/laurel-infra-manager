"""CRUD + lifecycle de Application."""
import logging

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app.core.db import db
from app.core.errors import AppError, ConflictError, NotFoundError
from app.core.utils import utcnow
from app.modules.apps.model import AppEvent, Application
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

        Provision (obligatoria): al crear una app se crean 2 repos en
        paralelo/huesped — repo GitHub (`laurel_<slug>` en la org) y repo
        Docker Hub (`laurel_<slug>` en el namespace). Cada paso se registra
        como un AppEvent (timeline) y si alguno falla la app queda
        `status=error` (esos checks son obligatorios).

        - Si `create_github_repo=true` y no se pasa `github_repo_url`,
          crea el repo en GitHub. Si el usuario paso una URL custom, ese
          check se considera `ok` (repo ya provisto).
        - Si no se pasa `docker_image_base`, crea el repo en Docker Hub.
        - Cualquier error != 503/409 deja `status=error`. Los 503 (PAT no
          configurado) y 409 (repo ya existe) dejan `status=error` tambien
          porque ahora el check es obligatorio: si falla, la app se crea
          igual pero marcada como erronea para que el usuario lo vea.
        """
        from app.modules.integrations.docker.service import ContainerRegistryService
        from app.modules.integrations.github.service import GitHubService

        name = data["name"]
        slug = AppsService._to_slug(name)
        github_url = data.get("github_repo_url")
        docker_base = data.get("docker_image_base")
        create_repo_flag = data.get("create_github_repo", False)
        workspace_id = data.get("workspace_id")
        if workspace_id is not None and not db.session.get(Workspace, workspace_id):
            raise NotFoundError(f"Workspace {workspace_id} no encontrado")

        events: list[tuple[str, str, str]] = []

        # Check 1: repo GitHub (obligatorio si se pidio crearlo).
        if github_url:
            events.append(("github_repo", "ok", f"Repo GitHub ya provisto: {github_url}"))
        elif create_repo_flag:
            try:
                result = GitHubService.create_empty_repo(slug)
                github_url = result["html_url"]
                AuditService.log(
                    "github_repo_created", "application", None,
                    {"slug": slug, "url": github_url},
                )
                events.append((
                    "github_repo", "ok",
                    f"Repo GitHub creado: {result['full_name']}",
                ))
            except AppError as exc:
                events.append(("github_repo", "error", exc.message))
                logger.warning("github_repo_failed para %s: %s", slug, exc.message)
        else:
            events.append((
                "github_repo", "error",
                "Falta GitHub: no se proporciono github_repo_url ni create_github_repo",
            ))

        # Check 2: imagen de contenedor en GHCR.
        # GHCR NO requiere pre-crear el repo: el paquete se materializa en el
        # primer `docker push ghcr.io/<owner>/<repo>` desde Jenkins. Solo
        # calculamos el image_base por defecto y registramos el evento como
        # `ok` (la creacion real ocurre en el push).
        if docker_base is None:
            docker_base = ContainerRegistryService.suggested_base(slug)
            events.append((
                "ghcr_repo", "ok",
                f"Imagen GHCR: {docker_base} (se crea en el primer push)",
            ))
        else:
            events.append(("ghcr_repo", "ok", f"Imagen base ya provista: {docker_base}"))

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
            db.session.flush()
        except IntegrityError as exc:
            db.session.rollback()
            raise ConflictError(
                f"Ya existe una Application con name='{name}' o slug='{slug}'"
            ) from exc

        # Persistir timeline de eventos y derivar el estado final.
        for event, status, detail in events:
            db.session.add(AppEvent(
                application_id=app.id,
                event=event,
                status=status,
                detail=detail,
            ))
            if status == "error":
                app.status = "error"
        if app.status != "error":
            app.status = "ok" if events else "ok"
        db.session.commit()

        AuditService.log(
            "app_create", "application", app.id,
            {"slug": slug, "name": name, "github_repo_url": github_url,
             "status": app.status},
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