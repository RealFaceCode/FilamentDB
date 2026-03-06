import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base, get_db
from app.main import app
import app.main as main_module
from app.models import Spool, UsageHistory


class UsageUndoCapacityTests(unittest.TestCase):
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

    def test_undo_does_not_exceed_spool_capacity(self):
        with self.SessionLocal() as db:
            spool = Spool(
                brand="Bambu",
                material="PLA",
                color="Black",
                weight_g=1000.0,
                remaining_g=990.0,
                in_use=True,
                project=self.project_scope,
            )
            db.add(spool)
            db.flush()

            usage = UsageHistory(
                actor="127.0.0.1",
                mode="save_manual",
                batch_id="undo-capacity-test",
                source_file="test.3mf",
                project=self.project_scope,
                spool_id=spool.id,
                spool_brand=spool.brand,
                spool_material=spool.material,
                spool_color=spool.color,
                deducted_g=25.0,
                remaining_before_g=1015.0,
                remaining_after_g=990.0,
                undone=False,
            )
            db.add(usage)
            db.commit()

        response = self.client.post(
            "/usage?project=private&lang=de",
            data={"action": "undo_last"},
        )

        self.assertEqual(response.status_code, 200)

        with self.SessionLocal() as db:
            spool = db.query(Spool).filter(Spool.project == self.project_scope).first()
            usage = db.query(UsageHistory).filter(UsageHistory.batch_id == "undo-capacity-test").first()

            self.assertIsNotNone(spool)
            self.assertEqual(float(spool.remaining_g), 1000.0)
            self.assertIsNotNone(usage)
            self.assertTrue(bool(usage.undone))


if __name__ == "__main__":
    unittest.main()
