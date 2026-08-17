"""Schemas Pydantic para el CRUD de Domain."""
import re
from typing import Optional

from pydantic import BaseModel, Field, field_validator

# FQDN basico: cada label DNS-1123, al menos un punto, TLD >= 2 chars.
_HOST_RE = re.compile(
    r"^([a-z0-9]([-a-z0-9]*[a-z0-9])?\.)+[a-z]{2,}$"
)


def _validate_host(v: str) -> str:
    if not v or not v.strip():
        raise ValueError("host no puede estar vacio")
    v = v.strip().lower()
    if len(v) > 253:
        raise ValueError("host no puede tener mas de 253 caracteres")
    if not _HOST_RE.match(v):
        raise ValueError(
            "host debe ser un FQDN valido (ej. 'notas.resto.com'). "
            "Cada label en minusculas, sin caracteres especiales."
        )
    return v


def _host_to_secret_name(host: str) -> str:
    """Convierte `notas.resto.com` -> `notas-resto-com` (DNS-1123, max 63)."""
    return host.replace(".", "-")[:63].rstrip("-")


class DomainCreate(BaseModel):
    """Crea un Domain. Valida que el scoop pertenezca a la app
    y que el scoop sea de tipo `api`."""
    application_id: int = Field(..., gt=0)
    scoop_id: int = Field(..., gt=0)
    host: str = Field(..., min_length=1, max_length=253)
    tls: bool = True

    @field_validator("host")
    @classmethod
    def _v_host(cls, v: str) -> str:
        return _validate_host(v)


class DomainUpdate(BaseModel):
    """Solo se puede cambiar `tls` y `host` despues de crear.
    `application_id` y `scoop_id` son inmutables (cambiar el target es
    semanticamente otro domain)."""
    host: Optional[str] = Field(None, max_length=253)
    tls: Optional[bool] = None

    @field_validator("host")
    @classmethod
    def _v_host(cls, v: Optional[str]) -> Optional[str]:
        return _validate_host(v) if v is not None else v


class DomainResponse(BaseModel):
    id: int
    application_id: int
    scoop_id: int
    host: str
    tls: bool
    status: str
    secret_name: str
    namespace: str
    scoop_name: str
    application_slug: str

    created_at: str
    updated_at: str

    model_config = {"from_attributes": True}

    @classmethod
    def from_domain(cls, domain) -> "DomainResponse":
        return cls(
            id=domain.id,
            application_id=domain.application_id,
            scoop_id=domain.scoop_id,
            host=domain.host,
            tls=domain.tls,
            status=domain.status,
            secret_name=domain.secret_name,
            namespace=domain.application.slug,
            scoop_name=domain.scoop.name,
            application_slug=domain.application.slug,
            created_at=domain.created_at.isoformat() if domain.created_at else "",
            updated_at=domain.updated_at.isoformat() if domain.updated_at else "",
        )


class DomainListResponse(BaseModel):
    items: list[DomainResponse]
    total: int
    page: int
    limit: int
    pages: int


class DomainStatusResponse(BaseModel):
    """Estado del domain contrastado con el cluster."""
    domain: DomainResponse
    deployed: bool
    certificate_ready: bool
    domain_status: str
    ingress_exists: bool
    certificate: Optional[dict] = None
    certificate_request: Optional[dict] = None
    challenges: list[dict] = Field(default_factory=list)
    events: list[dict] = Field(default_factory=list)
    message: Optional[str] = None