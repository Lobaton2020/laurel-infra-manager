"""Controller del modulo Configurator (schemas, columns, records, stats).

Rutas importadas de los `adapters/http/*` de configurator-lob y convertidas
al patron de blueprints de laurel. Heredan el gate de autenticacion global.
"""
from flask import Blueprint, jsonify

from app.core.errors import AppError, NotFoundError
from app.core.http import pagination, parse_body, raw_body
from app.modules.audits.service import AuditService
from app.modules.configurator.schema import (
    ColumnResponse,
    RecordCreate,
    RecordListResponse,
    RecordResponse,
    RecordUpdate,
    SchemaResponse,
    SchemaWithColumns,
    SchemaCreate,
    ColumnCreate,
)
from app.modules.configurator.records.service import RecordService
from app.modules.configurator.schemas.service import ColumnService, SchemaService
from app.modules.configurator.stats.service import StatsService

bp = Blueprint("configurator", __name__, url_prefix="/api")


def _dump(model, instance) -> dict:
    return model.model_validate(instance).model_dump()


def _record_payload(record) -> dict:
    return RecordResponse.model_validate(record).model_dump()


# ---------- Schemas ----------

@bp.get("/schemas")
def list_schemas():
    """Lista todos los schemas de configuracion
    ---
    tags: [Configurator]
    responses:
      200: {description: Lista de schemas}
    """
    schemas = SchemaService.get_all()
    return jsonify([_dump(SchemaResponse, s) for s in schemas])


@bp.post("/schemas")
def create_schema():
    """Crea un schema de configuracion
    ---
    tags: [Configurator]
    parameters:
      - name: body
        in: body
        required: true
        schema:
          type: object
          properties:
            name: {type: string}
            description: {type: string}
    responses:
      201: {description: Schema creado}
    """
    payload = parse_body(SchemaCreate)
    schema = SchemaService.create(payload.name, payload.description)
    return jsonify(_dump(SchemaResponse, schema)), 201


@bp.get("/schemas/<int:schema_id>")
def get_schema(schema_id: int):
    """Detalle de un schema con sus columnas
    ---
    tags: [Configurator]
    parameters:
      - {name: schema_id, in: path, required: true, type: integer}
    responses:
      200: {description: Schema con columnas}
      404: {description: No existe}
    """
    schema = SchemaService.get_by_id(schema_id)
    if not schema:
        raise NotFoundError("Schema no encontrado")
    columns = ColumnService.get_by_schema(schema_id)
    result = SchemaWithColumns(
        id=schema.id,
        name=schema.name,
        description=schema.description,
        created_at=schema.created_at,
        updated_at=schema.updated_at,
        columns=[_dump(ColumnResponse, c) for c in columns],
    )
    return jsonify(result.model_dump())


@bp.put("/schemas/<int:schema_id>")
def update_schema(schema_id: int):
    """Actualiza un schema
    ---
    tags: [Configurator]
    parameters:
      - {name: schema_id, in: path, required: true, type: integer}
      - name: body
        in: body
        schema:
          type: object
          properties:
            name: {type: string}
            description: {type: string}
    responses:
      200: {description: Schema actualizado}
      404: {description: No existe}
    """
    payload = parse_body(SchemaCreate)
    schema = SchemaService.update(schema_id, payload.name, payload.description)
    if not schema:
        raise NotFoundError("Schema no encontrado")
    return jsonify(_dump(SchemaResponse, schema))


@bp.delete("/schemas/<int:schema_id>")
def delete_schema(schema_id: int):
    """Elimina un schema (y sus columnas/records en cascada)
    ---
    tags: [Configurator]
    parameters:
      - {name: schema_id, in: path, required: true, type: integer}
    responses:
      200: {description: Schema eliminado}
      404: {description: No existe}
    """
    if not SchemaService.delete(schema_id):
        raise NotFoundError("Schema no encontrado")
    return jsonify({"message": "Deleted"})


# ---------- Columns ----------

