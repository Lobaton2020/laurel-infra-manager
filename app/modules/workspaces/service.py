"""CRUD + lifecycle de Workspace (scoped por owner_sub)."""
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


class WorkspaceService:

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
    def _base_query(owner_sub: str):
        return Workspace.query.filter(
            Workspace.owner_sub == owner_sub,
            Workspace.deleted_at.is_(None),
        )

    @staticmethod
    def list(owner_sub: str, page: int = 1, limit: int = 20) -> dict:
        """Lista los workspaces del usuario (no soft-deleted) con paginacion."""
        query = WorkspaceService._base_query(owner_sub)
        total = query.count()
        items = (
            query.order_by(Workspace.created_at.desc())
            .limit(limit).offset((page - 1) * limit).all()
        )
        ids = [ws.id for ws in items]
        apps_count: dict[int, int] = {}
        if ids:
            for ws_id, count in db.session.query(
                Application.workspace_id, func.count(Application.id)
            ).filter(
                Application.workspace_id.in_(ids),
                Application.deleted_at.is_(None),
            ).group_by(Application.workspace_id).all():
                apps_count[ws_id] = count
        pages = max(1, (total + limit - 1) // limit) if total else 0
        return {
            "items": items, "total": total, "page": page, "limit": limit,
            "pages": pages, "apps_count": apps_count,
        }

    @staticmethod
    def get(ws_id: int, owner_sub: str) -> Workspace:
        ws = WorkspaceService._base_query(owner_sub).filter_by(id=ws_id).first()
        if ws is None:
            raise NotFoundError(f"Workspace {ws_id} no encontrado")
        return ws

    @staticmethod
    def apps_count(ws_id: int) -> int:
        return Application.query.filter(
            Application.workspace_id == ws_id,
            Application.deleted_at.is_(None),
        ).count()

    @staticmethod
    def create(owner_sub: str, data: dict) -> Workspace:
        name = data["name"]
        slug = WorkspaceService._to_slug(name)
        duplicate = WorkspaceService._base_query(owner_sub).filter(
            (Workspace.name == name) | (Workspace.slug == slug)
        ).first()
        if duplicate is not None:
            raise ConflictError(
                f"Ya existe un Workspace con name='{name}' o slug='{slug}'"
            )

        ws = Workspace(
            name=name,
            slug=slug,
            owner_sub=owner_sub,
            description=data.get("description"),
        )
        try:
            db.session.add(ws)
            db.session.commit()
        except IntegrityError as exc:
            db.session.rollback()
            raise ConflictError(
                f"Ya existe un Workspace con name='{name}' o slug='{slug}'"
            ) from exc

        AuditService.log(
            "workspace_create", "workspace", ws.id,
            {"slug": slug, "name": name, "owner_sub": owner_sub},
        )
        return ws

    @staticmethod
    def update(ws_id: int, owner_sub: str, data: dict) -> Workspace:
        ws = WorkspaceService.get(ws_id, owner_sub)
        if "name" in data:
            slug = WorkspaceService._to_slug(data["name"])
            duplicate = WorkspaceService._base_query(owner_sub).filter(
                Workspace.id != ws_id,
                (Workspace.name == data["name"]) | (Workspace.slug == slug),
            ).first()
            if duplicate is not None:
                raise ConflictError(
                    f"Ya existe un Workspace con name='{data['name']}' o slug='{slug}'"
                )
            ws.name = data["name"]
            ws.slug = slug
        if "description" in data:
            ws.description = data["description"]
        try:
            db.session.commit()
        except IntegrityError as exc:
            db.session.rollback()
            raise ConflictError("Ya existe un Workspace con ese nombre o slug") from exc
        AuditService.log(
            "workspace_update", "workspace", ws.id,
            {k: v for k, v in data.items() if k in ("name", "description")},
        )
        return ws

    @staticmethod
    def soft_delete(ws_id: int, owner_sub: str) -> Workspace:
        """Soft-delete: marca `deleted_at` y desagrupa sus apps
        (workspace_id -> NULL). Las apps NO se borran."""
        ws = WorkspaceService.get(ws_id, owner_sub)
        Application.query.filter_by(workspace_id=ws.id).update(
            {Application.workspace_id: None}
        )
        ws.deleted_at = utcnow()
        db.session.commit()
        AuditService.log(
            "workspace_soft_delete", "workspace", ws.id,
            {"slug": ws.slug},
        )
        return ws