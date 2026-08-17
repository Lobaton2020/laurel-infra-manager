"""Tests del modulo integracion con Jenkins (no tocan Jenkins real)."""
from unittest.mock import Mock

import pytest
import requests

from app.core.errors import AppError
from app.modules.integrations.jenkins import service as jenkins_service
from app.modules.integrations.jenkins.service import JenkinsService


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

        def _post(url, params=None, data=None, timeout=None):
            posted.update(url=url, params=params, data=data, timeout=timeout)
            resp = Mock(status_code=201)
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
        assert posted["data"] == {
            "SLUG": "notas",
            "TAG": "1.2.4",
            "REPO": "laurel-applications/laurel_notas",
            "IMAGE": "aflobaton/laurel_notas:1.2.4",
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