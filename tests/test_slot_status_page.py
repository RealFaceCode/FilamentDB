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
from app.models import DeviceSlotState, Spool


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


if __name__ == "__main__":
    unittest.main()
