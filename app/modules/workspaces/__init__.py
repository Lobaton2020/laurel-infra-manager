"""Modulo workspaces: agrupamiento logico de primer nivel sobre Applications."""
from app.modules.workspaces.controller import bp
from app.modules.workspaces.model import Workspace
from app.modules.workspaces.schema import (
    WorkspaceCreate,
    WorkspaceListResponse,
    WorkspaceResponse,
    WorkspaceUpdate,
)
from app.modules.workspaces.service import WorkspaceService

__all__ = [
    "bp",
    "Workspace",
    "WorkspaceCreate",
    "WorkspaceUpdate",
    "WorkspaceResponse",
    "WorkspaceListResponse",
    "WorkspaceService",
]