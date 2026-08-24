"""Tests del modulo integracion con Jenkins (no tocan Jenkins real)."""
from unittest.mock import Mock

import pytest
import requests

from app.core.errors import AppError
from app.modules.integrations.jenkins import service as jenkins_service
from app.modules.integrations.jenkins.service import JenkinsService


class TestGetBuildStatusQueue:
    """Verifica que get_build_status maneja URLs de queue correctamente.

    El trigger puede devolver /queue/item/<id>/ cuando el build esta
    encolado. Esa URL es fragil: una vez Jenkins la promueve a build
    real, el item de queue se CONSUME y la URL devuelve 404. El polling
    debe resolver la URL de queue a una URL de build de tres formas:
      1) Queue API 200 + executable.url  -> seguir executable URL
      2) Queue API 200 sin executable     -> seguir pendiente
      3) Queue API 404 (item consumido)    -> fallback via job lastBuild
    """

    def test_queue_with_executable_resolves_to_build(self, app, monkeypatch):
        """Si la queue API ya tiene executable.url, el polling la sigue."""
        app.config["JENKINS_URL"] = "http://jenkins:8080"
        calls = []

        def _get(url, timeout=None, **kwargs):
            calls.append(url)
            if url == "http://jenkins:8080/queue/item/77/api/json":
                resp = Mock(status_code=200)
                resp.json.return_value = {
                    "executable": {
                        "url": "http://jenkins:8080/job/laurel_notas/42/",
                        "number": 42,
                    }
                }
                return resp
            if url == "http://jenkins:8080/job/laurel_notas/42/api/json":
                resp = Mock(status_code=200)
                resp.json.return_value = {
                    "number": 42,
                    "building": True,
                    "result": None,
                    "timestamp": 1700000000000,
                }
                return resp
            raise AssertionError(f"unexpected URL: {url}")

        monkeypatch.setattr(jenkins_service.requests, "get", _get)
        with app.app_context():
            status = JenkinsService.get_build_status(
                slug="notas",
                build_url="http://jenkins:8080/queue/item/77",
            )
        assert status["status"] == "running"
        assert status["number"] == 42
        assert "queue/item/77" in calls[0]
        assert "/job/laurel_notas/42" in calls[1]

    def test_queue_still_queued_returns_pending(self, app, monkeypatch):
        """Si la queue API no tiene executable, el build sigue pendiente."""
        app.config["JENKINS_URL"] = "http://jenkins:8080"

        def _get(url, timeout=None, **kwargs):
            if url != "http://jenkins:8080/queue/item/88/api/json":
                raise AssertionError(f"unexpected URL: {url}")
            resp = Mock(status_code=200)
            resp.json.return_value = {"executable": None}
            return resp

        monkeypatch.setattr(jenkins_service.requests, "get", _get)
        with app.app_context():
            status = JenkinsService.get_build_status(
                slug="notas",
                build_url="http://jenkins:8080/queue/item/88",
            )
        assert status["status"] == "pending"
        assert status["number"] is None

    def test_queue_item_gone_falls_back_to_job_lastbuild(
        self, app, monkeypatch,
    ):
        """Si la queue URL dio 404 (item consumido), usa job lastBuild."""
        app.config["JENKINS_URL"] = "http://jenkins:8080"
        calls = []

        def _get(url, timeout=None, **kwargs):
            calls.append(url)
            if url == "http://jenkins:8080/queue/item/99/api/json":
                return Mock(status_code=404, text="Not Found")
            if url == "http://jenkins:8080/job/laurel_notas/api/json":
                resp = Mock(status_code=200)
                resp.json.return_value = {
                    "lastBuild": {
                        "url": "http://jenkins:8080/job/laurel_notas/55/",
                        "number": 55,
                    }
                }
                return resp
            if url == "http://jenkins:8080/job/laurel_notas/55/api/json":
                resp = Mock(status_code=200)
                resp.json.return_value = {
                    "number": 55,
                    "building": False,
                    "result": "SUCCESS",
                    "timestamp": 1700000000000,
                }
                return resp
            raise AssertionError(f"unexpected URL: {url}")

        monkeypatch.setattr(jenkins_service.requests, "get", _get)
        with app.app_context():
            status = JenkinsService.get_build_status(
                slug="notas",
                build_url="http://jenkins:8080/queue/item/99",
            )
        assert status["status"] == "success"
        assert status["number"] == 55
        # Llamo queue API, luego job API, luego build API (en ese orden).
        assert len(calls) == 3
        assert "queue/item/99" in calls[0]
        assert "/job/laurel_notas/api/json" in calls[1]
        assert "/job/laurel_notas/55/api/json" in calls[2]

    def test_build_url_without_queue_goes_direct(self, app, monkeypatch):
        """URL directa de build (/job/<job>/<n>/) no consulta la queue."""
        app.config["JENKINS_URL"] = "http://jenkins:8080"

        def _get(url, timeout=None, **kwargs):
            assert "queue/item" not in url
            resp = Mock(status_code=200)
            resp.json.return_value = {
                "number": 10,
                "building": False,
                "result": "SUCCESS",
                "timestamp": 1700000000000,
            }
            return resp

        monkeypatch.setattr(jenkins_service.requests, "get", _get)
        with app.app_context():
            status = JenkinsService.get_build_status(
                slug="notas",
                build_url="http://jenkins:8080/job/laurel_notas/10",
            )
        assert status["status"] == "success"
        assert status["number"] == 10


