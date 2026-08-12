"""Modelos del modulo Configurator: esquemas de configuracion y sus columnas.

Importado desde configurator-lob (backend) y adaptado a las convenciones de
laurel-infra-manager (model.py por modulo, `utcnow` de app.core.utils).
"""
from app.core.db import db
from app.core.utils import utcnow


class Schema(db.Model):
    __tablename__ = "schemas"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    description = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    columns = db.relationship(
        "Column", backref="schema", lazy="dynamic", cascade="all, delete-orphan"
    )
    records = db.relationship(
        "Record", backref="schema", lazy="dynamic", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Schema {self.name}>"


class Column(db.Model):
    __tablename__ = "columns"

    id = db.Column(db.Integer, primary_key=True)
    schema_id = db.Column(db.Integer, db.ForeignKey("schemas.id"), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    data_type = db.Column(db.String(20), nullable=False)
    is_filterable = db.Column(db.Boolean, default=True)
    order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    __table_args__ = (db.UniqueConstraint("schema_id", "name", name="uq_schema_column"),)

    def __repr__(self) -> str:
        return f"<Column {self.name} ({self.data_type})>"
