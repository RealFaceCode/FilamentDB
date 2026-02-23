from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, UniqueConstraint, ForeignKey

from .db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Spool(Base):
    __tablename__ = "spools"

    id = Column(Integer, primary_key=True, index=True)
    brand = Column(String(120), nullable=False)
    material = Column(String(80), nullable=False)
    color = Column(String(80), nullable=False)
    weight_g = Column(Float, nullable=False)
    remaining_g = Column(Float, nullable=False)
    low_stock_threshold_g = Column(Float, nullable=True)
    price = Column(Float, nullable=True)
    location = Column(String(120), nullable=True)
    storage_sub_location_id = Column(Integer, ForeignKey("storage_sub_locations.id"), nullable=True, index=True)
    ams_printer = Column(String(120), nullable=True, index=True)
    ams_slot = Column(Integer, nullable=True, index=True)
    lifecycle_status = Column(String(32), nullable=False, default="new", index=True)
    in_use = Column(Boolean, default=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    project = Column(String(40), nullable=False, default="private", index=True)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


class UsageHistory(Base):
    __tablename__ = "usage_history"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime, default=_utcnow, index=True)
    actor = Column(String(120), nullable=True)
    mode = Column(String(20), nullable=False)
    source_app = Column(String(120), nullable=True)
    batch_id = Column(String(64), nullable=True, index=True)
    source_file = Column(String(255), nullable=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    project = Column(String(40), nullable=False, default="private", index=True)

    spool_id = Column(Integer, nullable=True, index=True)
    spool_brand = Column(String(120), nullable=True)
    spool_material = Column(String(80), nullable=True)
    spool_color = Column(String(80), nullable=True)

    deducted_g = Column(Float, nullable=False)
    remaining_before_g = Column(Float, nullable=False)
    remaining_after_g = Column(Float, nullable=False)
    undone = Column(Boolean, default=False)
    undone_at = Column(DateTime, nullable=True)


class StorageArea(Base):
    __tablename__ = "storage_areas"
    __table_args__ = (UniqueConstraint("project", "code", name="uq_storage_areas_project_code"),)

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    project = Column(String(40), nullable=False, index=True)
    code = Column(String(32), nullable=False, index=True)
    name = Column(String(120), nullable=True)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


class StorageSubLocation(Base):
    __tablename__ = "storage_sub_locations"
    __table_args__ = (
        UniqueConstraint("project", "area_id", "code", name="uq_storage_sub_locations_project_area_code"),
        UniqueConstraint("project", "path_code", name="uq_storage_sub_locations_project_path"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    project = Column(String(40), nullable=False, index=True)
    area_id = Column(Integer, ForeignKey("storage_areas.id"), nullable=False, index=True)
    code = Column(String(32), nullable=False, index=True)
    path_code = Column(String(80), nullable=False, index=True)
    name = Column(String(120), nullable=True)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


class UsageBatchContext(Base):
    __tablename__ = "usage_batch_context"
    __table_args__ = (UniqueConstraint("project", "batch_id", name="uq_usage_batch_context_project_batch"),)

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime, default=_utcnow, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    project = Column(String(40), nullable=False, index=True)
    batch_id = Column(String(64), nullable=False, index=True)
    printer_name = Column(String(120), nullable=True)
    ams_slots = Column(String(255), nullable=True)


class DeviceSlotState(Base):
    __tablename__ = "device_slot_state"
    __table_args__ = (UniqueConstraint("project", "printer_name", "slot", name="uq_device_slot_state_project_printer_slot"),)

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    project = Column(String(40), nullable=False, index=True)
    printer_name = Column(String(120), nullable=False, index=True)
    slot = Column(Integer, nullable=False, index=True)
    observed_brand = Column(String(120), nullable=True)
    observed_material = Column(String(80), nullable=True)
    observed_color = Column(String(80), nullable=True)
    source = Column(String(120), nullable=True)
    observed_at = Column(DateTime, default=_utcnow, index=True)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


class AppSetting(Base):
    __tablename__ = "app_settings"

    key = Column(String(80), primary_key=True, index=True)
    value = Column(String(255), nullable=False)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), nullable=False, unique=True, index=True)
    display_name = Column(String(120), nullable=True)
    password_hash = Column(String(255), nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


class UserSession(Base):
    __tablename__ = "user_sessions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    token_hash = Column(String(128), nullable=False, unique=True, index=True)
    user_agent = Column(String(255), nullable=True)
    ip_address = Column(String(120), nullable=True)
    expires_at = Column(DateTime, nullable=False, index=True)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


class UserApiToken(Base):
    __tablename__ = "user_api_tokens"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(120), nullable=False, default="default")
    token_hash = Column(String(128), nullable=False, unique=True, index=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=_utcnow)
    last_used_at = Column(DateTime, nullable=True)