@bp.get("/schemas/<int:schema_id>/columns")
def get_columns(schema_id: int):
    """Lista las columnas de un schema
    ---
    tags: [Configurator]
    parameters:
      - {name: schema_id, in: path, required: true, type: integer}
    responses:
      200: {description: Lista de columnas}
    """
    if not SchemaService.get_by_id(schema_id):
        raise NotFoundError("Schema no encontrado")
    columns = ColumnService.get_by_schema(schema_id)
    return jsonify([_dump(ColumnResponse, c) for c in columns])


@bp.post("/schemas/<int:schema_id>/columns")
def create_column(schema_id: int):
    """Agrega una columna a un schema
    ---
    tags: [Configurator]
    parameters:
      - {name: schema_id, in: path, required: true, type: integer}
      - name: body
        in: body
        required: true
        schema:
          type: object
          properties:
            name: {type: string}
            data_type: {type: string, enum: [string, number, boolean, json]}
            is_filterable: {type: boolean}
            order: {type: integer}
    responses:
      201: {description: Columna creada}
      404: {description: Schema no encontrado}
    """
    payload = parse_body(ColumnCreate)
    column = ColumnService.create(
        schema_id, payload.name, payload.data_type,
        payload.is_filterable, payload.order,
    )
    if not column:
        raise NotFoundError("Schema no encontrado")
    return jsonify(_dump(ColumnResponse, column)), 201


@bp.get("/schemas/<int:schema_id>/columns/<int:column_id>")
def get_column(schema_id: int, column_id: int):
    """Detalle de una columna
    ---
    tags: [Configurator]
    parameters:
      - {name: schema_id, in: path, required: true, type: integer}
      - {name: column_id, in: path, required: true, type: integer}
    responses:
      200: {description: Columna}
      404: {description: No existe}
    """
    column = ColumnService.get_by_id(column_id)
    if not column:
        raise NotFoundError("Columna no encontrada")
    return jsonify(_dump(ColumnResponse, column))


@bp.put("/schemas/<int:schema_id>/columns/<int:column_id>")
def update_column(schema_id: int, column_id: int):
    """Actualiza una columna
    ---
    tags: [Configurator]
    parameters:
      - {name: schema_id, in: path, required: true, type: integer}
      - {name: column_id, in: path, required: true, type: integer}
      - name: body
        in: body
        required: true
        schema:
          type: object
          properties:
            name: {type: string}
            data_type: {type: string}
            is_filterable: {type: boolean}
            order: {type: integer}
    responses:
      200: {description: Columna actualizada}
      404: {description: No existe}
    """
    payload = parse_body(ColumnCreate)
    column = ColumnService.update(
        column_id, payload.name, payload.data_type,
        payload.is_filterable, payload.order,
    )
    if not column:
        raise NotFoundError("Columna no encontrada")
    return jsonify(_dump(ColumnResponse, column))


@bp.delete("/schemas/<int:schema_id>/columns/<int:column_id>")
def delete_column(schema_id: int, column_id: int):
    """Elimina una columna
    ---
    tags: [Configurator]
    parameters:
      - {name: schema_id, in: path, required: true, type: integer}
      - {name: column_id, in: path, required: true, type: integer}
    responses:
      200: {description: Columna eliminada}
      404: {description: No existe}
    """
    if not ColumnService.delete(column_id):
        raise NotFoundError("Columna no encontrada")
    return jsonify({"message": "Deleted"})


# ---------- Records ----------

@bp.get("/schemas/<int:schema_id>/records")
def get_records(schema_id: int):
    """Lista paginada de records de un schema
    ---
    tags: [Configurator]
    parameters:
      - {name: schema_id, in: path, required: true, type: integer}
      - {name: page, in: query, type: integer}
      - {name: limit, in: query, type: integer}
    responses:
      200: {description: Records paginados}
      404: {description: Schema no encontrado}
    """
    if not SchemaService.get_by_id(schema_id):
        raise NotFoundError("Schema no encontrado")
    page, limit = pagination(default_limit=20)
    result = RecordService.get_by_schema(schema_id, page, limit)
    return jsonify(RecordListResponse(
        items=[_record_payload(r) for r in result["items"]],
        total=result["total"],
        page=result["page"],
        limit=result["limit"],
        pages=result["pages"],
    ).model_dump())


