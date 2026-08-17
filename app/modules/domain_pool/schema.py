"""Schemas Pydantic para el CRUD del catalogo de dominios de segundo nivel."""
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from app.modules.domains.schema import _HOST_RE


def _validate_domain(v: str) -> str:
    if not v or not v.strip():
        raise ValueError("domain no puede estar vacio")
    v = v.strip().lower()
    if len(v) > 253:
        raise ValueError("domain no puede tener mas de 253 caracteres")
    if not _HOST_RE.match(v):
        raise ValueError(
            "domain debe ser un FQDN valido de segundo nivel (ej. 'andreslobaton.top'). "
            "Sin protocolo, sin subdominio, en minusculas."
        )
    return v


class DomainPoolCreate(BaseModel):
    domain: str = Field(..., min_length=1, max_length=253)
    description: Optional[str] = Field(None, max_length=255)

    @field_validator("domain")
    @classmethod
    def _v_domain(cls, v: str) -> str:
        return _validate_domain(v)


class DomainPoolUpdate(BaseModel):
    """El dominio es inmutable; solo se puede editar `description`."""
    description: Optional[str] = Field(None, max_length=255)


class DomainPoolResponse(BaseModel):
    id: int
    domain: str
    description: Optional[str] = None
    created_at: str
    updated_at: str

    model_config = {"from_attributes": True}

    @classmethod
    def from_pool(cls, pool) -> "DomainPoolResponse":
        return cls(
            id=pool.id,
            domain=pool.domain,
            description=pool.description,
            created_at=pool.created_at.isoformat() if pool.created_at else "",
            updated_at=pool.updated_at.isoformat() if pool.updated_at else "",
        )
