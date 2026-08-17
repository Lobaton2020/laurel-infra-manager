"""Modulo domains: Domain de primer nivel, recurso de exposicion publica."""
from app.modules.domains.controller import bp
from app.modules.domains.model import Domain
from app.modules.domains.schema import (
    DomainCreate,
    DomainListResponse,
    DomainResponse,
    DomainStatusResponse,
    DomainUpdate,
)
from app.modules.domains.service import DomainService

__all__ = [
    "bp",
    "Domain",
    "DomainCreate",
    "DomainUpdate",
    "DomainResponse",
    "DomainListResponse",
    "DomainStatusResponse",
    "DomainService",
]