"""Tests del modulo apps: Application CRUD + lifecycle."""
import pytest


@pytest.fixture
def app_payload():
    return {"name": "Notas", "description": "Backend principal"}


class TestApplicationCreate:
    def test_create_assigns_slug(self, client, app_payload):
        r = client.post("/api/apps", json=app_payload)
        assert r.status_code == 201
        data = r.get_json()
        assert data["slug"] == "notas"
        assert data["name"] == "Notas"
        assert data["namespace"] == "notas"

    def test_create_duplicate_name_returns_409(self, client, app_payload):
        client.post("/api/apps", json=app_payload)
        r = client.post("/api/apps", json=app_payload)
        assert r.status_code == 409

    def test_create_strips_special_chars(self, client):
        r = client.post("/api/apps", json={"name": "Mi App #1"})
        assert r.status_code == 201
        assert r.get_json()["slug"] == "mi-app-1"

    def test_create_empty_name_returns_400(self, client):
        r = client.post("/api/apps", json={"name": ""})
        assert r.status_code in (400, 422)

    def test_create_invalid_docker_image_base_returns_400(self, client):
        r = client.post("/api/apps", json={
            "name": "Foo",
            "docker_image_base": "!!!@@@"
        })
        # Validacion Pydantic responde 422 (mismo patron que el resto del API).
        assert r.status_code in (400, 422)


class TestApplicationListGet:
    def test_list_pagination(self, client):
        for i in range(3):
            client.post("/api/apps", json={"name": f"App{i}"})
        r = client.get("/api/apps?page=1&limit=2")
        assert r.status_code == 200
        data = r.get_json()
        assert data["total"] == 3
        assert len(data["items"]) == 2

    def test_get_existing(self, client, app_payload):
        created = client.post("/api/apps", json=app_payload).get_json()
        r = client.get(f"/api/apps/{created['id']}")
        assert r.status_code == 200
        assert r.get_json()["slug"] == "notas"

    def test_get_404(self, client):
        r = client.get("/api/apps/99999")
        assert r.status_code == 404


class TestApplicationUpdate:
    def test_update_description(self, client, app_payload):
        created = client.post("/api/apps", json=app_payload).get_json()
        r = client.put(f"/api/apps/{created['id']}",
                       json={"description": "Nuevo texto"})
        assert r.status_code == 200
        assert r.get_json()["description"] == "Nuevo texto"

    def test_update_github_repo_url_manual_override(self, client, app_payload):
        created = client.post("/api/apps", json=app_payload).get_json()
        r = client.put(f"/api/apps/{created['id']}", json={
            "github_repo_url": "https://github.com/other-org/repo"
        })
        assert r.status_code == 200
        assert r.get_json()["github_repo_url"] == "https://github.com/other-org/repo"

    def test_update_docker_image_base_manual_override(self, client, app_payload):
        created = client.post("/api/apps", json=app_payload).get_json()
        r = client.put(f"/api/apps/{created['id']}", json={
            "docker_image_base": "custom/namespace/app"
        })
        assert r.status_code == 200
        assert r.get_json()["docker_image_base"] == "custom/namespace/app"


class TestApplicationDelete:
    def test_delete_hides_from_list(self, client, app_payload):
        created = client.post("/api/apps", json=app_payload).get_json()
        client.delete(f"/api/apps/{created['id']}")
        listed = client.get("/api/apps").get_json()["items"]
        assert all(a["id"] != created["id"] for a in listed)

    def test_delete_then_get_returns_404(self, client, app_payload):
        created = client.post("/api/apps", json=app_payload).get_json()
        client.delete(f"/api/apps/{created['id']}")
        r = client.get(f"/api/apps/{created['id']}")
        assert r.status_code == 404

class TestApplicationHardDeleteReuseSlug:
    """Hard delete libera name+slug para re-crear."""

    def test_can_recreate_app_with_same_name_after_delete(self, client, app_payload):
        r1 = client.post("/api/apps", json=app_payload).get_json()
        d = client.delete(f"/api/apps/{r1['id']}")
        assert d.status_code == 200
        # Mismo name deberia poder crearse de nuevo (ya no hay unique conflict).
        r2 = client.post("/api/apps", json=app_payload)
        assert r2.status_code == 201, r2.get_json()
        new = r2.get_json()
        assert new["slug"] == r1["slug"]
        assert new["name"] == r1["name"]


