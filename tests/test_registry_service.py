"""Tests del servicio de registro de contenedores (Docker Hub)."""
from unittest.mock import Mock, patch

import pytest

from app.core.errors import AppError
from app.modules.integrations.docker import service as docker_service
from app.modules.integrations.docker.service import ContainerRegistryService


class TestContainerRegistry:
    def test_validate_image_ref_accepts_dockerhub(self):
        assert ContainerRegistryService.validate_image_ref(
            "aflobaton/laurel_app:1.2.3"
        )
        assert ContainerRegistryService.validate_image_ref(
            "docker.io/aflobaton/laurel_app:1.2.3"
        )

    def test_validate_image_ref_rejects_no_tag(self):
        assert not ContainerRegistryService.validate_image_ref(
            "aflobaton/laurel_app"
        )

    def test_validate_image_base_accepts_short_and_full(self):
        assert ContainerRegistryService.validate_image_base(
            "aflobaton/laurel_app"
        )
        assert ContainerRegistryService.validate_image_base(
            "docker.io/aflobaton/laurel_app"
        )
        assert not ContainerRegistryService.validate_image_base("")

    def test_suggested_base_uses_dockerhub_user(self, app):
        app.config["DOCKERHUB_USER"] = "aflobaton"
        with app.app_context():
            base = ContainerRegistryService.suggested_base("miapp")
        assert base == "docker.io/aflobaton/laurel_miapp"


class TestLogin:
    def test_login_caches_token(self, app, monkeypatch):
        app.config["DOCKERHUB_USER"] = "aflobaton"
        app.config["DOCKERHUB_PASSWORD"] = "secret"
        with app.app_context():
            # Reset del cache para que el test sea determinista.
            docker_service._JWT_CACHE["token"] = None
            docker_service._JWT_CACHE["expires_at"] = 0

            calls = {"n": 0}

            def _fake_urlopen(req, timeout=10):
                calls["n"] += 1
                assert b"aflobaton" in req.data
                assert b"secret" in req.data
                return Mock(
                    __enter__=lambda s: s,
                    __exit__=lambda *a: None,
                    read=lambda: b'{"token": "jwt-abc"}',
                )

            with patch("urllib.request.urlopen", _fake_urlopen):
                t1 = docker_service._login()
                t2 = docker_service._login()
            assert t1 == "jwt-abc"
            assert t2 == "jwt-abc"
            # Segundo login NO pega a la API: usa el cache.
            assert calls["n"] == 1

    def test_login_missing_creds_raises_503(self, app, monkeypatch):
        app.config["DOCKERHUB_USER"] = ""
        app.config["DOCKERHUB_PASSWORD"] = ""
        with app.app_context():
            # Reset del cache: otro test puede haber dejado un JWT valido.
            docker_service._JWT_CACHE["token"] = None
            docker_service._JWT_CACHE["expires_at"] = 0
            with pytest.raises(AppError) as exc:
                docker_service._login()
        assert exc.value.status_code == 503


class TestCreateRepo:
    def test_create_repo_posts_to_v2(self, app, monkeypatch):
        app.config["DOCKERHUB_USER"] = "aflobaton"
        app.config["DOCKERHUB_PASSWORD"] = "secret"
        sent = {}

        def _fake_urlopen(req, timeout=10):
            sent["url"] = req.full_url
            sent["body"] = req.data
            sent["headers"] = req.headers
            return Mock(
                __enter__=lambda s: s,
                __exit__=lambda *a: None,
                read=lambda: b'{"name": "laurel_demo", "namespace": "aflobaton", "user": "aflobaton"}',
            )

        with app.app_context():
            docker_service._JWT_CACHE["token"] = "jwt-abc"
            docker_service._JWT_CACHE["expires_at"] = 10**12
            with patch("urllib.request.urlopen", _fake_urlopen):
                result = ContainerRegistryService.create_repo("demo")

        assert result == {
            "name": "laurel_demo",
            "namespace": "aflobaton",
            "user": "aflobaton",
            "existed": False,
        }
        assert sent["url"] == "https://hub.docker.com/v2/repositories/"
        assert "JWT jwt-abc" in sent["headers"]["Authorization"]
        import json
        body = json.loads(sent["body"])
        assert body["name"] == "laurel_demo"
        assert body["namespace"] == "aflobaton"
        assert body["is_private"] is False

    def test_create_repo_409_is_idempotent(self, app, monkeypatch):
        app.config["DOCKERHUB_USER"] = "aflobaton"
        app.config["DOCKERHUB_PASSWORD"] = "secret"

        def _boom(req, timeout=10):
            err = Mock(code=409)
            err.read = lambda: b'"name already exists"'
            raise __import__("urllib.error").error.HTTPError(
                req.full_url, 409, "Conflict", None, None
            )

        with app.app_context():
            docker_service._JWT_CACHE["token"] = "jwt-abc"
            docker_service._JWT_CACHE["expires_at"] = 10**12
            with patch("urllib.request.urlopen", _boom):
                result = ContainerRegistryService.create_repo("demo")

        assert result["existed"] is True
        assert result["name"] == "laurel_demo"

    def test_create_repo_error_raises_502(self, app, monkeypatch):
        app.config["DOCKERHUB_USER"] = "aflobaton"
        app.config["DOCKERHUB_PASSWORD"] = "secret"

        def _boom(req, timeout=10):
            raise __import__("urllib.error").error.HTTPError(
                req.full_url, 500, "Internal", None, None
            )

        with app.app_context():
            docker_service._JWT_CACHE["token"] = "jwt-abc"
            docker_service._JWT_CACHE["expires_at"] = 10**12
            with pytest.raises(AppError) as exc:
                with patch("urllib.request.urlopen", _boom):
                    ContainerRegistryService.create_repo("demo")
        assert exc.value.status_code == 502


class TestDeleteRepo:
    def test_delete_repo_404_returns_not_existed(self, app, monkeypatch):
        app.config["DOCKERHUB_USER"] = "aflobaton"
        app.config["DOCKERHUB_PASSWORD"] = "secret"

        def _boom(req, timeout=10):
            raise __import__("urllib.error").error.HTTPError(
                req.full_url, 404, "Not Found", None, None
            )

        with app.app_context():
            docker_service._JWT_CACHE["token"] = "jwt-abc"
            docker_service._JWT_CACHE["expires_at"] = 10**12
            with patch("urllib.request.urlopen", _boom):
                result = ContainerRegistryService.delete_repo("demo")

        assert result == {
            "deleted": False,
            "existed": False,
            "name": "laurel_demo",
            "namespace": "aflobaton",
        }
