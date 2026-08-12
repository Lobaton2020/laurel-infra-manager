"""Seed del modulo Configurator (importado de configurator-lob).

Crea los schemas de ejemplo solo si la tabla `schemas` esta vacia, manteniendo
el comportamiento del proyecto original. Idempotente.
"""
from app.core.db import db
from app.modules.configurator.records.model import Record
from app.modules.configurator.schemas.model import Column, Schema


def seed_configurator() -> None:
    if Schema.query.first():
        return

    s1 = Schema(name="app-settings", description="Configuraciones generales de la app")
    s2 = Schema(name="feature-flags", description="Feature flags para features on/off")

    db.session.add(s1)
    db.session.add(s2)
    db.session.commit()

    cols1 = [
        Column(schema_id=s1.id, name="Key", data_type="string", is_filterable=True, order=0),
        Column(schema_id=s1.id, name="Value", data_type="json", is_filterable=False, order=1),
        Column(schema_id=s1.id, name="Environment", data_type="string", is_filterable=True, order=2),
    ]

    cols2 = [
        Column(schema_id=s2.id, name="FeatureName", data_type="string", is_filterable=True, order=0),
        Column(schema_id=s2.id, name="Enabled", data_type="boolean", is_filterable=True, order=1),
        Column(schema_id=s2.id, name="Value", data_type="json", is_filterable=False, order=2),
    ]

    for c in cols1 + cols2:
        db.session.add(c)
    db.session.commit()

    records1 = [
        Record(schema_id=s1.id, data={"Key": "theme", "Value": {"mode": "dark"}, "Environment": "production"}),
        Record(schema_id=s1.id, data={"Key": "api_url", "Value": {"url": "https://api.example.com"}, "Environment": "production"}),
        Record(schema_id=s1.id, data={"Key": "max_retries", "Value": {"retries": 3}, "Environment": "development"}),
    ]

    records2 = [
        Record(schema_id=s2.id, data={"FeatureName": "new-dashboard", "Enabled": True, "Value": {"rollout": 100}}),
        Record(schema_id=s2.id, data={"FeatureName": "beta-features", "Enabled": False, "Value": {}}),
        Record(schema_id=s2.id, data={"FeatureName": "dark-mode", "Enabled": True, "Value": {"users": ["all"]}}),
    ]

    for r in records1 + records2:
        db.session.add(r)
    db.session.commit()