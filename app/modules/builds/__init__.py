from app.modules.builds.controller import bp
from app.modules.builds import model  # noqa: F401  (registra el modelo en SQLAlchemy)

__all__ = ["bp", "model"]
