"""Schemas Pydantic para el CRUD de Application."""
import re
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from app.modules.scoops.schema import slugify

# DNS-1123 reutilizado del modulo scoops (mismo patron).
DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")

# docker_image_base: <algo>/<algo>/<algo>... con `-` y `_` permitidos.
# Acepta 2 o 3 segmentos: `namespace/repo` o `registry/namespace/repo`.
# Sin tag. 4+ segmentos no son validos como imagen Docker.
DOCKER_IMAGE_BASE = re.compile(
    r"^[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+(/[a-zA-Z0-9._-]+)?$"
)


def _validate_name(v: str) -> str:
    """Valida que `name` (1-100 chars) sea texto razonable."""
    if not v or not v.strip():
        raise ValueError("name no puede estar vacio")
    if len(v) > 100:
        raise ValueError("name no puede tener mas de 100 caracteres")
    return v.strip()


def _validate_docker_image_base(v: Optional[str]) -> Optional[str]:
    if v is None:
        return v
    v = v.strip()
    if not v:
        return None
    if not DOCKER_IMAGE_BASE.match(v):
        raise ValueError(
            "docker_image_base debe tener formato '<registry>/<repo>' "
            "(ej. 'aflobaton/laurel-notas'). Sin tag."
        )
    return v


def _validate_github_url(v: Optional[str]) -> Optional[str]:
    if v is None:
        return v
    v = v.strip()
    if not v:
        return None
    if not v.startswith("https://github.com/"):
        raise ValueError("github_repo_url debe empezar con 'https://github.com/'")
    if len(v) > 255:
        raise ValueError("github_repo_url no puede tener mas de 255 caracteres")
    return v


class ApplicationCreate(BaseModel):
    """Crea una nueva Application.

    Atributos opcionales editables para excepciones al bootstrap por defecto:
    - `github_repo_url`: si lo pasan manualmente, NO se intenta crear el repo
      en GitHub aunque `create_github_repo=true`.
    - `docker_image_base`: si lo pasan manualmente, se respeta tal cual
      aunque no siga el patron `laurel_<slug>`.
    """
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = Field(None, max_length=500)
    github_repo_url: Optional[str] = Field(None, max_length=255)
    docker_image_base: Optional[str] = Field(None, max_length=255)
    create_github_repo: bool = False

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

    @field_validator("github_repo_url")
    @classmethod
    def _v_github_url(cls, v: Optional[str]) -> Optional[str]:
        return _validate_github_url(v)

    @field_validator("docker_image_base")
    @classmethod
    def _v_image_base(cls, v: Optional[str]) -> Optional[str]:
        return _validate_docker_image_base(v)


class ApplicationUpdate(BaseModel):
    """Actualizacion parcial: `name` y `slug` son inmutables.

    El caller puede sobreescribir `github_repo_url` o `docker_image_base`
    para excepciones al bootstrap automatico.
    """
    description: Optional[str] = Field(None, max_length=500)
    github_repo_url: Optional[str] = Field(None, max_length=255)
    docker_image_base: Optional[str] = Field(None, max_length=255)

    @field_validator("description")
    @classmethod
    def _v_description(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip()
        return v or None

    @field_validator("github_repo_url")
    @classmethod
    def _v_github_url(cls, v: Optional[str]) -> Optional[str]:
        return _validate_github_url(v)

    @field_validator("docker_image_base")
    @classmethod
    def _v_image_base(cls, v: Optional[str]) -> Optional[str]:
        return _validate_docker_image_base(v)


class ApplicationResponse(BaseModel):
    id: int
    name: str
    slug: str
    description: Optional[str] = None
    github_repo_url: Optional[str] = None
    docker_image_base: Optional[str] = None

    scoops_count: int = 0
    domains_count: int = 0
    namespace: str

    created_at: str
    updated_at: str

    model_config = {"from_attributes": True}

    @classmethod
    def from_app(cls, app, scoops_count: int = 0, domains_count: int = 0) -> "ApplicationResponse":
        return cls(
            id=app.id,
            name=app.name,
            slug=app.slug,
            description=app.description,
            github_repo_url=app.github_repo_url,
            docker_image_base=app.docker_image_base,
            scoops_count=scoops_count,
            domains_count=domains_count,
            namespace=app.slug,
            created_at=app.created_at.isoformat() if app.created_at else "",
            updated_at=app.updated_at.isoformat() if app.updated_at else "",
        )


class ApplicationListResponse(BaseModel):
    items: list[ApplicationResponse]
    total: int
    page: int
    limit: int
    pages: int