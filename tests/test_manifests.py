"""Tests del generador de manifiestos.

Es la logica con mas reglas de negocio del proyecto y no requiere cluster: se
verifica sobre los dicts generados. Patron canonico: ver deploy/base/ de
Manejo-Finanzas.
"""
import pytest

from app.core.constants import MANAGED_BY
from app.modules.scoops.manifest import ManifestService
from app.modules.scoops.model import Scoop


def make_scoop(**overrides) -> Scoop:
    defaults = {
        "id": 1,
        "name": "manejo-finanzas",
        "application": "manejo-finanzas",
        "type": "api",
        "status": "pending",
        "version": "1.4.2",
        "is_productive": True,
        "requested_vcpu": "50m",
        "requested_memory": "128M",
        "limit_vcpu": "300m",
        "limit_memory": "384M",
        "min_replicas": 1,
        "max_replicas": 1,
        "url_registry": "aflobaton/manejo-finanzas:latest",
        "port": 3001,
        "namespace": "prod",
        # Agnostico: el scoop declara lo que la imagen expone.
        "container_port": 80,
        "health_path": "/",
    }
    return Scoop(**{**defaults, **overrides})


def by_kind(manifests: list[dict]) -> dict:
    return {m["kind"]: m for m in manifests}


class TestApiScoop:
    @pytest.fixture
    def manifests(self, app):
        with app.app_context():
            return by_kind(ManifestService.build(make_scoop()))

    def test_generates_deployment_and_service(self, manifests):
        assert set(manifests) == {"Deployment", "Service"}

    def test_image_is_url_registry_verbatim(self, manifests):
        container = manifests["Deployment"]["spec"]["template"]["spec"]["containers"][0]
        assert container["image"] == "aflobaton/manejo-finanzas:latest"

    def test_uses_simple_app_label(self, manifests):
        labels = manifests["Deployment"]["metadata"]["labels"]
        assert labels["app"] == "manejo-finanzas"
        assert labels["app.kubernetes.io/managed-by"] == MANAGED_BY
        assert labels["version"] == "1.4.2"

    def test_container_listens_on_declared_port(self, manifests):
        container = manifests["Deployment"]["spec"]["template"]["spec"]["containers"][0]
        assert container["ports"][0]["containerPort"] == 80

    def test_service_is_loadbalancer(self, manifests):
        spec = manifests["Service"]["spec"]
        assert spec["type"] == "LoadBalancer"
        port = spec["ports"][0]
        assert port["port"] == 3001
        assert port["targetPort"] == 80
        assert port["name"] == "http"

    def test_service_selector_matches_deployment(self, manifests):
        dep_sel = manifests["Deployment"]["spec"]["selector"]["matchLabels"]
        svc_sel = manifests["Service"]["spec"]["selector"]
        assert dep_sel == svc_sel

    def test_resources(self, manifests):
        resources = manifests["Deployment"]["spec"]["template"]["spec"]["containers"][0]["resources"]
        assert resources["requests"] == {"cpu": "50m", "memory": "128M"}
        assert resources["limits"] == {"cpu": "300m", "memory": "384M"}

    def test_probes_target_declared_path(self, manifests):
        container = manifests["Deployment"]["spec"]["template"]["spec"]["containers"][0]
        assert container["readinessProbe"]["httpGet"]["path"] == "/"
        assert container["readinessProbe"]["httpGet"]["port"] == 80
        assert container["livenessProbe"]["httpGet"]["path"] == "/"

    def test_no_ingress(self, manifests):
        assert "Ingress" not in manifests

    def test_selector_is_stable_subset_of_labels(self, manifests):
        selector = manifests["Deployment"]["spec"]["selector"]["matchLabels"]
        labels = manifests["Deployment"]["spec"]["template"]["metadata"]["labels"]
        assert selector.items() <= labels.items()
        assert set(selector) == {"app", "app.kubernetes.io/managed-by"}


