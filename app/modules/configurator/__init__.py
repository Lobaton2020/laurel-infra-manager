"""Modulo Configurator: schemas/columns/records de configuracion.

Importado del backend de configurator-lob y adaptado al patron de laurel.
La auditoria usa el AuditService unificado de laurel (misma tabla `audits`).
"""
from app.modules.configurator.controller import bp
from app.modules.configurator.records.model import Record
from app.modules.configurator.schemas.model import Column, Schema
from app.modules.configurator.schemas.service import ColumnService, SchemaService
from app.modules.configurator.records.service import RecordService
from app.modules.configurator.stats.service import StatsService

__all__ = [
    "bp",
    "Schema", "Column", "Record",
    "SchemaService", "ColumnService", "RecordService", "StatsService",
]