"""Estadisticas globales del modulo Configurator (importado de configurator-lob)."""
from app.modules.configurator.records.model import Record
from app.modules.configurator.schemas.model import Column, Schema


class StatsService:

    @staticmethod
    def get_global() -> dict:
        total_schemas = Schema.query.count()
        total_columns = Column.query.count()
        total_records = Record.query.count()

        schemas = Schema.query.all()
        schemas_with_counts = []
        for s in schemas:
            col_count = Column.query.filter_by(schema_id=s.id).count()
            rec_count = Record.query.filter_by(schema_id=s.id).count()
            schemas_with_counts.append({
                "id": s.id,
                "name": s.name,
                "columns": col_count,
                "records": rec_count,
            })

        return {
            "total_schemas": total_schemas,
            "total_columns": total_columns,
            "total_records": total_records,
            "schemas": schemas_with_counts,
        }
