import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base, get_db
from app.main import app
import app.main as main_module
from app.models import Spool


class SpoolListHideEmptyTests(unittest.TestCase):
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

    def _seed_spools(self):
        with self.SessionLocal() as db:
            db.add_all(
                [
                    Spool(
                        brand="BrandFull",
                        material="PLA",
                        color="Blue",
                        weight_g=1000.0,
                        remaining_g=500.0,
                        in_use=False,
                        project=self.project_scope,
                    ),
                    Spool(
                        brand="BrandEmpty",
                        material="PLA",
                        color="Black",
                        weight_g=1000.0,
                        remaining_g=0.0,
                        in_use=False,
                        project=self.project_scope,
                    ),
                ]
            )
            db.commit()

    def test_spool_list_hides_empty_by_default(self):
        self._seed_spools()

        response = self.client.get("/spools?project=private")

        self.assertEqual(response.status_code, 200)
        self.assertIn("BrandFull", response.text)
        self.assertNotIn("BrandEmpty", response.text)

    def test_spool_list_can_show_empty_when_filter_disabled(self):
        self._seed_spools()

        response = self.client.get("/spools?project=private&hide_empty=false")

        self.assertEqual(response.status_code, 200)
        self.assertIn("BrandFull", response.text)
        self.assertIn("BrandEmpty", response.text)

    def test_create_spool_rejects_duplicate_ams_slot_mapping(self):
        with self.SessionLocal() as db:
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
                    ams_slot=4,
                )
            )
            db.commit()

        response = self.client.post(
            "/spools/new",
            data={
                "brand": "Bambu",
                "material": "PLA",
                "color": "Black",
                "weight_g": "1000",
                "remaining_g": "450",
                "price": "20",
                "location": "Shelf A",
                "ams_printer": "P1S-01",
                "ams_slot": "4",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            ("AMS slot conflict" in response.text) or ("AMS Slot-Konflikt" in response.text),
            "Expected AMS slot conflict message in EN or DE",
        )

        with self.SessionLocal() as db:
            spool_count = db.query(Spool).count()
            self.assertEqual(spool_count, 1)

    def test_spool_list_can_filter_by_lifecycle_status(self):
        with self.SessionLocal() as db:
            db.add_all(
                [
                    Spool(
                        brand="OpenedOne",
                        material="PLA",
                        color="White",
                        weight_g=1000.0,
                        remaining_g=500.0,
                        lifecycle_status="opened",
                        project=self.project_scope,
                    ),
                    Spool(
                        brand="ArchivedOne",
                        material="PETG",
                        color="Black",
                        weight_g=1000.0,
                        remaining_g=500.0,
                        lifecycle_status="archived",
                        project=self.project_scope,
                    ),
                ]
            )
            db.commit()

        response = self.client.get("/spools?project=private&hide_empty=false&lifecycle_status=opened")

        self.assertEqual(response.status_code, 200)
        self.assertIn("OpenedOne", response.text)
        self.assertNotIn("ArchivedOne", response.text)


if __name__ == "__main__":
    unittest.main()
