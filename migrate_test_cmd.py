"""
Migracion: agregar applications.test_cmd.

Idempotente. Pensado para correr despues de hacer pull del cambio
en app/modules/apps/model.py que agrega la columna `test_cmd` (TEXT NOT
NULL con default "echo 'no tests configured'"). El backend la pasa a
Jenkins como parametro TEST_CMD del build; Jenkins corre el comando
como STAGE 1 del pipeline (tests -> build -> push).

Uso:
    .venv/bin/python migrate_test_cmd.py
"""
import logging
import os
import sys
from pathlib import Path

from sqlalchemy import create_engine, text

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("migrate_test_cmd")


def main() -> int:
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

    db_type = os.environ.get("DB_TYPE", "sqlite")
    if db_type == "mysql":
        host = os.environ.get("MYSQL_HOST", "localhost")
        port = int(os.environ.get("MYSQL_PORT", 3306))
        user = os.environ.get("MYSQL_USER", "root")
        pw = os.environ.get("MYSQL_PASSWORD", "")
        db = os.environ.get("MYSQL_DATABASE", "orchestrator")
        url = f"mysql+pymysql://{user}:{pw}@{host}:{port}/{db}"
    else:
        url = "sqlite:///laurel.db"

    logger.info("Conectando a %s", url.split("@")[-1])
    engine = create_engine(url, future=True)

    # En MySQL el default se escapa con '' (dobleo las comillas). En SQLite
    # el default es literal entre comillas dobles.
    default_value = "echo 'no tests configured'"

    with engine.begin() as conn:
        if db_type == "mysql":
            exists = conn.execute(text("""
                SELECT COUNT(*) FROM information_schema.columns
                WHERE table_schema = DATABASE()
                  AND table_name = 'applications'
                  AND column_name = 'test_cmd'
            """)).scalar()
            if exists:
                logger.info("applications.test_cmd ya existe, skip")
                return 0
            logger.info("Agregando applications.test_cmd")
            escaped = default_value.replace("'", "''")
            conn.execute(text(
                "ALTER TABLE applications "
                "ADD COLUMN test_cmd TEXT NOT NULL "
                f"DEFAULT '{escaped}'"
            ))
        else:
            cols = [row[1] for row in conn.execute(text("PRAGMA table_info(applications)"))]
            if "test_cmd" in cols:
                logger.info("applications.test_cmd ya existe, skip")
                return 0
            logger.info("Agregando applications.test_cmd")
            conn.execute(text(
                "ALTER TABLE applications "
                f"ADD COLUMN test_cmd TEXT NOT NULL DEFAULT '{default_value}'"
            ))

    logger.info("Migracion completada")
    return 0


if __name__ == "__main__":
    sys.exit(main())
