"""CRUD + lifecycle de Application."""
import logging

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app.core.db import db
from app.core.errors import AppError, ClusterError, ConflictError, NotFoundError
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
        """Crea una Application nueva con rollback semantico.

        Pasos (en orden):
        1. Repo GitHub (`laurel_<slug>`) si se pidio `create_github_repo`.
        2. Namespace K8s `user-apps-<slug>` (idempotente).
        3. INSERT en BD + eventos de timeline.

        Si CUALQUIER llamada externa (GitHub create, K8s create) o el
        INSERT en BD falla, se hace rollback de lo que se haya creado:
        - DB: `db.session.rollback()`.
        - GitHub: `delete_repo` si llegamos a crearlo.
        - K8s: `delete_namespace` si llegamos a crearlo.

        La excepcion original se propaga al controller; el rollback es
        best-effort (un fallo de cleanup se loguea y se descarta: la
        excepcion original es la que importa al usuario).

        Nota: el caso "no se proporciono `github_repo_url` ni
        `create_github_repo`" NO se considera falla de GitHub (no se hace
        ninguna llamada): la app se crea igualmente con `status=error` y
        el evento de timeline marca el motivo.
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
        # Flags de rollback: solo limpiamos lo que marcamos como creado.
        created_github_repo = False
        created_k8s_namespace = False
        created_jenkins_job = False
        created_docker_repo = False
        ns = f"user-apps-{slug}"

        # Step 1: repo GitHub (si se pidio crearlo).
        if github_url:
            events.append(("github_repo", "ok", f"Repo GitHub ya provisto: {github_url}"))
        elif create_repo_flag:
            try:
                result = GitHubService.create_empty_repo(slug)
                github_url = result["html_url"]
                created_github_repo = True
                AuditService.log(
                    "github_repo_created", "application", None,
                    {"slug": slug, "url": github_url},
                )
                events.append((
                    "github_repo", "ok",
                    f"Repo GitHub creado: {result['full_name']}",
                ))
            except AppError as exc:
                # Falla real: nada que limpiar todavia (no llegamos a tocar
                # el namespace ni la BD). Propagar con contexto.
                logger.warning("github_repo_failed para %s: %s", slug, exc.message)
                raise AppError(
                    f"No se pudo crear el repo en GitHub: {exc.message}",
                    status_code=exc.status_code,
                    details={"step": "github_repo"},
                ) from exc
        else:
            events.append((
                "github_repo", "error",
                "Falta GitHub: no se proporciono github_repo_url ni create_github_repo",
            ))

        # Step 2: imagen de contenedor en Docker Hub. A diferencia de
        # GHCR (que se materializa en el primer push), Docker Hub
        # requiere un POST explicito a /v2/repositories/ para crear el
        # repo. create_repo es idempotente (409 -> existed=True).
        # Si falla, NO es fatal: la app se crea igual con status=error
        # y el operador puede rotar las credenciales de Docker Hub y
        # reintentar manualmente.
        if docker_base is None:
            try:
                result = ContainerRegistryService.create_repo(
                    slug,
                    description=data.get("description", ""),
                )
                user = result["namespace"]
                docker_base = f"docker.io/{user}/{result['name']}"
                created_docker_repo = not result.get("existed", False)
                events.append((
                    "docker_repo", "ok",
                    f"Repo Docker Hub creado: {user}/{result['name']}"
                    + (" (ya existia)" if result.get("existed") else ""),
                ))
            except AppError as exc:
                # Fallo el create (creds mal, Docker Hub caido, etc).
                # La app igual se crea, pero con status=error para que
                # el operador lo note y pueda reintentar.
                logger.warning(
                    "docker_repo_failed para %s: %s", slug, exc.message
                )
                docker_base = ContainerRegistryService.suggested_base(slug)
                events.append((
                    "docker_repo", "error",
                    f"No se pudo crear el repo en Docker Hub: {exc.message}. "
                    f"Imagen base sugerida: {docker_base} (el job de Jenkins "
                    "fallara al pushear hasta que se cree manualmente).",
                ))
        else:
            events.append((
                "docker_repo", "ok",
                f"Imagen base ya provista: {docker_base} "
                "(asume que el repo en Docker Hub existe o lo creara el job)",
            ))

        # Step 3: namespace K8s `user-apps-<slug>` (idempotente).
        from app.modules.cluster.service import K8sService
        try:
            if not K8sService.namespace_exists(ns):
                K8sService.create_namespace(ns)
                created_k8s_namespace = True
            events.append(("k8s_namespace", "ok", f"Namespace K8s listo: {ns}"))
        except Exception as exc:
            logger.warning("k8s_namespace_failed para %s: %s", slug, exc)
            AppsService._rollback_external(
                slug=slug, ns=ns,
                created_github=created_github_repo,
                created_namespace=False,
            )
            raise ClusterError(
                f"No se pudo crear el namespace K8s '{ns}': {exc}",
                status_code=502,
                details={"step": "k8s_namespace"},
            ) from exc

        # Step 4: INSERT en BD.
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
            AppsService._rollback_external(
                slug=slug, ns=ns,
                created_github=created_github_repo,
                created_namespace=created_k8s_namespace,
                created_docker=created_docker_repo,
            )
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

        # Step 5: crear el job `laurel_<slug>` en Jenkins con el pipeline
        # CI/CD de 3 stages (tests -> build -> push). Solo lo creamos
        # (NO lo disparamos): un push a master lo hara despues via el
        # webhook. Si falla, hacemos rollback completo: el INSERT de la
        # app todavia no esta commiteado (solo flushed), asi que
        # session.rollback() lo deshace; ademas limpiamos namespace y
        # GH repo si llegaron a crearse.
        from app.modules.integrations.jenkins.service import JenkinsService
        try:
            JenkinsService.create_job(
                slug=slug,
                test_cmd=app.test_cmd,
                image_base=app.docker_image_base,
                github_repo_url=app.github_repo_url,
            )
            created_jenkins_job = True
            events.append((
                "jenkins_job", "ok",
                f"Job Jenkins laurel_{slug} creado con pipeline tests->build->push",
            ))
        except AppError as exc:
            logger.warning("jenkins_job_failed para %s: %s", slug, exc.message)
            db.session.rollback()
            AppsService._rollback_external(
                slug=slug, ns=ns,
                created_github=created_github_repo,
                created_namespace=created_k8s_namespace,
                created_docker=created_docker_repo,
                created_jenkins=False,  # nunca llegamos a crearlo
            )
            raise AppError(
                f"No se pudo crear el job en Jenkins: {exc.message}",
                status_code=exc.status_code,
                details={"step": "jenkins_job"},
            ) from exc

        # Persistir el evento jenkins_job (los demas eventos ya estan en
        # la session desde antes). Un solo commit al final.
        for event, status, detail in events:
            if event == "jenkins_job":
                db.session.add(AppEvent(
                    application_id=app.id,
                    event=event,
                    status=status,
                    detail=detail,
                ))
        db.session.commit()

        AuditService.log(
            "app_create", "application", app.id,
            {"slug": slug, "name": name, "github_repo_url": github_url,
             "status": app.status},
        )
        return app
    @staticmethod
    def _rollback_external(
        slug: str,
        ns: str,
        *,
        created_github: bool,
        created_namespace: bool,
        created_jenkins: bool = False,
        created_docker: bool = False,
    ) -> None:
        """Best-effort cleanup de recursos externos cuando `create` falla.

        Solo limpia lo que se marco como creado. Errores de cleanup se
        loguean y descartan: la excepcion original (que disparo el
        rollback) es la que importa al usuario y la que se propaga.
        """
        from app.modules.cluster.service import K8sService
        from app.modules.integrations.docker.service import ContainerRegistryService
        from app.modules.integrations.github.service import GitHubService
        from app.modules.integrations.jenkins.service import JenkinsService

        if created_namespace:
            try:
                K8sService.delete_namespace(ns)
                logger.info("create_rollback: namespace %s borrado", ns)
            except Exception as exc:
                logger.warning(
                    "create_rollback: fallo borrando namespace %s: %s", ns, exc,
                )
        if created_github:
            try:
                GitHubService.delete_repo(slug)
                logger.info("create_rollback: GitHub repo %s borrado", slug)
            except Exception as exc:
                logger.warning(
                    "create_rollback: fallo borrando GitHub repo %s: %s", slug, exc,
                )
        if created_docker:
            try:
                ContainerRegistryService.delete_repo(slug)
                logger.info("create_rollback: Docker Hub repo %s borrado", slug)
            except Exception as exc:
                logger.warning(
                    "create_rollback: fallo borrando Docker Hub repo %s: %s",
                    slug, exc,
                )
        if created_jenkins:
            try:
                JenkinsService.delete_job(slug)
                logger.info("create_rollback: Jenkins job laurel_%s borrado", slug)
            except Exception as exc:
                logger.warning(
                    "create_rollback: fallo borrando Jenkins job %s: %s", slug, exc,
                )
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
    def delete(app_id: int) -> Application:
        """Hard-delete real del registro en BD y limpieza absoluta.

        Antes del borrado guarda un snapshot completo en `AppDeletionLog`
        para trazabilidad (la tabla `app_deletion_logs` mantiene los datos
        aunque la app se elimine del registro principal).

        Luego elimina:
        - K8s: namespace `user-apps-<slug>` (cascade: secrets, configmaps,
          deployments, services, ingresses, hpa, jobs, etc.).
        - DB: HARD DELETE de la app. Las FKs en cascada se encargan del
          resto: scoops.application_id SET NULL (los scoops sobreviven sin
          app), domains.application_id CASCADE (se borran), app_events
          CASCADE (se borran). Queda solo `app_deletion_logs` con el
          snapshot.
        - GitHub: borra el repo `laurel_<slug>` en la org.
        - Docker Hub: borra el repo `<user>/laurel_<slug>`.
        - Jenkins: borra el job `laurel_<slug>` con el pipeline CI/CD.

        Si algun paso externo falla (k8s/github/docker/jenkins), logueamos
        warning y seguimos con el resto: la app debe quedar eliminada
        en la BD aunque queden recursos huerfanos en el cluster.
        """
        from app.modules.apps.model import AppDeletionLog
        from app.modules.cluster.service import K8sService
        from app.modules.integrations.docker.service import ContainerRegistryService
        from app.modules.integrations.github.service import GitHubService
        from app.modules.integrations.jenkins.service import JenkinsService

        app = AppsService.get(app_id)
        slug = app.slug
        ns = f"user-apps-{slug}"

        # 0) Snapshot completo de la config para trazabilidad (en
        #    `app_deletion_logs` que NO tiene FK a `applications`).
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
        db.session.flush()  # que el INSERT quede antes del DELETE de la app

        # 1) K8s: borrar el namespace (cascade todos los recursos dentro).
        try:
            if K8sService.namespace_exists(ns):
                K8sService.delete_namespace(ns)
                logger.info("app_hard_delete: namespace %s borrado", ns)
        except Exception as exc:
            logger.warning("app_hard_delete: fallo borrando namespace %s: %s", ns, exc)

        # 2) GitHub: borrar el repo (si se creo).
        try:
            if GitHubService.repo_exists(slug):
                GitHubService.delete_repo(slug)
                logger.info("app_hard_delete: repo GitHub borrado para %s", slug)
        except Exception as exc:
            logger.warning("app_hard_delete: fallo borrando repo GitHub para %s: %s", slug, exc)

        # 3) Docker Hub: borrar el repo (si existe).
        try:
            if ContainerRegistryService.repo_exists(slug):
                ContainerRegistryService.delete_repo(slug)
                logger.info("app_hard_delete: repo Docker Hub borrado para %s", slug)
        except Exception as exc:
            logger.warning("app_hard_delete: fallo borrando repo Docker Hub para %s: %s", slug, exc)

        # 3.5) Jenkins: borrar el job `laurel_<slug>`. Asi no queda un job
        #      huerfano apuntando a un repo borrado. Si el cluster no tiene
        #      Jenkins configurado, el service devuelve False sin lanzar.
        try:
            if JenkinsService.job_exists(slug):
                deleted = JenkinsService.delete_job(slug)
                if deleted:
                    logger.info("app_hard_delete: Jenkins job laurel_%s borrado", slug)
                else:
                    logger.warning("app_hard_delete: Jenkins job laurel_%s no se borro", slug)
        except Exception as exc:
            logger.warning("app_hard_delete: fallo borrando Jenkins job %s: %s", slug, exc)

        # 4) DB: HARD DELETE. Las FKs declaradas en los modelos (scoops SET NULL,
        #    domains CASCADE, app_events CASCADE) hacen el resto. En SQLite
        #    se necesita PRAGMA foreign_keys=ON (activado en tests/conftest).
        deleted_id = app.id
        db.session.delete(app)
        db.session.commit()
        AuditService.log(
            "app_hard_delete", "application", deleted_id,
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