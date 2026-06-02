from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from gglads.models.base import Base


class EntityTask(Base):
    """One row per (entity_type, entity_id, task_slug). Tracks both the
    assignment (assigned_to_user_id) and the completion (completed_*).

    Same row is reused across the lifecycle:
      open + unassigned  → all fields except entity/* are NULL
      assigned           → assigned_to_user_id + assigned_at set
      completed          → completed_by_user_id + completed_at set
      reset              → completed_* cleared (assignment may stay)
    """

    __tablename__ = "entity_tasks"
    __table_args__ = (
        UniqueConstraint(
            "entity_type", "entity_id", "task_slug",
            name="uq_entity_tasks_entity_task",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(20), nullable=False)
    entity_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    task_slug: Mapped[str] = mapped_column(String(40), nullable=False)

    assigned_to_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    assigned_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    assigned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    completed_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