class TestApplicationCreateRollback:
    """Si una llamada externa (GitHub / K8s) o el INSERT falla,
    se hace rollback y la app NO queda en BD."""

    def test_github_failure_returns_error_and_no_app_created(
        self, client, app_payload, monkeypatch,
    ):
        """create_github_repo=True + GitHub.create_empty_repo falla:
        respuesta de error, ninguna app en BD, delete_repo no se llama
        (no llegamos a crearlo)."""
        from app.core.errors import AppError

        called = {"create": 0, "delete": 0}

        def fake_create(slug, private=False):
            called["create"] += 1
            raise AppError("boom", status_code=503)

        def fake_delete(slug):
            called["delete"] += 1
            return {"deleted": True, "name": slug}

        monkeypatch.setattr(
            "app.modules.integrations.github.service.GitHubService.create_empty_repo",
            staticmethod(fake_create),
        )
        monkeypatch.setattr(
            "app.modules.integrations.github.service.GitHubService.delete_repo",
            staticmethod(fake_delete),
        )

        r = client.post("/api/apps", json={**app_payload, "create_github_repo": True})
        assert r.status_code == 503
        body = r.get_json()
        assert "github" in body["error"].lower()
        assert body.get("details", {}).get("step") == "github_repo"

        # No se creo ninguna app y delete_repo no se llamo.
        from app.core.db import db
        from app.modules.apps.model import Application
        assert Application.query.count() == 0
        assert called == {"create": 1, "delete": 0}

    def test_k8s_failure_rolls_back_github_repo(
        self, client, app_payload, monkeypatch,
    ):
        """GitHub.create_empty_repo OK pero K8s.create_namespace falla:
        app no en BD, delete_repo SI se llamo para el rollback."""
        from kubernetes.client.exceptions import ApiException
        from types import SimpleNamespace

        class _FakeCore:
            def read_namespace(self, name):
                raise ApiException(status=404, reason="NotFound")

            def create_namespace(self, body):
                raise ApiException(status=500, reason="ServerError")

            def delete_namespace(self, name):
                # Verificamos que el rollback llega aca.
                _FakeCore.delete_calls.append(name)
                return {"name": name, "deleted": True}

        _FakeCore.delete_calls = []

        class _FakeClients:
            def __init__(self):
                self.core = _FakeCore()

            @staticmethod
            def serialize(obj):
                return obj

        from app.core.k8s import reset_clients
        reset_clients()
        monkeypatch.setattr(
            "app.core.k8s.get_clients", lambda: _FakeClients()
        )
        # El binding local en cluster.service ya fue importado; parchear ese.
        monkeypatch.setattr(
            "app.modules.cluster.service.get_clients",
            lambda: _FakeClients(), raising=False,
        )

        deleted = {"called": 0}

        def fake_create_repo(slug, private=False):
            return {
                "name": f"laurel_{slug}",
                "full_name": f"org/laurel_{slug}",
                "html_url": f"https://github.com/org/laurel_{slug}",
                "private": private,
            }

        def fake_delete_repo(slug):
            deleted["called"] += 1
            return {"deleted": True, "name": slug}

        monkeypatch.setattr(
            "app.modules.integrations.github.service.GitHubService.create_empty_repo",
            staticmethod(fake_create_repo),
        )
        monkeypatch.setattr(
            "app.modules.integrations.github.service.GitHubService.delete_repo",
            staticmethod(fake_delete_repo),
        )

        r = client.post("/api/apps", json={**app_payload, "create_github_repo": True})
        assert r.status_code == 502
        body = r.get_json()
        assert "namespace" in body["error"].lower()
        assert body.get("details", {}).get("step") == "k8s_namespace"

        # No app en BD, y el rollback SI llamo a delete_repo.
        from app.core.db import db
        from app.modules.apps.model import Application
        assert Application.query.count() == 0
        assert deleted["called"] == 1
        reset_clients()

    def test_duplicate_name_rolls_back_namespace(
        self, client, app_payload, monkeypatch,
    ):
        """Insertar la 2da app con el mismo name (slug identico) choca
        por el UNIQUE: debe borrar el namespace que se acababa de crear."""
        from kubernetes.client.exceptions import ApiException

        # Mock K8s para que namespace_exists=False y create_namespace
        # registre los calls. delete_namespace tambien se trackea.
        class _FakeCore:
            create_calls = []
            delete_calls = []

            def read_namespace(self, name):
                raise ApiException(status=404, reason="NotFound")

            def create_namespace(self, body):
                _FakeCore.create_calls.append(body["metadata"]["name"])
                return {"metadata": {"name": body["metadata"]["name"]}}

            def delete_namespace(self, name):
                _FakeCore.delete_calls.append(name)
                return {"name": name, "deleted": True}

        class _FakeClients:
            def __init__(self):
                self.core = _FakeCore()

            @staticmethod
            def serialize(obj):
                return obj

        from app.core.k8s import reset_clients
        reset_clients()
        monkeypatch.setattr(
            "app.core.k8s.get_clients", lambda: _FakeClients()
        )
        monkeypatch.setattr(
            "app.modules.cluster.service.get_clients",
            lambda: _FakeClients(), raising=False,
        )

        # 1ra app: OK.
        r1 = client.post("/api/apps", json=app_payload)
        assert r1.status_code == 201
        assert len(_FakeCore.create_calls) == 1

        # 2da app: mismo name -> IntegrityError -> 409.
        r2 = client.post("/api/apps", json=app_payload)
        assert r2.status_code == 409

        # La 2da intento crear su namespace (create_calls=2) pero
        # tambien lo borro (delete_calls=1 con el slug correcto).
        assert len(_FakeCore.create_calls) == 2
        assert _FakeCore.delete_calls == [_FakeCore.create_calls[1]]
        reset_clients()