@bp.post("/schemas/<int:schema_id>/records")
def create_record(schema_id: int):
    """Crea un record dentro de un schema
    ---
    tags: [Configurator]
    parameters:
      - {name: schema_id, in: path, required: true, type: integer}
      - name: body
        in: body
        required: true
        schema:
          type: object
          properties:
            data: {type: object}
    responses:
      201: {description: Record creado}
      400: {description: Datos invalidos segun las columnas}
      404: {description: Schema no encontrado}
    """
    payload = parse_body(RecordCreate)
    record, msg = RecordService.create(schema_id, payload.data)
    if not record:
        raise AppError(msg, 400)
    return jsonify(_record_payload(record)), 201


@bp.get("/schemas/<int:schema_id>/records/<int:record_id>")
def get_record(schema_id: int, record_id: int):
    """Detalle de un record
    ---
    tags: [Configurator]
    parameters:
      - {name: schema_id, in: path, required: true, type: integer}
      - {name: record_id, in: path, required: true, type: integer}
    responses:
      200: {description: Record}
      404: {description: No existe}
    """
    record = RecordService.get_by_id(record_id)
    if not record:
        raise NotFoundError("Record no encontrado")
    return jsonify(_record_payload(record))


@bp.put("/schemas/<int:schema_id>/records/<int:record_id>")
def update_record(schema_id: int, record_id: int):
    """Actualiza un record
    ---
    tags: [Configurator]
    parameters:
      - {name: schema_id, in: path, required: true, type: integer}
      - {name: record_id, in: path, required: true, type: integer}
      - name: body
        in: body
        required: true
        schema:
          type: object
          properties:
            data: {type: object}
    responses:
      200: {description: Record actualizado}
      400: {description: Datos invalidos}
      404: {description: No existe}
    """
    payload = parse_body(RecordUpdate)
    record, msg = RecordService.update(record_id, payload.data)
    if not record:
        raise NotFoundError(msg or "Record no encontrado")
    return jsonify(_record_payload(record))


@bp.delete("/schemas/<int:schema_id>/records/<int:record_id>")
def delete_record(schema_id: int, record_id: int):
    """Elimina un record
    ---
    tags: [Configurator]
    parameters:
      - {name: schema_id, in: path, required: true, type: integer}
      - {name: record_id, in: path, required: true, type: integer}
    responses:
      200: {description: Record eliminado}
      404: {description: No existe}
    """
    if not RecordService.delete(record_id):
        raise NotFoundError("Record no encontrado")
    return jsonify({"message": "Deleted"})


@bp.post("/schemas/<int:schema_id>/records/search")
def search_records(schema_id: int):
    """Busca records por filtros sobre las columnas filterables
    ---
    tags: [Configurator]
    parameters:
      - {name: schema_id, in: path, required: true, type: integer}
      - name: body
        in: body
        required: true
        schema:
          type: object
          properties:
            filters: {type: object}
            page: {type: integer}
            limit: {type: integer}
    responses:
      200: {description: Resultados de la busqueda}
    """
    body = raw_body()
    filters = body.get("filters") or {}
    page = max(int(body.get("page", 1)), 1)
    limit = min(max(int(body.get("limit", 20)), 1), 200)
    result = RecordService.search(schema_id, filters, page, limit)
    return jsonify(RecordListResponse(
        items=[_record_payload(r) for r in result["items"]],
        total=result["total"],
        page=result["page"],
        limit=result["limit"],
        pages=result["pages"],
    ).model_dump())


# ---------- Stats ----------

@bp.get("/stats")
def get_stats():
    """Estadisticas globales del configurator
    ---
    tags: [Configurator]
    responses:
      200: {description: Estadisticas}
    """
    return jsonify(StatsService.get_global())


# ---------- Audits por entidad ----------

@bp.get("/audits/<entity_type>/<int:entity_id>")
def get_audits_by_entity(entity_type: str, entity_id: int):
    """Historial de auditoria de una entidad del configurator (schema/column/record)
    ---
    tags: [Configurator]
    parameters:
      - {name: entity_type, in: path, required: true, type: string}
      - {name: entity_id, in: path, required: true, type: integer}
    responses:
      200: {description: Eventos de auditoria}
    """
    return jsonify(AuditService.get_by_entity(entity_type, entity_id))
