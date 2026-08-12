"""Servicios de records (importados de configurator-lob)."""
from typing import Any

from app.core.db import db
from app.modules.audits.service import AuditService
from app.modules.configurator.schemas.model import Column
from app.modules.configurator.records.model import Record


class RecordService:

    @staticmethod
    def validate_record_data(schema_id: int, data: dict[str, Any]) -> tuple[bool, str]:
        columns = Column.query.filter_by(schema_id=schema_id).all()
        col_map = {c.name: c.data_type for c in columns}

        for key, value in data.items():
            if key not in col_map:
                return False, f"Unknown column: {key}"

            expected_type = col_map[key]
            if expected_type == "string":
                if isinstance(value, str):
                    continue
                if isinstance(value, (int, float)):
                    continue
                return False, f"Column '{key}' must be string"
            elif expected_type == "number":
                if isinstance(value, (int, float)):
                    continue
                if isinstance(value, str):
                    try:
                        float(value)
                        continue
                    except ValueError:
                        return False, f"Column '{key}' must be number"
                return False, f"Column '{key}' must be number"
            elif expected_type == "boolean":
                if isinstance(value, bool):
                    continue
                if isinstance(value, str) and value.lower() in ("true", "false"):
                    continue
                return False, f"Column '{key}' must be boolean"
            elif expected_type == "json":
                if isinstance(value, (dict, list)):
                    continue
                if isinstance(value, str):
                    try:
                        import json
                        json.loads(value)
                        continue
                    except json.JSONDecodeError:
                        pass
                return False, f"Column '{key}' must be json object or array"

        return True, ""

    @staticmethod
    def create(schema_id: int, data: dict[str, Any]) -> tuple[Record | None, str]:
        valid, msg = RecordService.validate_record_data(schema_id, data)
        if not valid:
            return None, msg

        record = Record(schema_id=schema_id, data=data)
        db.session.add(record)
        db.session.commit()
        AuditService.log("create", "record", record.id, {"schema_id": schema_id, "data": data})
        return record, ""

    @staticmethod
    def get_by_schema(schema_id: int, page: int = 1, limit: int = 20) -> dict[str, Any]:
        query = Record.query.filter_by(schema_id=schema_id)
        total = query.count()
        pages = (total + limit - 1) // limit if total else 0
        items = query.order_by(Record.created_at.desc()).offset((page - 1) * limit).limit(limit).all()

        return {
            "items": items,
            "total": total,
            "page": page,
            "limit": limit,
            "pages": pages,
        }

    @staticmethod
    def get_by_id(record_id: int) -> Record | None:
        return db.session.get(Record, record_id)

    @staticmethod
    def update(record_id: int, data: dict[str, Any]) -> tuple[Record | None, str]:
        record = db.session.get(Record, record_id)
        if not record:
            return None, "Record not found"

        valid, msg = RecordService.validate_record_data(record.schema_id, data)
        if not valid:
            return None, msg

        old_data = record.data
        record.data = data
        db.session.commit()
        AuditService.log("update", "record", record_id, {"data": data}, old_data)
        return record, ""

    @staticmethod
    def delete(record_id: int) -> bool:
        record = db.session.get(Record, record_id)
        if record:
            old_data = {"schema_id": record.schema_id, "data": record.data}
            db.session.delete(record)
            db.session.commit()
            AuditService.log("delete", "record", record_id, None, old_data)
            return True
        return False

    @staticmethod
    def search(schema_id: int, filters: dict[str, Any],
               page: int = 1, limit: int = 20) -> dict[str, Any]:
        """Busca por filtros sobre las columnas filterables.

        Soporta comodines de prefijo/sufijo: `foo*`, `*foo`, `*foo*`.
        """
        filterable_columns = Column.query.filter_by(schema_id=schema_id, is_filterable=True).all()
        filterable_names = {c.name for c in filterable_columns}
        valid_filters = {k: v for k, v in filters.items() if k in filterable_names}

        all_records = Record.query.filter_by(schema_id=schema_id).all()
        filtered_records = []
        for record in all_records:
            record_data = record.data or {}
            match = True
            for key, search_value in valid_filters.items():
                record_value = record_data.get(key)
                if record_value is None:
                    match = False
                    break
                record_value_str = str(record_value).lower()
                search_str_lower = str(search_value).lower()

                if search_str_lower.startswith("*") and search_str_lower.endswith("*"):
                    pattern = search_str_lower[1:-1]
                    if pattern not in record_value_str:
                        match = False
                        break
                elif search_str_lower.startswith("*"):
                    pattern = search_str_lower[1:]
                    if not record_value_str.endswith(pattern):
                        match = False
                        break
                elif search_str_lower.endswith("*"):
                    pattern = search_str_lower[:-1]
                    if not record_value_str.startswith(pattern):
                        match = False
                        break
                else:
                    if record_value_str != search_str_lower:
                        match = False
                        break
            if match:
                filtered_records.append(record)

        total = len(filtered_records)
        pages = (total + limit - 1) // limit if total else 0
        start = (page - 1) * limit
        items = filtered_records[start:start + limit]

        return {
            "items": items,
            "total": total,
            "page": page,
            "limit": limit,
            "pages": pages,
        }
