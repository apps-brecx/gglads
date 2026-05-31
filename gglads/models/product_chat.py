from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from gglads.models.base import Base


class ProductChatMessage(Base):
    """Conversation between the user and Claude about a specific product.

    Used by the SEO tab so the user can give context ("we just lowered the
    price", "ship-only-to-US now") and Claude folds it into the next SEO
    generation. topic='seo' for now; reserved for future per-tab chats.
    """

    __tablename__ = "product_chat_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("shopify_products.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    topic: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="seo"
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)  # 'user' | 'assistant'
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
