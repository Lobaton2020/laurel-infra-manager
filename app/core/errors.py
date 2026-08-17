"""Errores de aplicacion y traduccion de errores del API de Kubernetes."""
import json
import logging

from flask import jsonify
from kubernetes.client.exceptions import ApiException
from pydantic import ValidationError
from werkzeug.exceptions import HTTPException

logger = logging.getLogger(__name__)


class AppError(Exception):
    """Error de negocio con codigo HTTP explicito."""

    def __init__(self, message: str, status_code: int = 400, details=None, reason: str | None = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.details = details
        self.reason = reason

    def to_dict(self) -> dict:
        payload = {"error": self.message}
        if self.details:
            payload["details"] = self.details
        if self.reason:
            payload["reason"] = self.reason
        return payload


class NotFoundError(AppError):
    def __init__(self, message: str = "Recurso no encontrado", details=None):
        super().__init__(message, 404, details)


class ConflictError(AppError):
    def __init__(self, message: str = "Conflicto con el estado actual", details=None):
        super().__init__(message, 409, details)


class ClusterError(AppError):
    """Fallo hablando con el API de Kubernetes."""

    def __init__(self, message: str, status_code: int = 502, details=None):
        super().__init__(message, status_code, details)


def _k8s_reason(exc: ApiException) -> str:
    """Extrae el mensaje legible del cuerpo JSON que devuelve el API server."""
    try:
        return json.loads(exc.body).get("message", exc.reason)
    except (ValueError, TypeError, AttributeError):
        return exc.reason or "Error desconocido del cluster"


def register_error_handlers(app):
    @app.errorhandler(AppError)
    def handle_app_error(exc: AppError):
        return jsonify(exc.to_dict()), exc.status_code

    @app.errorhandler(ValidationError)
    def handle_validation_error(exc: ValidationError):
        return jsonify({
            "error": "Datos invalidos",
            "details": [
                {"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]}
                for e in exc.errors()
            ],
        }), 422

    @app.errorhandler(ApiException)
    def handle_k8s_error(exc: ApiException):
        message = _k8s_reason(exc)
        # 404/409/403 del cluster son significativos para el frontend, se propagan tal cual.
        if exc.status in (403, 404, 409, 422):
            return jsonify({"error": message, "source": "kubernetes"}), exc.status
        logger.exception("Error del API de Kubernetes")
        return jsonify({"error": message, "source": "kubernetes"}), 502

    @app.errorhandler(404)
    def handle_404(_):
        return jsonify({"error": "Endpoint no encontrado"}), 404

    @app.errorhandler(405)
    def handle_405(_):
        return jsonify({"error": "Metodo no permitido"}), 405

    @app.errorhandler(Exception)
    def handle_unexpected(exc: Exception):
        # Werkzeug HTTPException (abort(410), abort(404), etc.) ya trae su
        # codigo y mensaje: lo dejamos pasar al handler por defecto.
        if isinstance(exc, HTTPException):
            return exc
        logger.exception("Error no controlado")
        return jsonify({"error": "Error interno del servidor"}), 500
