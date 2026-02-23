import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base, get_db
from app.main import app
import app.main as main_module
from app.models import Spool, User


class QrScanLifecycleTests(unittest.TestCase):
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

            spool = Spool(
                user_id=self.user_id,
                brand="Bambu",
                material="PLA",
                color="Black",
                weight_g=1000.0,
                remaining_g=800.0,
                lifecycle_status="new",
                project=self.project_scope,
            )
            db.add(spool)
            db.commit()
            self.spool_id = int(spool.id)

    def tearDown(self):
        main_module.COOKIE_SECURE = self._orig_cookie_secure
        main_module.SessionLocal = self._orig_session_local
        app.dependency_overrides.clear()
        self.engine.dispose()
        self.temp_dir.cleanup()

    def test_qr_scan_action_can_update_lifecycle_status(self):
        response = self.client.post(
            "/qr-scan/action",
            data={
                "spool_id": str(self.spool_id),
                "action": "set_lifecycle",
                "lifecycle_status": "humidity_risk",
                "return_to_scan": "0",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Feuchterisiko", response.text)

        with self.SessionLocal() as db:
            spool = db.query(Spool).filter(Spool.id == self.spool_id).first()
            self.assertIsNotNone(spool)
            self.assertEqual(spool.lifecycle_status, "humidity_risk")


if __name__ == "__main__":
    unittest.main()
