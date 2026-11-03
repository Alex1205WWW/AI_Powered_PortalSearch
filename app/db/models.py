"""SQLAlchemy models: the lean Milestone 1 schema (Roadmap_M1.md §5). Migration 0001 is generated from here.

Conventions
- Enumerations are short strings validated in `app.domain`, not Postgres enums (easier to evolve).
- Geo codes are hierarchical paths: `lisboa`, `lisboa/oeiras`, `lisboa/oeiras/alges`.
- Money is whole euros (Integer); areas are m2 (Float).
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


TS = DateTime(timezone=True)


class Geo(Base):
    __tablename__ = "geo"
    code: Mapped[str] = mapped_column(String(160), primary_key=True)
    level: Mapped[str] = mapped_column(String(10), nullable=False)  # district | council | parish
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    parent_code: Mapped[str | None] = mapped_column(String(160), ForeignKey("geo.code"), index=True)


class Source(Base):
    __tablename__ = "sources"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(60), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)  # portal | agency | email
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    schedule_minutes: Mapped[int | None] = mapped_column(Integer)
    crawl_delay_s: Mapped[float] = mapped_column(Float, default=2.0, nullable=False)
    robots_fetched_at: Mapped[datetime | None] = mapped_column(TS)
    robots_status: Mapped[int | None] = mapped_column(Integer)
    robots_allowed: Mapped[bool | None] = mapped_column(Boolean)
    robots_txt: Mapped[str | None] = mapped_column(Text)
    content_signal: Mapped[str | None] = mapped_column(String(200))
    signal_search: Mapped[str | None] = mapped_column(String(5))
    signal_ai_input: Mapped[str | None] = mapped_column(String(5))
    signal_ai_train: Mapped[str | None] = mapped_column(String(5))
    confidence: Mapped[str | None] = mapped_column(String(10))  # full | partial | none (agency sites)
    profile: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(10), default="ok", nullable=False)  # ok | degraded | blocked
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Size of the catalogue the last successful pass saw. The removal guard refuses to retire listings when a
    # sitemap comes back short, so one truncated fetch cannot wipe an agency's stock (Plan_Day5.md section 3.3).
    last_catalogue_size: Mapped[int | None] = mapped_column(Integer)
    last_ok_at: Mapped[datetime | None] = mapped_column(TS)
    last_error: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), nullable=False)


class Buyer(Base):
    __tablename__ = "buyers"
    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    paused_at: Mapped[datetime | None] = mapped_column(TS)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), nullable=False)

    criteria: Mapped[list["Criteria"]] = relationship(
        back_populates="buyer", order_by="Criteria.id", cascade="all, delete-orphan", passive_deletes=True
    )


class Criteria(Base):
    __tablename__ = "criteria"
    id: Mapped[int] = mapped_column(primary_key=True)
    buyer_id: Mapped[int] = mapped_column(ForeignKey("buyers.id", ondelete="CASCADE"), nullable=False, index=True)
    transaction: Mapped[str] = mapped_column(String(10), default="sale", nullable=False)  # sale | rent
    estate_types: Mapped[list[str]] = mapped_column(ARRAY(String(20)), default=list, nullable=False)
    typology_min: Mapped[int | None] = mapped_column(Integer)
    typology_max: Mapped[int | None] = mapped_column(Integer)  # NULL = open ("T2 ou mais")
    price_min: Mapped[int | None] = mapped_column(Integer)
    price_max: Mapped[int | None] = mapped_column(Integer)
    area_min: Mapped[int | None] = mapped_column(Integer)
    must_have: Mapped[list[str]] = mapped_column(ARRAY(String(30)), default=list, nullable=False)
    nice_to_have: Mapped[list[str]] = mapped_column(ARRAY(String(30)), default=list, nullable=False)
    exclude: Mapped[list[str]] = mapped_column(ARRAY(String(60)), default=list, nullable=False)
    include_private_owner: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    include_new_developments: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    unknown_policy: Mapped[str] = mapped_column(String(5), default="show", nullable=False)  # show | hide
    source_text: Mapped[str | None] = mapped_column(Text)
    input_channel: Mapped[str] = mapped_column(String(6), default="form", nullable=False)  # voice | text | form
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), onupdate=func.now(), nullable=False)

    buyer: Mapped[Buyer] = relationship(back_populates="criteria")
    zones: Mapped[list["CriteriaZone"]] = relationship(cascade="all, delete-orphan", order_by="CriteriaZone.geo_code")


class CriteriaZone(Base):
    __tablename__ = "criteria_zones"
    criteria_id: Mapped[int] = mapped_column(ForeignKey("criteria.id", ondelete="CASCADE"), primary_key=True)
    geo_code: Mapped[str] = mapped_column(ForeignKey("geo.code"), primary_key=True)

    geo: Mapped[Geo] = relationship()


class CriteriaDraft(Base):
    __tablename__ = "criteria_drafts"
    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    telegram_file_id: Mapped[str | None] = mapped_column(String(200))
    transcript: Mapped[str | None] = mapped_column(Text)
    parsed: Mapped[dict | None] = mapped_column(JSONB)
    ambiguities: Mapped[list | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(10), default="pending", nullable=False)  # pending | confirmed | cancelled
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), nullable=False)


class Agency(Base):
    __tablename__ = "agencies"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    name_norm: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    phone: Mapped[str | None] = mapped_column(String(40))
    website_url: Mapped[str | None] = mapped_column(Text)
    website_resolution: Mapped[str] = mapped_column(String(8), default="none", nullable=False)  # profile | manual | none
    imovirtual_slug: Mapped[str | None] = mapped_column(String(200))
    # The advertiser's CRM/platform as the portal feed reports it, NOT an identity: measured 2026-10-31 the
    # live values are `urn:partner:ego` (132 agencies), `urn:site:local` (65), `urn:partner:maxwork` (55),
    # `urn:partner:century21` (54), `proppy`, `ximo`, `zomecrm`... It is a free platform fingerprint from the
    # portal, available before the agency's own site is ever fetched.
    source_urn: Mapped[str | None] = mapped_column(String(120))
    # The directory UUID: this is the real identity. `name_norm` is not, because two different agencies can
    # normalise to the same string (Plan_Day5.md section 4).
    imovirtual_id: Mapped[str | None] = mapped_column(String(40), unique=True)
    active_ads_count: Mapped[int | None] = mapped_column(Integer)          # adsSell
    active_ads_rent_count: Mapped[int | None] = mapped_column(Integer)     # adsRent: tells a dead account from a rental-only one
    checked_at: Mapped[datetime | None] = mapped_column(TS)                # profile looked at; "no website" is a remembered answer
    offportal_ratio: Mapped[float | None] = mapped_column(Float)           # share of its listings no portal had
    listings_seen: Mapped[int | None] = mapped_column(Integer)             # denominator of the ratio
    geo_code: Mapped[str | None] = mapped_column(ForeignKey("geo.code"))
    watch_source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"))
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), nullable=False)


class Property(Base):
    """Cluster id for one real-world property; members are `listings.property_id`."""

    __tablename__ = "properties"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), nullable=False)


class Listing(Base):
    __tablename__ = "listings"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_listing_source_external"),
        Index("ix_listings_geo_type_rooms", "geo_code", "estate_type", "rooms"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False)
    external_id: Mapped[str] = mapped_column(String(120), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    property_id: Mapped[int | None] = mapped_column(ForeignKey("properties.id"), index=True)
    title: Mapped[str | None] = mapped_column(Text)
    price: Mapped[int | None] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="EUR", nullable=False)
    hide_price: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    area_util: Mapped[float | None] = mapped_column(Float)
    area_bruta: Mapped[float | None] = mapped_column(Float)
    land_area: Mapped[float | None] = mapped_column(Float)
    rooms: Mapped[int | None] = mapped_column(Integer)
    wc: Mapped[int | None] = mapped_column(Integer)
    floor: Mapped[int | None] = mapped_column(Integer)  # -1 cave, 0 res-do-chao, 11 = above tenth; NULL unknown
    estate_type: Mapped[str | None] = mapped_column(String(20))  # apartamento | moradia | ...
    transaction: Mapped[str] = mapped_column(String(10), default="sale", nullable=False)
    geo_code: Mapped[str | None] = mapped_column(ForeignKey("geo.code"))
    street: Mapped[str | None] = mapped_column(Text)
    lat: Mapped[float | None] = mapped_column(Float)
    lng: Mapped[float | None] = mapped_column(Float)
    agency_id: Mapped[int | None] = mapped_column(ForeignKey("agencies.id"), index=True)
    agency_name_raw: Mapped[str | None] = mapped_column(String(200))
    agency_phone_raw: Mapped[str | None] = mapped_column(String(60))
    reference: Mapped[str | None] = mapped_column(String(80))
    is_private_owner: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_new_development: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    features: Mapped[dict | None] = mapped_column(JSONB)  # {"garagem": {"value": true, "tier": "confirmed"}, ...}
    images: Mapped[list | None] = mapped_column(JSONB)
    description: Mapped[str | None] = mapped_column(Text)  # plain text; needed to re-match "excluir" terms later
    missing_fields: Mapped[list[str]] = mapped_column(ARRAY(String(30)), default=list, nullable=False)
    status: Mapped[str] = mapped_column(String(10), default="active", nullable=False)  # active | removed | sold
    published_at: Mapped[datetime | None] = mapped_column(TS)
    first_seen_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), nullable=False)
    removed_at: Mapped[datetime | None] = mapped_column(TS)
    raw: Mapped[dict | None] = mapped_column(JSONB)

    images_rel: Mapped[list["ListingImage"]] = relationship(cascade="all, delete-orphan", order_by="ListingImage.position")


class ListingImage(Base):
    __tablename__ = "listing_images"
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"), primary_key=True)
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    phash: Mapped[int | None] = mapped_column(BigInteger, index=True)
    # Failed hash attempts for this URL. `phash` stays NULL - the image really has no hash, and dedup must
    # keep reading that as unknown - but the warmer needs to know it has already tried, or a permanently
    # dead URL is re-fetched every 15 minutes for ever (36 of them were).
    phash_failures: Mapped[int] = mapped_column(SmallInteger, default=0, nullable=False, server_default="0")


class ListingPriceHistory(Base):
    __tablename__ = "listing_price_history"
    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"), nullable=False, index=True)
    price: Mapped[int] = mapped_column(Integer, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), nullable=False)
    source_reported: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class PropertySplit(Base):
    """Manual override: these two listings are NOT the same property."""

    __tablename__ = "property_splits"
    listing_a: Mapped[int] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"), primary_key=True)
    listing_b: Mapped[int] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"), primary_key=True)


class PropertyMatch(Base):
    __tablename__ = "property_matches"
    __table_args__ = (
        UniqueConstraint("property_id", "criteria_id", name="uq_match_property_criteria"),
        Index("ix_matches_buyer_state", "buyer_id", "state"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    property_id: Mapped[int] = mapped_column(ForeignKey("properties.id", ondelete="CASCADE"), nullable=False)
    criteria_id: Mapped[int] = mapped_column(ForeignKey("criteria.id", ondelete="CASCADE"), nullable=False)
    buyer_id: Mapped[int] = mapped_column(ForeignKey("buyers.id", ondelete="CASCADE"), nullable=False)
    grade: Mapped[str] = mapped_column(String(10), nullable=False)  # confirmada | possivel
    satisfied: Mapped[list[str]] = mapped_column(ARRAY(String(30)), default=list, nullable=False)
    unsatisfied: Mapped[list[str]] = mapped_column(ARRAY(String(30)), default=list, nullable=False)
    unknown: Mapped[list[str]] = mapped_column(ARRAY(String(30)), default=list, nullable=False)
    state: Mapped[str] = mapped_column(String(12), default="novo", nullable=False)
    commission_share_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    notes: Mapped[str | None] = mapped_column(Text)
    matched_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), nullable=False)
    state_updated_at: Mapped[datetime | None] = mapped_column(TS)


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint(
            "buyer_id", "property_id", "reason", "price_at_send",
            name="uq_notification_once", postgresql_nulls_not_distinct=True,
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    buyer_id: Mapped[int] = mapped_column(ForeignKey("buyers.id", ondelete="CASCADE"), nullable=False)
    property_id: Mapped[int | None] = mapped_column(ForeignKey("properties.id", ondelete="CASCADE"))
    reason: Mapped[str] = mapped_column(String(16), nullable=False)
    price_at_send: Mapped[int | None] = mapped_column(Integer)
    payload: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(TS)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    # Delivery bookkeeping. Without it a failing send is retried every two minutes for the life of the
    # process - 738 such attempts in 24 h while the bot was blocked - with nothing anywhere saying so.
    # `failed_at` is the give-up marker: the row stays for the record but leaves the pending queue, so a
    # permanent failure can never block the alerts queued behind it.
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    failed_at: Mapped[datetime | None] = mapped_column(TS)


class CrawlRun(Base):
    __tablename__ = "crawl_runs"
    __table_args__ = (Index("ix_crawl_runs_source_started", "source_id", "started_at"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)  # sweep | initial | backfill | refresh | watch | email
    criteria_id: Mapped[int | None] = mapped_column(ForeignKey("criteria.id", ondelete="SET NULL"))
    started_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(TS)
    status: Mapped[str] = mapped_column(String(10), default="running", nullable=False)  # running | ok | failed
    pages_fetched: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    listings_new: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    listings_updated: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    cursor: Mapped[dict | None] = mapped_column(JSONB)  # resumable position (backfill)


class EmailMessage(Base):
    __tablename__ = "email_messages"
    mailbox: Mapped[str] = mapped_column(String(120), primary_key=True)
    uid: Mapped[int] = mapped_column(Integer, primary_key=True)
    received_at: Mapped[datetime | None] = mapped_column(TS)
    parsed_ok: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    listing_urls_found: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)


class AiSpend(Base):
    __tablename__ = "ai_spend"
    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    operation: Mapped[str] = mapped_column(String(30), nullable=False)
    model: Mapped[str | None] = mapped_column(String(60))
    tokens_in: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    audio_seconds: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    cost_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), nullable=False, index=True)


class AppSetting(Base):
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[dict | list | str | int | None] = mapped_column(JSONB)


class ManualFinding(Base):
    """A link she found by hand that the system did not bring: the acceptance-test instrument."""

    __tablename__ = "manual_findings"
    id: Mapped[int] = mapped_column(primary_key=True)
    buyer_id: Mapped[int | None] = mapped_column(ForeignKey("buyers.id", ondelete="SET NULL"))
    url: Mapped[str] = mapped_column(Text, nullable=False)
    reported_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), nullable=False)
    matched_listing_id: Mapped[int | None] = mapped_column(ForeignKey("listings.id", ondelete="SET NULL"))
    in_agreed_source: Mapped[bool | None] = mapped_column(Boolean)
    published_in_window: Mapped[bool | None] = mapped_column(Boolean)
    verdict: Mapped[str | None] = mapped_column(String(20))