class TestBuildToken:
    def test_token_not_configured_raises_503(self, app, monkeypatch):
        def _no_secret(*_a, **_k):
            raise AppError("no existe", status_code=404)
        monkeypatch.setattr(
            "app.modules.system.service.SystemSecretService.get_content", _no_secret
        )
        monkeypatch.setattr(jenkins_service, "_validate_slug", lambda _s: None)
        with app.app_context():
            with pytest.raises(AppError) as exc:
                JenkinsService.trigger_build("notas", "1.2.4")
        assert exc.value.status_code == 503
        assert "Jenkins token not configured" in exc.value.message

    def test_empty_token_raises_503(self, app, monkeypatch):
        monkeypatch.setattr(
            "app.modules.system.service.SystemSecretService.get_content",
            lambda *a, **k: {"content": "  "},
        )
        monkeypatch.setattr(jenkins_service, "_validate_slug", lambda _s: None)
        with app.app_context():
            with pytest.raises(AppError) as exc:
                JenkinsService.trigger_build("notas", "1.2.4")
        assert exc.value.status_code == 503


class TestTriggerBuild:
    def test_success_posts_build_with_token_and_params(self, app, monkeypatch):
        app.config["JENKINS_URL"] = "http://jenkins:8080"
        posted = {}

        def _post(url, params=None, data=None, timeout=None, **kwargs):
            posted.update(url=url, params=params, data=data, timeout=timeout, headers=kwargs.get("headers"))
            resp = Mock(status_code=201, text="")
            # Jenkins responde 201 con header `Location: /job/<job>/<n>/`.
            resp.headers = {"Location": "http://jenkins:8080/job/laurel_notas/42/"}
            return resp

        monkeypatch.setattr(jenkins_service.requests, "post", _post)
        monkeypatch.setattr(jenkins_service, "_get_build_token", lambda: "tok123")
        with app.app_context():
            result = JenkinsService.trigger_build("notas", "1.2.4")

        assert result == {
            "job": "laurel_notas",
            "number": 42,
            "url": "http://jenkins:8080/job/laurel_notas/42",
        }
        assert posted["url"] == "http://jenkins:8080/job/laurel_notas/buildWithParameters"
        assert posted["params"] == {"token": "tok123"}
        assert posted["timeout"] == jenkins_service.JENKINS_TIMEOUT
        # El IMAGE se envia SIN registry ni tag (el job agrega docker.io/ y :${TAG}).
        # Sin app en BD ni system secret -> password params "placeholder";
        # el user se defaulta a "aflobaton" (igual que docker/service._get_user()).
        # Sin TEST_CMD/SLUG/GITHUB_PAT: el job clona publico (repos publicos)
        # y autodetecta el framework en Clone+Test.
        assert posted["data"] == {
            "TAG": "1.2.4",
            "REPO": "laurel-applications/laurel_notas",
            "IMAGE": "laurel_notas",
            "DOCKERHUB_USER": "aflobaton",
            "DOCKERHUB_PASSWORD": "placeholder",
        }

    @pytest.mark.parametrize("status,expected_status,expected_msg", [
        (401, 502, "Jenkins authentication failed"),
        (403, 502, "Jenkins authentication failed"),
        (404, 404, "Jenkins job 'laurel_notas' not found"),
    ])
    def test_error_mapping(self, app, monkeypatch, status, expected_status, expected_msg):
        monkeypatch.setattr(jenkins_service.requests, "post",
                            lambda *a, **k: Mock(status_code=status, text="nope"))
        monkeypatch.setattr(jenkins_service, "_get_build_token", lambda: "tok")
        with app.app_context():
            with pytest.raises(AppError) as exc:
                JenkinsService.trigger_build("notas", "1.2.4")
        assert exc.value.status_code == expected_status
        assert expected_msg in exc.value.message

    def test_timeout_raises_504(self, app, monkeypatch):
        def _boom(*_a, **_k):
            raise requests.RequestException("connect timeout")
        monkeypatch.setattr(jenkins_service.requests, "post", _boom)
        monkeypatch.setattr(jenkins_service, "_get_build_token", lambda: "tok")
        with app.app_context():
            with pytest.raises(AppError) as exc:
                JenkinsService.trigger_build("notas", "1.2.4")
        assert exc.value.status_code == 504
        assert exc.value.message == "Jenkins timeout"


class TestJobExists:
    def test_returns_true_on_200(self, app, monkeypatch):
        monkeypatch.setattr(jenkins_service.requests, "get",
                            lambda *a, **k: Mock(status_code=200))
        with app.app_context():
            assert JenkinsService.job_exists("notas") is True

    def test_returns_false_on_404(self, app, monkeypatch):
        monkeypatch.setattr(jenkins_service.requests, "get",
                            lambda *a, **k: Mock(status_code=404))
        with app.app_context():
            assert JenkinsService.job_exists("notas") is False

    def test_returns_false_on_request_error(self, app, monkeypatch):
        def _boom(*_a, **_k):
            raise requests.RequestException("conn refused")
        monkeypatch.setattr(jenkins_service.requests, "get", _boom)
        with app.app_context():
            assert JenkinsService.job_exists("notas") is False