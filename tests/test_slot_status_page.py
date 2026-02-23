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
from app.models import DeviceSlotState, Spool, User


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

        self.client.post(
            "/auth/register",
            data={"name": "Tester", "email": "tester@example.com", "password": "password123"},
            follow_redirects=False,
        )
        with self.SessionLocal() as db:
            user = db.query(User).filter(User.email == "tester@example.com").first()
            self.assertIsNotNone(user)
            self.user_id = int(user.id)
            self.project_scope = f"u{user.id}_private"

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
                        user_id=self.user_id,
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
                        user_id=self.user_id,
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
                        user_id=self.user_id,
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
                        user_id=self.user_id,
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


if __name__ == "__main__":
    unittest.main()
