import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base, get_db
from app.main import app
import app.main as main_module
from app.models import Spool, StorageArea, StorageSubLocation, User


class StorageLocationsQrTests(unittest.TestCase):
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

    def _seed_location(self, path_code: str = "R1/A1") -> StorageSubLocation:
        area_code, sub_code = path_code.split("/", 1)
        with self.SessionLocal() as db:
            area = StorageArea(user_id=self.user_id, project=self.project_scope, code=area_code)
            db.add(area)
            db.flush()
            location = StorageSubLocation(
                user_id=self.user_id,
                project=self.project_scope,
                area_id=area.id,
                code=sub_code,
                path_code=path_code,
            )
            db.add(location)
            db.commit()
            db.refresh(location)
            return location

    def test_create_storage_location_and_assign_on_spool_create(self):
        response = self.client.post(
            "/storage-locations",
            data={
                "area_code": "r1",
                "sub_code": "a1",
                "area_name": "Regal 1",
                "sub_name": "Fach 1",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("R1/A1", response.text)

        with self.SessionLocal() as db:
            location = db.query(StorageSubLocation).filter(StorageSubLocation.path_code == "R1/A1").first()
            self.assertIsNotNone(location)
            self.assertEqual(location.name, "Fach 1")
            location_id = location.id

        create_response = self.client.post(
            "/spools/new",
            data={
                "brand": "Bambu",
                "material": "PLA",
                "color": "White",
                "weight_g": "1000",
                "remaining_g": "800",
                "price": "25",
                "location": "",
                "storage_sub_location_id": str(location_id),
            },
            follow_redirects=False,
        )
        self.assertEqual(create_response.status_code, 303)

        with self.SessionLocal() as db:
            spool = db.query(Spool).first()
            self.assertIsNotNone(spool)
            self.assertEqual(spool.location, "R1/A1")
            self.assertEqual(spool.storage_sub_location_id, location_id)

    def test_qr_scan_location_redirects_to_filtered_spool_list(self):
        location = self._seed_location("R2/A4")

        with self.SessionLocal() as db:
            db.add_all(
                [
                    Spool(
                        user_id=self.user_id,
                        brand="A",
                        material="PLA",
                        color="Blue",
                        weight_g=1000.0,
                        remaining_g=500.0,
                        project=self.project_scope,
                        location="R2/A4",
                        storage_sub_location_id=location.id,
                    ),
                    Spool(
                        user_id=self.user_id,
                        brand="B",
                        material="PETG",
                        color="Black",
                        weight_g=1000.0,
                        remaining_g=500.0,
                        project=self.project_scope,
                        location="R9/A9",
                    ),
                ]
            )
            db.commit()

        scan_response = self.client.post(
            "/qr-scan",
            data={"qr_payload": f"location:{self.project_scope}:R2/A4"},
            follow_redirects=False,
        )
        self.assertEqual(scan_response.status_code, 303)
        self.assertIn(f"location_id={location.id}", scan_response.headers.get("location", ""))

        list_response = self.client.get(f"/spools?location_id={location.id}&hide_empty=false")
        self.assertEqual(list_response.status_code, 200)
        self.assertIn("R2/A4", list_response.text)
        self.assertNotIn("R9/A9", list_response.text)

    def test_storage_location_qr_endpoint_returns_png(self):
        location = self._seed_location("R3/A7")

        response = self.client.get(f"/storage-locations/{location.id}/qr")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("content-type"), "image/png")
        self.assertTrue(response.content.startswith(b"\x89PNG"))

    def test_storage_location_accepts_flexible_codes(self):
        response = self.client.post(
            "/storage-locations",
            data={
                "area_code": "regal-1",
                "sub_code": "fach_a9",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("REGAL-1/FACH_A9", response.text)

        scan_response = self.client.post(
            "/qr-scan",
            data={"qr_payload": f"location:{self.project_scope}:regal-1/fach_a9"},
            follow_redirects=False,
        )
        self.assertEqual(scan_response.status_code, 303)
        self.assertIn("location_id=", scan_response.headers.get("location", ""))

    def test_can_print_storage_location_labels_via_labels_route(self):
        location = self._seed_location("R1/A1")

        response = self.client.post(
            "/labels",
            data={
                "label_target": "location",
                "storage_location_ids": str(location.id),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(f"/storage-locations/{location.id}/qr", response.text)
        self.assertIn("R1/A1", response.text)

    def test_bulk_add_assigns_categorized_storage_location(self):
        location = self._seed_location("R5/B2")

        response = self.client.post(
            "/spools/bulk",
            data={
                "brand": ["Bambu"],
                "material": ["PLA"],
                "color": ["White"],
                "weight_g": ["1000"],
                "remaining_g": ["900"],
                "price": ["20"],
                "storage_sub_location_id": [str(location.id)],
                "quantity": ["2"],
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)

        with self.SessionLocal() as db:
            rows = db.query(Spool).order_by(Spool.id.asc()).all()
            self.assertEqual(len(rows), 2)
            for spool in rows:
                self.assertEqual(spool.storage_sub_location_id, location.id)
                self.assertEqual(spool.location, "R5/B2")


if __name__ == "__main__":
    unittest.main()
