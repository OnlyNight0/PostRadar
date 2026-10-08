"""Minimal local MVP database models."""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from postradar.db.base import Base


class AdminMessageId(TypeDecorator):
    """Read historical TEXT preview IDs as integers without rewriting rows."""

    impl = Integer
    cache_ok = True

    def process_result_value(
        self, value: int | str | None, _dialect,
    ) -> int | None:
        return int(value) if value is not None else None


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120, collation="NOCASE"), unique=True, nullable=False)
    destination_channel_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    destination_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    sources: Mapped[list["Source"]] = relationship(back_populates="category")
    posts: Mapped[list["SourcePost"]] = relationship(back_populates="category")


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_chat_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    destination_channel_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("categories.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    posts: Mapped[list["SourcePost"]] = relationship(back_populates="source")
    category: Mapped[Category | None] = relationship(back_populates="sources")


class SourcePost(Base):
    __tablename__ = "source_posts"
    __table_args__ = (
        UniqueConstraint("source_id", "telegram_message_id", name="uq_source_post_identity"),
        Index(
            "uq_source_post_grouped_identity",
            "source_id",
            "grouped_id",
            unique=True,
            sqlite_where=text("grouped_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), nullable=False)
    telegram_message_id: Mapped[int] = mapped_column(Integer, nullable=False)
    grouped_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    original_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Legacy compatibility: new captures store complete source text here.
    sanitized_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    edited_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    edited_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    classification_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    media_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    admin_message_id: Mapped[int | None] = mapped_column(AdminMessageId(), nullable=True)
    admin_message_ids: Mapped[str | None] = mapped_column(Text, nullable=True)
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("categories.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), default="NEW", nullable=False)
    capture_attempts: Mapped[int | None] = mapped_column(Integer, nullable=True)
    capture_next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    capture_lease_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    capture_token: Mapped[str | None] = mapped_column(String(32), nullable=True)
    capture_error: Mapped[str | None] = mapped_column(String(120), nullable=True)

    source: Mapped[Source] = relationship(back_populates="posts")
    category: Mapped[Category | None] = relationship(back_populates="posts", lazy="joined")
    media_items: Mapped[list["SourcePostMedia"]] = relationship(
        back_populates="source_post",
        order_by="SourcePostMedia.position",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class SourcePostMedia(Base):
    __tablename__ = "source_post_media"
    __table_args__ = (
        UniqueConstraint(
            "source_post_id", "telegram_message_id", name="uq_source_post_media_message"
        ),
        UniqueConstraint("source_post_id", "position", name="uq_source_post_media_position"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_post_id: Mapped[int] = mapped_column(
        ForeignKey("source_posts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    telegram_message_id: Mapped[int] = mapped_column(Integer, nullable=False)
    media_type: Mapped[str] = mapped_column(String(50), nullable=False)
    media_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)

    source_post: Mapped[SourcePost] = relationship(back_populates="media_items")


class PublishAttempt(Base):
    __tablename__ = "publish_attempts"
    __table_args__ = (
        Index("uq_publish_unresolved_post", "source_post_id", unique=True,
              sqlite_where=text("state IN ('ACTIVE', 'UNKNOWN')")),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    source_post_id: Mapped[int] = mapped_column(ForeignKey("source_posts.id", ondelete="RESTRICT"), index=True)
    destination_id: Mapped[int] = mapped_column(Integer, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    lease_until: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    resolved_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    parts: Mapped[list["PublishAttemptPart"]] = relationship(
        order_by="PublishAttemptPart.position", lazy="selectin", cascade="all, delete-orphan",
    )


class PublishAttemptPart(Base):
    __tablename__ = "publish_attempt_parts"
    __table_args__ = (UniqueConstraint("attempt_id", "position", name="uq_publish_part_position"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("publish_attempts.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    telegram_message_ids: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
