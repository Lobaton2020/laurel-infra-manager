from app.core.db import db
from app.core.utils import utcnow


class Audit(db.Model):
    """Traza de toda mutacion, tanto en el catalogo como en el cluster."""

    __tablename__ = "audits"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.String(50), default="unknown")
    action = db.Column(db.String(50), nullable=False)
    entity_type = db.Column(db.String(50), nullable=False)
    entity_id = db.Column(db.String(150), nullable=False)
    old_data = db.Column(db.JSON)
    new_data = db.Column(db.JSON)
    created_at = db.Column(db.DateTime, default=utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "action": self.action,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "old_data": self.old_data,
            "new_data": self.new_data,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
