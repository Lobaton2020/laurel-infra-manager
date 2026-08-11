import logging

from flasgger import Swagger
from flask import Flask
from flask_cors import CORS

from app.core.db import db
from app.core.errors import register_error_handlers
from config import Config

SWAGGER_TEMPLATE = {
    "info": {
        "title": "Laurel Infra Manager API",
        "description": (
            "Wrapper del API de Kubernetes sobre el cluster K3s 'homelob'. "
            "Gestiona un catalogo de componentes desplegables y los materializa "
            "como Deployments, Services, Ingresses y HPAs."
        ),
        "version": "1.0.0",
    },
    "basePath": "/",
    "schemes": ["http"],
    "tags": [
        {"name": "Health", "description": "Estado del API y del cluster"},
        {"name": "Components", "description": "Catalogo de componentes y su despliegue"},
        {"name": "Cluster", "description": "Informacion general del cluster"},
        {"name": "Pods", "description": "Pods y logs"},
        {"name": "Deployments", "description": "Deployments, scale y restart"},
        {"name": "Services", "description": "Services"},
        {"name": "Ingresses", "description": "Ingresses (Traefik)"},
        {"name": "Audits", "description": "Historial de cambios"},
    ],
}


def create_app(config_class=Config) -> Flask:
    """Application factory."""
    app = Flask(__name__)
    app.config.from_object(config_class)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    CORS(app)
    db.init_app(app)

    # Importar los modelos antes de create_all para que se registren en el metadata.
    from app.modules.audits.model import Audit  # noqa: F401
    from app.modules.scoops.model import Scoop  # noqa: F401

    with app.app_context():
        db.create_all()

    Swagger(app, template=SWAGGER_TEMPLATE)

    # Un blueprint por modulo, mas el de health (infraestructura, vive en core).
    from app.core.health import bp as health_bp
    from app.modules.audits import bp as audits_bp
    from app.modules.cluster import bp as cluster_bp
    from app.modules.scoops import bp as scoops_bp

    for blueprint in (health_bp, scoops_bp, cluster_bp, audits_bp):
        app.register_blueprint(blueprint)

    register_error_handlers(app)

    return app
