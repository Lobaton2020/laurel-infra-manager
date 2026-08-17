import logging

from flasgger import Swagger
from flask import Flask, request
from flask_cors import CORS

from app.core.auth import authenticate_request
from app.core.db import db
from app.core.errors import AppError, register_error_handlers
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
        {"name": "ConfigStore", "description": "ConfigMaps y Secrets de aplicacion"},
        {"name": "Apps", "description": "Applications de primer nivel (namespace dedicado)"},
        {"name": "Workspaces", "description": "Workspaces de primer nivel: agrupamiento logico de Applications"},
        {"name": "Domains", "description": "Subdominios public (uno por Scoop)"},
        {"name": "DomainPool", "description": "Catalogo de dominios de segundo nivel propios"},
        {"name": "System", "description": "Secretos del backend y bootstrap"},
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

    db.init_app(app)

    # Importar los modelos antes de create_all para que se registren en el metadata.
    from app.modules.audits.model import Audit  # noqa: F401
    from app.modules.configurator.records.model import Record  # noqa: F401
    from app.modules.configurator.schemas.model import Column, Schema  # noqa: F401
    from app.modules.users.model import User  # noqa: F401
    from app.modules.apps.model import Application  # noqa: F401
    from app.modules.workspaces.model import Workspace  # noqa: F401
    from app.modules.scoops.model import Scoop  # noqa: F401
    from app.modules.domains.model import Domain  # noqa: F401
    from app.modules.domain_pool.model import DomainPool  # noqa: F401

    with app.app_context():
        db.create_all()
        # Seed del configurator: idempotente, solo llena la tabla si esta vacia.
        from app.modules.configurator.seed import seed_configurator
        seed_configurator()

    Swagger(app, template=SWAGGER_TEMPLATE)

    # Un blueprint por modulo, mas el de health (infraestructura, vive en core).
    from app.core.health import bp as health_bp
    from app.modules.auth import bp as auth_bp
    from app.modules.audits import bp as audits_bp
    from app.modules.cluster import bp as cluster_bp
    from app.modules.configurator import bp as configurator_bp
    from app.modules.configstore import bp as configstore_bp
    from app.modules.apps import bp as apps_bp
    from app.modules.workspaces import bp as workspaces_bp
    from app.modules.domains import bp as domains_bp
    from app.modules.domain_pool import bp as domain_pool_bp
    from app.modules.scoops import bp as scoops_bp
    from app.modules.system import bp as system_bp
    from app.modules.webhooks import bp as webhooks_bp

    # CORS restringido a los origins del front; mandamos Bearer en header
    # (no usamos cookies), por eso no hace falta supports_credentials=True.
    origins = app.config.get("AUTH_ALLOWED_ORIGINS") or ["*"]
    CORS(app, resources={r"/api/*": {"origins": origins}}, supports_credentials=False)

    app.before_request(authenticate_request)

    for blueprint in (health_bp, auth_bp, audits_bp, cluster_bp, configurator_bp, configstore_bp, apps_bp, workspaces_bp, domains_bp, domain_pool_bp, scoops_bp, system_bp, webhooks_bp):
        app.register_blueprint(blueprint)

    register_error_handlers(app)

    return app
