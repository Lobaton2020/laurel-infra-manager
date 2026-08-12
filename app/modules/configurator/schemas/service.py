"""Servicios de esquemas y columnas (importados de configurator-lob).

La auditoria usa el AuditService de laurel: queda unificada con los eventos
de scoops/cluster y registra el usuario autenticado via `g.user`.
"""
from app.core.db import db
from app.modules.audits.service import AuditService
from app.modules.configurator.schemas.model import Column, Schema


class SchemaService:

    @staticmethod
    def create(name: str, description: str | None = None) -> Schema:
        schema = Schema(name=name, description=description)
        db.session.add(schema)
        db.session.commit()
        AuditService.log("create", "schema", schema.id, {"name": name, "description": description})
        return schema

    @staticmethod
    def get_all() -> list[Schema]:
        return Schema.query.order_by(Schema.created_at.desc()).all()

    @staticmethod
    def get_by_id(schema_id: int) -> Schema | None:
        return db.session.get(Schema, schema_id)

    @staticmethod
    def update(schema_id: int, name: str, description: str | None = None) -> Schema | None:
        schema = db.session.get(Schema, schema_id)
        if schema:
            old_data = {"name": schema.name, "description": schema.description}
            schema.name = name
            schema.description = description
            db.session.commit()
            AuditService.log(
                "update", "schema", schema.id,
                {"name": name, "description": description}, old_data,
            )
        return schema

    @staticmethod
    def delete(schema_id: int) -> bool:
        schema = db.session.get(Schema, schema_id)
        if schema:
            old_data = {"name": schema.name, "description": schema.description}
            db.session.delete(schema)
            db.session.commit()
            AuditService.log("delete", "schema", schema_id, None, old_data)
            return True
        return False


class ColumnService:

    @staticmethod
    def create(schema_id: int, name: str, data_type: str,
               is_filterable: bool = True, order: int = 0) -> Column | None:
        schema = db.session.get(Schema, schema_id)
        if not schema:
            return None
        column = Column(
            schema_id=schema_id,
            name=name,
            data_type=data_type,
            is_filterable=is_filterable,
            order=order,
        )
        db.session.add(column)
        db.session.commit()
        AuditService.log(
            "create", "column", column.id,
            {"name": name, "data_type": data_type, "schema_id": schema_id},
        )
        return column

    @staticmethod
    def get_by_schema(schema_id: int) -> list[Column]:
        return Column.query.filter_by(schema_id=schema_id).order_by(Column.order).all()

    @staticmethod
    def get_by_id(column_id: int) -> Column | None:
        return db.session.get(Column, column_id)

    @staticmethod
    def delete(column_id: int) -> bool:
        column = db.session.get(Column, column_id)
        if column:
            old_data = {"name": column.name, "data_type": column.data_type,
                        "schema_id": column.schema_id}
            db.session.delete(column)
            db.session.commit()
            AuditService.log("delete", "column", column_id, None, old_data)
            return True
        return False

    @staticmethod
    def update(column_id: int, name: str | None = None, data_type: str | None = None,
               is_filterable: bool | None = None, order: int | None = None) -> Column | None:
        column = db.session.get(Column, column_id)
        if not column:
            return None
        old_data = {"name": column.name, "data_type": column.data_type,
                    "is_filterable": column.is_filterable, "order": column.order}
        if name is not None:
            column.name = name
        if data_type is not None:
            column.data_type = data_type
        if is_filterable is not None:
            column.is_filterable = is_filterable
        if order is not None:
            column.order = order
        db.session.commit()
        AuditService.log(
            "update", "column", column_id,
            {"name": column.name, "data_type": column.data_type,
             "is_filterable": column.is_filterable, "order": column.order},
            old_data,
        )
        return column
