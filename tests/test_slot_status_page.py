import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base, get_db
from app.main import app
import app.main as main_module
from app.models import DeviceSlotState, Printer, Spool, UsageBatchContext


class SlotStatusPageTests(unittest.TestCase):
    def setUp(self):
        self._orig_cookie_secure = main_module.COOKIE_SECURE
        main_module.COOKIE_SECURE = False

        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_app.db"
        self.engine = create_engine(
            f"sqlite:///{self.db_path.as_posix()}",
            connect_args={"check_same_thread": False},
        )
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        self._orig_session_local = main_module.SessionLocal
        main_module.SessionLocal = self.SessionLocal
        Base.metadata.create_all(bind=self.engine)

        def override_get_db():
            db = self.SessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app, base_url="https://testserver")

        self.project_scope = "private"

    def tearDown(self):
        main_module.COOKIE_SECURE = self._orig_cookie_secure
        main_module.SessionLocal = self._orig_session_local
        app.dependency_overrides.clear()
        self.engine.dispose()
        self.temp_dir.cleanup()

    def test_slot_status_page_shows_mismatch_and_ok(self):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with self.SessionLocal() as db:
            db.add_all(
                [
                    Spool(
                        brand="Bambu",
                        material="PLA",
                        color="Black",
                        weight_g=1000.0,
                        remaining_g=500.0,
                        in_use=True,
                        project=self.project_scope,
                        ams_printer="P1S-01",
                        ams_slot=1,
                    ),
                    Spool(
                        brand="Bambu",
                        material="PETG",
                        color="White",
                        weight_g=1000.0,
                        remaining_g=400.0,
                        in_use=True,
                        project=self.project_scope,
                        ams_printer="P1S-01",
                        ams_slot=2,
                    ),
                    DeviceSlotState(
                        project=self.project_scope,
                        printer_name="P1S-01",
                        slot=1,
                        observed_brand="Bambu",
                        observed_material="PLA",
                        observed_color="Black",
                        source="test",
                        observed_at=now,
                    ),
                    DeviceSlotState(
                        project=self.project_scope,
                        printer_name="P1S-01",
                        slot=2,
                        observed_brand="Bambu",
                        observed_material="PETG",
                        observed_color="Red",
                        source="test",
                        observed_at=now - timedelta(minutes=1),
                    ),
                ]
            )
            db.commit()

        response = self.client.get("/slot-status?project=private&lang=en")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Expected/Live slot status", response.text)
        self.assertIn("Mismatch", response.text)
        self.assertIn("OK", response.text)

    def test_slot_status_page_includes_live_slots_without_mapping_for_multiple_printers(self):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with self.SessionLocal() as db:
            db.add_all(
                [
                    DeviceSlotState(
                        project=self.project_scope,
                        printer_name="X1C-A",
                        slot=1,
                        observed_brand="Bambu",
                        observed_material="PLA",
                        observed_color="Black",
                        source="test",
                        observed_at=now,
                    ),
                    DeviceSlotState(
                        project=self.project_scope,
                        printer_name="X1C-A",
                        slot=2,
                        observed_brand="Bambu",
                        observed_material="PLA",
                        observed_color="White",
                        source="test",
                        observed_at=now,
                    ),
                    DeviceSlotState(
                        project=self.project_scope,
                        printer_name="X1C-A",
                        slot=3,
                        observed_brand="Bambu",
                        observed_material="PETG",
                        observed_color="Blue",
                        source="test",
                        observed_at=now,
                    ),
                    DeviceSlotState(
                        project=self.project_scope,
                        printer_name="X1C-A",
                        slot=4,
                        observed_brand="Bambu",
                        observed_material="PETG",
                        observed_color="Red",
                        source="test",
                        observed_at=now,
                    ),
                    DeviceSlotState(
                        project=self.project_scope,
                        printer_name="X1C-A",
                        slot=5,
                        observed_brand="Bambu",
                        observed_material="PLA",
                        observed_color="Green",
                        source="test",
                        observed_at=now,
                    ),
                    DeviceSlotState(
                        project=self.project_scope,
                        printer_name="X1C-A",
                        slot=6,
                        observed_brand="Bambu",
                        observed_material="ABS",
                        observed_color="Gray",
                        source="test",
                        observed_at=now,
                    ),
                    DeviceSlotState(
                        project=self.project_scope,
                        printer_name="P1S-B",
                        slot=1,
                        observed_brand="Bambu",
                        observed_material="PLA",
                        observed_color="Black",
                        source="test",
                        observed_at=now,
                    ),
                ]
            )
            db.commit()

        response = self.client.get("/slot-status?project=private&lang=en")

        self.assertEqual(response.status_code, 200)
        self.assertIn("X1C-A", response.text)
        self.assertIn("P1S-B", response.text)
        self.assertIn("<td class=\"ui-td\">6</td>", response.text)

    def test_slot_status_page_shows_stale_live_data_badge(self):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        stale_observed = now - timedelta(minutes=int(main_module.SLOT_STATE_STALE_MINUTES) + 2)
        with self.SessionLocal() as db:
            db.add(
                DeviceSlotState(
                    project=self.project_scope,
                    printer_name="P1S-Stale",
                    slot=1,
                    observed_brand="Bambu",
                    observed_material="PLA",
                    observed_color="Black",
                    source="test",
                    observed_at=stale_observed,
                )
            )
            db.commit()

        response = self.client.get("/slot-status?project=private&lang=en")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Live data status", response.text)
        self.assertIn("Stale", response.text)

    def test_slot_status_uses_printer_ams_name_mapping_labels(self):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with self.SessionLocal() as db:
            db.add(
                Printer(
                    project=self.project_scope,
                    name="P1S-01",
                    serial="P1S-01",
                    is_active=True,
                    ams_name_map="1=Linkes AMS",
                )
            )
            db.add(
                Spool(
                    brand="Bambu",
                    material="PLA",
                    color="Black",
                    weight_g=1000.0,
                    remaining_g=500.0,
                    in_use=True,
                    project=self.project_scope,
                    ams_printer="P1S-01",
                    ams_slot=1,
                )
            )
            db.add(
                DeviceSlotState(
                    project=self.project_scope,
                    printer_name="P1S-01",
                    slot=1,
                    ams_unit=1,
                    slot_local=1,
                    observed_brand="Bambu",
                    observed_material="PLA",
                    observed_color="Black",
                    source="test",
                    observed_at=now,
                )
            )
            db.commit()

        response = self.client.get("/slot-status?project=private&lang=en")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Linkes AMS · S1", response.text)

    def test_slot_status_migrate_slot_format_endpoint(self):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with self.SessionLocal() as db:
            spool = Spool(
                brand="Bambu",
                material="PLA",
                color="Black",
                weight_g=1000.0,
                remaining_g=500.0,
                in_use=True,
                project=self.project_scope,
                ams_printer="P1S-01",
                ams_slot=1,
            )
            db.add(spool)
            db.add(
                DeviceSlotState(
                    project=self.project_scope,
                    printer_name="P1S-01",
                    slot=1,
                    ams_unit=1,
                    slot_local=1,
                    observed_brand="Bambu",
                    observed_material="PLA",
                    observed_color="Black",
                    source="test",
                    observed_at=now,
                )
            )
            db.add(
                UsageBatchContext(
                    project=self.project_scope,
                    batch_id="legacy-slot-batch",
                    printer_name="P1S-01",
                    ams_slots="1,2",
                )
            )
            db.commit()
            spool_id = int(spool.id)

        response = self.client.post("/slot-status/migrate-slot-format?project=private&lang=en")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Slot format migrated", response.text)

        with self.SessionLocal() as db:
            spool = db.query(Spool).filter(Spool.id == spool_id).first()
            self.assertIsNotNone(spool)
            self.assertEqual(int(spool.ams_slot or 0), 101)

            state = (
                db.query(DeviceSlotState)
                .filter(DeviceSlotState.project == self.project_scope, DeviceSlotState.printer_name == "P1S-01")
                .first()
            )
            self.assertIsNotNone(state)
            self.assertEqual(int(state.slot or 0), 101)

            context = (
                db.query(UsageBatchContext)
                .filter(UsageBatchContext.project == self.project_scope, UsageBatchContext.batch_id == "legacy-slot-batch")
                .first()
            )
            self.assertIsNotNone(context)
            self.assertEqual(context.ams_slots, "101,102")

    def test_slot_status_remap_updates_unique_material_color_mapping(self):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with self.SessionLocal() as db:
            spool_a = Spool(
                brand="Bambu",
                material="PLA",
                color="Black",
                weight_g=1000.0,
                remaining_g=500.0,
                in_use=True,
                project=self.project_scope,
                ams_printer="P1S-01",
                ams_slot=1,
            )
            spool_b = Spool(
                brand="Bambu",
                material="PETG",
                color="White",
                weight_g=1000.0,
                remaining_g=500.0,
                in_use=True,
                project=self.project_scope,
                ams_printer="P1S-01",
                ams_slot=2,
            )
            db.add_all(
                [
                    spool_a,
                    spool_b,
                    DeviceSlotState(
                        project=self.project_scope,
                        printer_name="P1S-01",
                        slot=201,
                        ams_unit=2,
                        slot_local=1,
                        observed_brand="Bambu",
                        observed_material="PLA",
                        observed_color="Black",
                        source="test",
                        observed_at=now,
                    ),
                    DeviceSlotState(
                        project=self.project_scope,
                        printer_name="P1S-01",
                        slot=2,
                        ams_unit=1,
                        slot_local=2,
                        observed_brand="Bambu",
                        observed_material="PETG",
                        observed_color="White",
                        source="test",
                        observed_at=now,
                    ),
                ]
            )
            db.commit()
            spool_a_id = int(spool_a.id)
            spool_b_id = int(spool_b.id)

        response = self.client.post("/slot-status/remap-ams?project=private&lang=en")
        self.assertEqual(response.status_code, 200)
        self.assertIn("AMS mapping updated: 1 spool(s).", response.text)

        with self.SessionLocal() as db:
            spool_a = db.query(Spool).filter(Spool.id == spool_a_id).first()
            spool_b = db.query(Spool).filter(Spool.id == spool_b_id).first()
            self.assertIsNotNone(spool_a)
            self.assertIsNotNone(spool_b)
            self.assertEqual(int(spool_a.ams_slot or 0), 201)
            self.assertEqual(int(spool_b.ams_slot or 0), 2)

    def test_slot_status_remap_skips_ambiguous_duplicates(self):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with self.SessionLocal() as db:
            spool_a = Spool(
                brand="Bambu",
                material="PLA",
                color="Black",
                weight_g=1000.0,
                remaining_g=500.0,
                in_use=True,
                project=self.project_scope,
                ams_printer="P1S-01",
                ams_slot=1,
            )
            spool_b = Spool(
                brand="Bambu",
                material="PLA",
                color="Black",
                weight_g=1000.0,
                remaining_g=500.0,
                in_use=True,
                project=self.project_scope,
                ams_printer="P1S-01",
                ams_slot=2,
            )
            db.add_all(
                [
                    spool_a,
                    spool_b,
                    DeviceSlotState(
                        project=self.project_scope,
                        printer_name="P1S-01",
                        slot=201,
                        ams_unit=2,
                        slot_local=1,
                        observed_brand="Bambu",
                        observed_material="PLA",
                        observed_color="Black",
                        source="test",
                        observed_at=now,
                    ),
                    DeviceSlotState(
                        project=self.project_scope,
                        printer_name="P1S-01",
                        slot=202,
                        ams_unit=2,
                        slot_local=2,
                        observed_brand="Bambu",
                        observed_material="PLA",
                        observed_color="Black",
                        source="test",
                        observed_at=now,
                    ),
                ]
            )
            db.commit()
            spool_a_id = int(spool_a.id)
            spool_b_id = int(spool_b.id)

        response = self.client.post("/slot-status/remap-ams?project=private&lang=en")
        self.assertEqual(response.status_code, 200)
        self.assertIn("No unambiguous corrections found.", response.text)

        with self.SessionLocal() as db:
            spool_a = db.query(Spool).filter(Spool.id == spool_a_id).first()
            spool_b = db.query(Spool).filter(Spool.id == spool_b_id).first()
            self.assertIsNotNone(spool_a)
            self.assertIsNotNone(spool_b)
            self.assertEqual(int(spool_a.ams_slot or 0), 1)
            self.assertEqual(int(spool_b.ams_slot or 0), 2)


if __name__ == "__main__":
    unittest.main()
