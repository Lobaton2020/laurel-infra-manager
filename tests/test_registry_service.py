"""Tests del servicio de registro de contenedores (GHCR)."""
import pytest

from app.modules.integrations.docker.service import ContainerRegistryService


class TestContainerRegistry:
    def test_validate_image_ref_accepts_ghcr(self):
        assert ContainerRegistryService.validate_image_ref(
            "ghcr.io/laurel-applications/laurel_app:1.2.3"
        )

    def test_validate_image_ref_rejects_no_tag(self):
        assert not ContainerRegistryService.validate_image_ref(
            "ghcr.io/laurel-applications/laurel_app"
        )

    def test_validate_image_base_accepts_short_and_full(self):
        assert ContainerRegistryService.validate_image_base(
            "aflobaton/laurel_app"
        )
        assert ContainerRegistryService.validate_image_base(
            "ghcr.io/laurel-applications/laurel_app"
        )
        assert not ContainerRegistryService.validate_image_base("")

    def test_suggested_base_uses_ghcr_owner(self, app):
        with app.app_context():
            base = ContainerRegistryService.suggested_base("miapp")
        assert base == "ghcr.io/laurel-applications/laurel_miapp"