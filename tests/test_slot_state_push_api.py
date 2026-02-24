import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base, get_db
from app.main import app
import app.main as main_module
from app.models import DeviceSlotState


class SlotStatePushApiTests(unittest.TestCase):
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

    def test_push_slot_state_upserts_rows(self):
        payload = {
            "project": "private",
            "source": "local-slot-bridge",
            "printers": [
                {
                    "printer": "P1S-01",
                    "slots": [
                        {"slot": 1, "brand": "Bambu", "material": "PLA", "color": "Black"},
                        {"slot": 2, "brand": "Bambu", "material": "PETG", "color": "White"},
                    ],
                }
            ],
        }

        response = self.client.post("/api/slot-state/push", json=payload)
        self.assertEqual(response.status_code, 200)
        data = response.json()

        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("entries"), 2)
        self.assertEqual(data.get("updated"), 2)

        with self.SessionLocal() as db:
            rows = (
                db.query(DeviceSlotState)
                .filter(DeviceSlotState.project == self.project_scope, DeviceSlotState.printer_name == "P1S-01")
                .order_by(DeviceSlotState.slot.asc())
                .all()
            )
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0].slot, 1)
            self.assertEqual(rows[0].observed_material, "PLA")
            self.assertEqual(rows[1].slot, 2)
            self.assertEqual(rows[1].observed_material, "PETG")

    def test_push_slot_state_rejects_invalid_json(self):
        response = self.client.post(
            "/api/slot-state/push",
            content="{bad-json",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data.get("ok"))
        self.assertEqual(data.get("error"), "invalid_json")


if __name__ == "__main__":
    unittest.main()
