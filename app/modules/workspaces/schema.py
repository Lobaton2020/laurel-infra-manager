"""Schemas Pydantic para el CRUD de Workspace."""
from typing import Optional

from pydantic import BaseModel, Field, field_validator


def _validate_name(v: str) -> str:
    if not v or not v.strip():
        raise ValueError("name no puede estar vacio")
    if len(v) > 100:
        raise ValueError("name no puede tener mas de 100 caracteres")
    return v.strip()


class WorkspaceCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = Field(None, max_length=500)

    @field_validator("name")
    @classmethod
    def _v_name(cls, v: str) -> str:
        return _validate_name(v)

    @field_validator("description")
    @classmethod
    def _v_description(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip()
        return v or None


class WorkspaceUpdate(BaseModel):
    """Actualizacion parcial: si llega `name`, el slug se re-deriva de el."""
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    description: Optional[str] = Field(None, max_length=500)

    @field_validator("name")
    @classmethod
    def _v_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        return _validate_name(v)

    @field_validator("description")
    @classmethod
    def _v_description(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip()
        return v or None


class WorkspaceResponse(BaseModel):
    id: int
    name: str
    slug: str
    description: Optional[str] = None
    owner_sub: str
    apps_count: int = 0

    created_at: str
    updated_at: str

    model_config = {"from_attributes": True}

    @classmethod
    def from_ws(cls, ws, apps_count: int = 0) -> "WorkspaceResponse":
        return cls(
            id=ws.id,
            name=ws.name,
            slug=ws.slug,
            description=ws.description,
            owner_sub=ws.owner_sub,
            apps_count=apps_count,
            created_at=ws.created_at.isoformat() if ws.created_at else "",
            updated_at=ws.updated_at.isoformat() if ws.updated_at else "",
        )


class WorkspaceListResponse(BaseModel):
    items: list[WorkspaceResponse]
    total: int
    page: int
    limit: int
    pages: int