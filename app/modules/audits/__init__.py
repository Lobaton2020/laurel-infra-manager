"""Modulo Audits: trazabilidad de las mutaciones del catalogo y del cluster."""
from app.modules.audits.controller import bp
from app.modules.audits.model import Audit
from app.modules.audits.service import AuditService

__all__ = ["bp", "Audit", "AuditService"]