class TestAppCreateJenkinsJob:
    """Al crear una app, el backend crea el job laurel_<slug> en Jenkins."""

    def test_create_also_creates_jenkins_job(self, client, app_payload, monkeypatch):
        from app.modules.integrations.jenkins.service import JenkinsService
        called = {"args": None}

        def fake_create(slug, test_cmd, image_base, github_repo_url=None):
            called["args"] = {
                "slug": slug, "test_cmd": test_cmd,
                "image_base": image_base, "github_repo_url": github_repo_url,
            }
            return True

        monkeypatch.setattr(JenkinsService, "create_job", staticmethod(fake_create))

        r = client.post("/api/apps", json=app_payload)
        assert r.status_code == 201, r.get_json()

        assert called["args"] is not None, "create_job no fue llamado"
        assert called["args"]["slug"] == "notas"
        assert "no tests" in called["args"]["test_cmd"]
        assert called["args"]["image_base"]  # default generado por ContainerRegistryService

        # Y el evento 'jenkins_job' aparece en el timeline
        app_id = r.get_json()["id"]
        events_r = client.get(f"/api/apps/{app_id}/events")
        events = events_r.get_json()["items"]
        jenkins_events = [e for e in events if e["event"] == "jenkins_job"]
        assert len(jenkins_events) == 1
        assert jenkins_events[0]["status"] == "ok"

    def test_jenkins_failure_rolls_back_app(self, client, app_payload, monkeypatch):
        """Si Jenkins falla al crear el job, la app tampoco queda en la BD."""
        from app.core.errors import AppError
        from app.modules.integrations.jenkins.service import JenkinsService

        def fake_create_boom(slug, test_cmd, image_base, github_repo_url=None):
            raise AppError("Jenkins unavailable", status_code=503)

        monkeypatch.setattr(JenkinsService, "create_job", staticmethod(fake_create_boom))

        r = client.post("/api/apps", json=app_payload)
        assert r.status_code == 503, r.get_json()
        assert r.get_json().get("details", {}).get("step") == "jenkins_job"

        # La app NO debe estar en la BD
        from app.modules.apps.model import Application
        from app.core.db import db
        with client.application.app_context():
            found = Application.query.filter_by(slug="notas").first()
            assert found is None, "App quedo en BD pese a fallo de Jenkins"


class TestAppHardDeleteJenkins:
    """Al hacer hard-delete de la app, tambien se borra el job de Jenkins."""

    def test_hard_delete_calls_jenkins_delete_job(self, app, client, monkeypatch):
        from app.modules.integrations.jenkins.service import JenkinsService

        # Seed: una app + su app_build para que delete recorra el camino completo
        from app.modules.apps.model import Application
        from app.modules.builds.model import AppBuild
        from app.core.db import db
        with app.app_context():
            app_obj = Application(
                slug="demo-del",
                name="App a borrar",
                docker_image_base="aflobaton/laurel_demo-del",
                current_version="1.0.0",
            )
            db.session.add(app_obj)
            db.session.commit()
            app_id = app_obj.id

            # Mock: el job existe, el borrado devuelve True
            monkeypatch.setattr(JenkinsService, "job_exists",
                                staticmethod(lambda slug: True))
            deleted = {"called": 0, "slug": None}
            def fake_delete(slug):
                deleted["called"] += 1
                deleted["slug"] = slug
                return True
            monkeypatch.setattr(JenkinsService, "delete_job",
                                staticmethod(fake_delete))

        r = client.delete(f"/api/apps/{app_id}")
        assert r.status_code == 200, r.get_json()

        assert deleted["called"] == 1
        assert deleted["slug"] == "demo-del"

        # La app esta borrada de la BD
        with app.app_context():
            assert Application.query.get(app_id) is None

    def test_hard_delete_succeeds_when_jenkins_unreachable(self, app, client, monkeypatch):
        """Si Jenkins no responde, la app igual se borra (best-effort)."""
        from app.modules.integrations.jenkins.service import JenkinsService
        from app.modules.apps.model import Application
        from app.core.db import db

        with app.app_context():
            app_obj = Application(
                slug="demo-del2",
                name="App a borrar 2",
                docker_image_base="aflobaton/laurel_demo-del2",
            )
            db.session.add(app_obj)
            db.session.commit()
            app_id = app_obj.id

        # Jenkins job_exists tira excepcion (cluster caido, token invalido, etc)
        def boom(slug):
            raise RuntimeError("Jenkins unreachable")
        monkeypatch.setattr(JenkinsService, "job_exists", staticmethod(boom))

        r = client.delete(f"/api/apps/{app_id}")
        assert r.status_code == 200, r.get_json()

        # La app igual se borro
        with app.app_context():
            assert Application.query.get(app_id) is None
