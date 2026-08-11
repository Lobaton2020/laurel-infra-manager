import pytest

from app import create_app
from app.core.db import db
from config import Config


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    DEFAULT_NAMESPACE = "prod"
    INGRESS_BASE_DOMAIN = "andrelobaton.top"
    INGRESS_CLASS = "traefik"
    CONTAINER_PORT = 8080
    SERVICE_PORT_RANGE_START = 3000
    SERVICE_PORT_RANGE_END = 3005  # rango corto para poder probar el agotamiento
    HPA_TARGET_CPU = 80


@pytest.fixture
def app():
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def scoop_payload():
    return {
        "name": "portafolio",
        "application": "portafolio-web",
        "type": "api",
        "version": "1.4.2",
        "url_registry": "ghcr.io/lobaton/portafolio:latest",
        "requested_vcpu": "100m",
        "requested_memory_value": 128,
        "requested_memory_unit": "M",
        "limit_vcpu": "500m",
        "limit_memory_value": 512,
        "limit_memory_unit": "M",
        "min_replicas": 1,
        "max_replicas": 3,
        "container_port": 8080,
        "health_path": "/health",
    }
