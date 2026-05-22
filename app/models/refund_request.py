from __future__ import annotations
from datetime import datetime
from typing import TYPE_CHECKING
from sqlalchemy import DateTime, Enum as SAEnum, ForeignKey, Integer, Numeric, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base
from app.enums import RefundStatus
if TYPE_CHECKING:
    from app.models.user import User
    from app.models.payment import Payment

class RefundRequest(Base):
    __tablename__ = "refund_requests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    payment_id: Mapped[int] = mapped_column(ForeignKey("payments.id", ondelete="CASCADE"), nullable=False, index=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[RefundStatus] = mapped_column(
        SAEnum(RefundStatus, values_callable=lambda x: [e.value for e in x], native_enum=False),
        nullable=False,
        default=RefundStatus.pending,
        index=True,
    )
    traffic_used_mb_at_request: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    admin_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    processed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    user: Mapped["User"] = relationship("User", foreign_keys=[user_id])
    payment: Mapped["Payment"] = relationship("Payment", foreign_keys=[payment_id])
    processed_by: Mapped["User | None"] = relationship("User", foreign_keys=[processed_by_id])
