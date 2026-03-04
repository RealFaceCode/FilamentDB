from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, UniqueConstraint, ForeignKey, Text

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
    project = Column(String(40), nullable=False, index=True)
    batch_id = Column(String(64), nullable=False, index=True)
    printer_name = Column(String(120), nullable=True)
    printer_serial = Column(String(120), nullable=True, index=True)
    ams_slots = Column(String(255), nullable=True)


class DeviceSlotState(Base):
    __tablename__ = "device_slot_state"
    __table_args__ = (UniqueConstraint("project", "printer_name", "slot", name="uq_device_slot_state_project_printer_slot"),)

    id = Column(Integer, primary_key=True, index=True)
    project = Column(String(40), nullable=False, index=True)
    printer_name = Column(String(120), nullable=False, index=True)
    printer_serial = Column(String(120), nullable=True, index=True)
    slot = Column(Integer, nullable=False, index=True)
    ams_unit = Column(Integer, nullable=True, index=True)
    slot_local = Column(Integer, nullable=True, index=True)
    ams_name = Column(String(120), nullable=True)
    observed_brand = Column(String(120), nullable=True)
    observed_material = Column(String(80), nullable=True)
    observed_color = Column(String(80), nullable=True)
    source = Column(String(120), nullable=True)
    observed_at = Column(DateTime, default=_utcnow, index=True)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


class Printer(Base):
    __tablename__ = "printers"
    __table_args__ = (
        UniqueConstraint("project", "serial", name="uq_printers_project_serial"),
        UniqueConstraint("project", "name", name="uq_printers_project_name"),
    )

    id = Column(Integer, primary_key=True, index=True)
    project = Column(String(40), nullable=False, index=True)
    serial = Column(String(120), nullable=False, index=True)
    name = Column(String(120), nullable=False, index=True)
    host = Column(String(255), nullable=True)
    access_code = Column(String(120), nullable=True)
    ams_name_map = Column(String(500), nullable=True)
    port = Column(Integer, nullable=False, default=8883)
    is_active = Column(Boolean, nullable=False, default=True, index=True)
    status = Column(String(32), nullable=True, index=True)
    last_seen_at = Column(DateTime, nullable=True, index=True)
    last_source = Column(String(120), nullable=True)
    telemetry_job_name = Column(String(255), nullable=True)
    telemetry_job_status = Column(String(80), nullable=True)
    telemetry_progress = Column(Float, nullable=True)
    telemetry_nozzle_temp = Column(Float, nullable=True)
    telemetry_bed_temp = Column(Float, nullable=True)
    telemetry_chamber_temp = Column(Float, nullable=True)
    telemetry_firmware = Column(String(120), nullable=True)
    telemetry_error = Column(String(255), nullable=True)
    telemetry_external_spool_active = Column(Boolean, nullable=True)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


class AppSetting(Base):
    __tablename__ = "app_settings"

    key = Column(String(80), primary_key=True, index=True)
    value = Column(String(255), nullable=False)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime, default=_utcnow, index=True)
    project = Column(String(40), nullable=False, index=True)
    actor = Column(String(120), nullable=True)
    action = Column(String(80), nullable=False, index=True)
    entity_type = Column(String(80), nullable=True, index=True)
    entity_id = Column(String(120), nullable=True, index=True)
    details_json = Column(Text, nullable=True)


class ImportMappingProfile(Base):
    __tablename__ = "import_mapping_profiles"
    __table_args__ = (UniqueConstraint("project", "name", name="uq_import_mapping_profiles_project_name"),)

    id = Column(Integer, primary_key=True, index=True)
    project = Column(String(40), nullable=False, index=True)
    name = Column(String(120), nullable=False, index=True)
    mapping_json = Column(Text, nullable=False)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)
