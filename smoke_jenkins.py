"""
Smoke E2E: webhook real -> Jenkins real local.

Prueba el flujo completo del modulo builds sin k8s ni GitHub:
  1. Sube el backend Flask con app context.
  2. Stub `SystemSecretService.get_content("jenkins_token")` para que
     devuelva el token que configuramos en Jenkins.
  3. Stub `application_id` lookup para que encuentre la app 'demo' (seed).
  4. Llama al controller del webhook con un payload simulado de GitHub.
  5. Verifica que se creo un AppBuild en estado pending -> running -> success.
  6. Verifica que el GET /api/apps/<id>/builds lo refleja con polling.
"""
import os
import sys
import time
from pathlib import Path

# Carga .env (config.py se apoya alli).
env_path = Path(__file__).parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

# Forzar JENKINS_URL local para el smoke test.
os.environ["JENKINS_URL"] = "http://localhost:8081"
os.environ["JENKINS_BUILD_TOKEN"] = "laurel-test-token-2026"

from app import create_app
from app.core.db import db
from app.modules.apps.model import Application
from app.modules.builds.model import AppBuild
from app.modules.system.service import SystemSecretService


# 1) Stub del SystemSecret para que devuelva el token sin ir a k8s.
def _fake_get_content(secret_id: str) -> dict:
    if secret_id == "jenkins_token":
        return {
            "id": "jenkins_token",
            "namespace": "prod",
            "name": "laurel-integrations",
            "key": "jenkins-token",
            "kind": "text",
            "content": "laurel-test-token-2026",
            "entries": None,
        }
    raise Exception(f"no stub for {secret_id}")


SystemSecretService.get_content = staticmethod(_fake_get_content)


def _ensure_demo_app() -> int:
    """Crea la app 'demo' si no existe y devuelve su id."""
    existing = Application.query.filter_by(slug="demo", deleted_at=None).first()
    if existing:
        existing.current_version = "1.2.3-smoke"
        db.session.commit()
        return existing.id
    app = Application(
        slug="demo",
        name="Demo app for Jenkins smoke test",
        github_repo_url="git@github.com:aflobaton/laurel_demo.git",
        docker_image_base="aflobaton/laurel_demo",
        current_version="1.2.3-smoke",
    )
    db.session.add(app)
    db.session.commit()
    return app.id


def main() -> int:
    app = create_app()
    with app.app_context():
        db.create_all()
        app_id = _ensure_demo_app()
        print(f"[seed] app id={app_id}, current_version=1.2.3-smoke")

        # 2) Llamar al webhook como si fuera GitHub.
        import hmac, hashlib, json
        app.config["GITHUB_WEBHOOK_SECRET"] = "test-secret"
        payload = {
            "ref": "refs/heads/master",
            "repository": {"name": "laurel_demo", "full_name": "aflobaton/laurel_demo"},
            "head_commit": {"id": "deadbeefcafe1234567890abcdef1234567890ab"},
        }
        payload_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        sig = "sha256=" + hmac.new(b"test-secret", payload_bytes, hashlib.sha256).hexdigest()

        # Generar un JWT para que el resto de los endpoints (que SI requieren
        # Bearer) acepten las requests. Usamos el SECRET_KEY del config.
        import jwt as _jwt
        from datetime import datetime, timedelta, timezone
        from app.modules.users.model import User
        user = User.query.first()
        if user is None:
            user = User(sub="smoke-tester", email="smoke@test.local", name="Smoke")
            db.session.add(user)
            db.session.commit()
        now = datetime.now(timezone.utc)
        jwt_token = _jwt.encode(
            {
                "sub": user.sub,
                "email": user.email,
                "name": user.name,
                "iat": now,
                "exp": now + timedelta(hours=1),
            },
            app.config["SECRET_KEY"],
            algorithm="HS256",
        )

        client = app.test_client()
        r = client.post(
            "/api/webhooks/github",
            data=payload_bytes,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": sig,
            },
        )
        print(f"[webhook] status={r.status_code} body={r.get_json()}")
        if r.status_code != 200:
            return 1

        build_id = r.get_json().get("build_id")
        if not build_id:
            print("[FAIL] webhook no devolvio build_id")
            return 1
        print(f"[webhook] AppBuild #{build_id} creado")

        # 3) Polling al build: deberia estar running o success.
        auth = {"Authorization": f"Bearer {jwt_token}"}
        for attempt in range(10):
            r = client.get(
                f"/api/apps/{app_id}/builds/{build_id}?poll=true",
                headers=auth,
            )
            data = r.get_json()
            print(f"[poll {attempt}] status={data.get('status')} "
                  f"jenkins_url={data.get('jenkins_url')}")
            if data.get("status") in ("success", "failed", "aborted"):
                break
            time.sleep(2)
        else:
            print("[FAIL] no llego a estado terminal en 20s")
            return 1

        # 4) Listar todos los builds de la app.
        r = client.get(f"/api/apps/{app_id}/builds", headers=auth)
        items = r.get_json()["items"]
        print(f"[list] {len(items)} builds, ultimo status={items[0]['status']}")
        for it in items:
            print(f"  - #{it['id']} v{it['version']} {it['status']} "
                  f"({it.get('jenkins_url', '?')})")

        if items[0]["status"] != "success":
            print(f"[FAIL] esperado 'success', obtuvo '{items[0]['status']}'")
            return 1

        print("[OK] end-to-end OK")
        return 0


if __name__ == "__main__":
    sys.exit(main())
