"""Utilidades compartidas por los controllers."""
from flask import current_app, request
from pydantic import BaseModel

from app.core.errors import AppError

_TRUTHY = {"true", "1", "yes", "si"}
_FALSY = {"false", "0", "no"}


def parse_body(model: type[BaseModel]) -> BaseModel:
    """Valida el JSON del request contra un modelo Pydantic.

    Los ValidationError los captura el handler global y salen como 422.
    """
    data = request.get_json(silent=True)
    if data is None:
        raise AppError("Se esperaba un cuerpo JSON valido", 400)
    return model.model_validate(data)


def raw_body() -> dict:
    """Cuerpo JSON sin validar, para endpoints que reciben manifiestos crudos."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise AppError("Se esperaba un manifiesto JSON", 400)
    return data


def pagination(default_limit: int = 20, max_limit: int = 200) -> tuple[int, int]:
    page = request.args.get("page", 1, type=int) or 1
    limit = request.args.get("limit", default_limit, type=int) or default_limit
    return max(page, 1), min(max(limit, 1), max_limit)


def bool_arg(name: str) -> bool | None:
    value = request.args.get(name)
    if value is None:
        return None
    value = value.lower()
    if value in _TRUTHY:
        return True
    if value in _FALSY:
        return False
    raise AppError(f"El parametro '{name}' debe ser booleano", 400)


def namespace_arg() -> str:
    """Namespace de la query string, con el default del proyecto ('prod')."""
    return request.args.get("namespace") or current_app.config["DEFAULT_NAMESPACE"]
