import pytest

from app import create_app
from app.core.db import db
from config import Config


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    DEFAULT_NAMESPACE = "prod"
    INGRESS_BASE_DOMAIN = "andreslobaton.top"
    INGRESS_CLASS = "traefik"
    CERT_MANAGER_CLUSTER_ISSUER = "letsencrypt-prod"
    CONTAINER_PORT = 8080
    SERVICE_PORT_RANGE_START = 3000
    SERVICE_PORT_RANGE_END = 3005  # rango corto para poder probar el agotamiento
    HPA_TARGET_CPU = 80
    # Sin PATs en tests: las integraciones (GitHub/Docker Hub) siempre fallan
    # con 503 y el create de apps no hace llamadas HTTP reales.
    GITHUB_PAT = ""
    DOCKER_HUB_TOKEN = ""


@pytest.fixture
def app():
    app = create_app(TestConfig)
    with app.app_context():
        # SQLite no enforce FK CASCADE por defecto. Lo activamos via un
        # listener de SQLAlchemy "connect" para que borrar una Application
        # cascadee a scoops/domains/app_events como en MariaDB.
        from sqlalchemy import event
        @event.listens_for(db.engine, "connect")
        def _fk_on(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()
        # Forzar al menos una conexion para que el listener se dispare.
        with db.engine.connect() as _conn:
            pass
        db.create_all()
        # Sin Jenkins real en tests: stub de create_job/delete_job. Asi
        # AppsService.create puede correr el step 5 sin pegarle a un
        # servidor inexistente. Si un test quiere verificar la creacion
        # del job, puede sobreescribir este mock con monkeypatch.
        from app.modules.integrations.jenkins import service as jenkins_svc
        jenkins_svc.JenkinsService.create_job = staticmethod(
            lambda slug, test_cmd, image_base, github_repo_url=None: True
        )
        jenkins_svc.JenkinsService.delete_job = staticmethod(
            lambda slug: True
        )
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
    }