class TestDefaultsApplied:
    """El server asigna container_port y health_path en create() desde config.

    Aqui validamos que el manifest respeta esos valores y que un cambio en config
    cambia el puerto interno, no el puerto del LB.
    """

    def test_container_port_appears_in_deployment(self, app):
        with app.app_context():
            manifests = by_kind(ManifestService.build(make_scoop()))
        container = manifests["Deployment"]["spec"]["template"]["spec"]["containers"][0]
        assert container["ports"][0]["containerPort"] == 80
        assert container["readinessProbe"]["httpGet"]["port"] == 80

    def test_lb_target_uses_container_port(self, app):
        with app.app_context():
            manifests = by_kind(ManifestService.build(make_scoop(port=3020, container_port=80)))
        port = manifests["Service"]["spec"]["ports"][0]
        assert port["port"] == 3020      # LB port (lo que usa LAN)
        assert port["targetPort"] == 80  # container port (lo que el form NO edita)

    def test_health_path_is_applied(self, app):
        with app.app_context():
            manifests = by_kind(ManifestService.build(make_scoop(health_path="/api/health")))
        container = manifests["Deployment"]["spec"]["template"]["spec"]["containers"][0]
        assert container["readinessProbe"]["httpGet"]["path"] == "/api/health"

    def test_worker_still_has_no_service(self, app):
        with app.app_context():
            manifests = by_kind(ManifestService.build(
                make_scoop(type="worker", port=None)
            ))
        assert "Service" not in manifests
        assert "Deployment" in manifests


class TestHpaOnlyWithScalingRange:
    def test_hpa_generated_when_max_above_min(self, app):
        with app.app_context():
            manifests = by_kind(ManifestService.build(
                make_scoop(min_replicas=1, max_replicas=5)
            ))
        assert "HorizontalPodAutoscaler" in manifests
        assert manifests["HorizontalPodAutoscaler"]["spec"]["maxReplicas"] == 5

    def test_no_hpa_when_min_equals_max(self, app):
        with app.app_context():
            manifests = by_kind(ManifestService.build(
                make_scoop(min_replicas=2, max_replicas=2)
            ))
        assert "HorizontalPodAutoscaler" not in manifests
        assert manifests["Deployment"]["spec"]["replicas"] == 2


class TestOtherTypes:
    def test_worker_has_no_service(self, app):
        with app.app_context():
            manifests = by_kind(ManifestService.build(make_scoop(type="worker", port=None)))
        assert set(manifests) == {"Deployment"}

    def test_cronjob_generates_only_cronjob(self, app):
        with app.app_context():
            manifests = by_kind(ManifestService.build(
                make_scoop(type="cronjob", port=None, schedule="0 3 * * *")
            ))
        assert set(manifests) == {"CronJob"}
        assert manifests["CronJob"]["spec"]["schedule"] == "0 3 * * *"
        assert manifests["CronJob"]["spec"]["jobTemplate"]["spec"]["template"]["spec"][
            "restartPolicy"] == "OnFailure"


class TestNamespaceAndLabels:
    def test_namespace_override(self, app):
        with app.app_context():
            manifests = ManifestService.build(make_scoop(), namespace="monitoring")
        assert all(m["metadata"]["namespace"] == "monitoring" for m in manifests)

    def test_falls_back_to_scoop_namespace(self, app):
        with app.app_context():
            manifests = ManifestService.build(make_scoop(namespace="openclaw"))
        assert all(m["metadata"]["namespace"] == "openclaw" for m in manifests)

    def test_git_ref_is_sanitized_into_valid_label(self, app):
        with app.app_context():
            manifests = by_kind(ManifestService.build(
                make_scoop(version="feature/nueva-api")
            ))
        version = manifests["Deployment"]["metadata"]["labels"]["version"]
        assert version == "feature-nueva-api"

    def test_manifest_order_puts_dependencies_first(self, app):
        with app.app_context():
            kinds = [m["kind"] for m in ManifestService.build(
                make_scoop(min_replicas=1, max_replicas=3)
            )]
        assert kinds.index("Deployment") < kinds.index("Service")


class TestPreviewEndpoint:
    def test_preview_does_not_touch_cluster(self, client, scoop_payload):
        created = client.post("/api/scoops", json=scoop_payload).get_json()
        response = client.get(f"/api/scoops/{created['id']}/manifests")
        assert response.status_code == 200
        data = response.get_json()
        assert data["namespace"] == "prod"
        # El fixture trae max_replicas=3 con min_replicas=1, asi que tambien
        # aparece el HPA. Aqui validamos que Deployment + Service estan, y
        # que el orden de dependencias es el correcto.
        kinds = [m["kind"] for m in data["manifests"]]
        assert "Deployment" in kinds
        assert "Service" in kinds
        assert kinds.index("Deployment") < kinds.index("Service")