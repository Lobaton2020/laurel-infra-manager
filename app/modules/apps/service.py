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

        # Check 3: namespace K8s `user-apps-<slug>` (idempotente).
        # Lo creamos al crear la app para que ya este listo para secrets/configs/scoops.
        k8s_namespace = f"user-apps-{slug}"
        try:
            from app.modules.cluster.service import K8sService
            if not K8sService.namespace_exists(k8s_namespace):
                K8sService.create_namespace(k8s_namespace)
            events.append(("k8s_namespace", "ok", f"Namespace K8s listo: {k8s_namespace}"))
        except Exception as exc:
            events.append(("k8s_namespace", "error", f"No se pudo crear namespace: {exc}"))
            logger.warning("k8s_namespace_failed para %s: %s", slug, exc)

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
        """Elimina ABSOLUTAMENTE TODO lo asociado a la app.

        Antes del borrado, guarda un snapshot completo de la config en
        `AppDeletionLog` para trazabilidad. Despues elimina:
        - K8s: namespace `user-apps-<slug>` (cascade: secrets, configmaps,
          deployments, services, ingresses, hpa, jobs, etc.).
        - DB: marca la app como soft-deleted, marca scoops y dominios como
          archived/deleted, y borra los eventos del timeline.
        - GitHub: borra el repo `laurel_<slug>` en la org.
        - GHCR: borra el paquete `laurel_<slug>` en GHCR.

        Si algun paso externo falla (k8s/github/ghcr), logueamos warning
        y seguimos con el resto: la app debe quedar eliminada en la BD.
        """
        from app.modules.apps.model import AppDeletionLog
        from app.modules.cluster.service import K8sService
        from app.modules.domains.model import Domain
        from app.modules.integrations.docker.service import ContainerRegistryService
        from app.modules.integrations.github.service import GitHubService
        from app.modules.scoops.model import Scoop, STATUS_ARCHIVED

        app = AppsService.get(app_id)
        slug = app.slug
        ns = f"user-apps-{slug}"

        # 0) Snapshot completo de la config para trazabilidad.
        snapshot = AppsService._snapshot_for_deletion(app_id, ns)
        try:
            from flask import g
            user = getattr(g, "user", None)
            deleted_by = user.get("email") or user.get("sub") if user else None
        except RuntimeError:
            deleted_by = None
        log_row = AppDeletionLog(
            application_id=app.id,
            application_slug=slug,
            application_name=app.name,
            workspace_id=app.workspace_id,
            snapshot=snapshot,
            deleted_by=deleted_by,
        )
        db.session.add(log_row)
        db.session.flush()  # para que la FK exista antes de tocar la app

        # 1) K8s: borrar el namespace (cascade todos los recursos dentro).
        try:
            if K8sService.namespace_exists(ns):
                K8sService.delete_namespace(ns)
                logger.info("app_force_delete: namespace %s borrado", ns)
        except Exception as exc:
            logger.warning("app_force_delete: fallo borrando namespace %s: %s", ns, exc)

        # 2) GitHub: borrar el repo (si se creo).
        try:
            if GitHubService.repo_exists(slug):
                GitHubService.delete_repo(slug)
                logger.info("app_force_delete: repo GitHub borrado para %s", slug)
        except Exception as exc:
            logger.warning("app_force_delete: fallo borrando repo GitHub para %s: %s", slug, exc)

        # 3) GHCR: borrar el paquete (si existe).
        try:
            ContainerRegistryService.delete_package(slug)
        except Exception as exc:
            logger.warning("app_force_delete: fallo borrando paquete GHCR para %s: %s", slug, exc)

        # 4) DB: marcar scoops como archived y dominios como deleted,
        #    luego soft-delete la app y limpiar eventos del timeline.
        Scoop.query.filter(Scoop.application_id == app.id).update(
            {Scoop.status: STATUS_ARCHIVED}, synchronize_session=False
        )
        Domain.query.filter(
            Domain.application_id == app.id, Domain.deleted_at.is_(None)
        ).update({Domain.deleted_at: utcnow()}, synchronize_session=False)
        # Borrar eventos del timeline (FK cascade al hacer delete de la app).
        from app.modules.apps.model import AppEvent
        AppEvent.query.filter(AppEvent.application_id == app.id).delete(
            synchronize_session=False
        )
        app.deleted_at = utcnow()
        db.session.commit()
        AuditService.log(
            "app_force_delete", "application", app.id,
            {"slug": slug, "namespace": ns, "deletion_log_id": log_row.id},
        )
        return app

    @staticmethod
    def _snapshot_for_deletion(app_id: int, namespace: str) -> dict:
        """Captura el estado completo de la app al momento del borrado."""
        from app.modules.scoops.model import Scoop
        from app.modules.domains.model import Domain
        from app.modules.apps.model import AppEvent

        app = AppsService.get(app_id)
        scoops = Scoop.query.filter(Scoop.application_id == app_id).all()
        domains = Domain.query.filter(Domain.application_id == app_id).all()
        events = AppEvent.query.filter(AppEvent.application_id == app_id).all()

        k8s_resources = {"configmaps": [], "secrets": []}
        try:
            from app.modules.cluster.service import K8sService
            from app.core.k8s import get_clients
            clients = get_clients()
            try:
                cms = clients.core.list_namespaced_config_map(
                    namespace, label_selector=f"app={app.slug}"
                ).items
                k8s_resources["configmaps"] = [
                    {"name": cm.metadata.name, "namespace": cm.metadata.namespace,
                     "labels": cm.metadata.labels or {}}
                    for cm in cms
                ]
            except Exception:
                pass
            try:
                secs = clients.core.list_namespaced_secret(
                    namespace, label_selector=f"app={app.slug}"
                ).items
                k8s_resources["secrets"] = [
                    {"name": s.metadata.name, "namespace": s.metadata.namespace,
                     "labels": s.metadata.labels or {},
                     "keys": sorted((s.data or {}).keys())}
                    for s in secs
                ]
            except Exception:
                pass
        except Exception as exc:
            logger.warning("snapshot_k8s_failed: %s", exc)

        return {
            "app": {
                "id": app.id,
                "name": app.name,
                "slug": app.slug,
                "description": app.description,
                "github_repo_url": app.github_repo_url,
                "docker_image_base": app.docker_image_base,
                "workspace_id": app.workspace_id,
                "status": app.status,
                "created_at": app.created_at.isoformat() if app.created_at else None,
            },
            "scoops": [
                {"id": s.id, "name": s.name, "type": s.type, "version": s.version,
                 "url_registry": s.url_registry, "namespace": s.namespace,
                 "is_productive": s.is_productive, "status": s.status}
                for s in scoops
            ],
            "domains": [
                {"id": d.id, "host": d.host, "scoop_id": d.scoop_id}
                for d in domains
            ],
            "events": [
                {"event": e.event, "status": e.status, "detail": e.detail,
                 "created_at": e.created_at.isoformat() if e.created_at else None}
                for e in events
            ],
            "k8s_namespace": namespace,
            "k8s_resources": k8s_resources,
        }

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