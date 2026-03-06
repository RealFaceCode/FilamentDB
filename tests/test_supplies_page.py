import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base, get_db
from app.main import app
import app.main as main_module
from app.models import SupplyCategory, SupplyItem, StorageArea, StorageSubLocation


class SuppliesPageTests(unittest.TestCase):
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

    def tearDown(self):
        main_module.COOKIE_SECURE = self._orig_cookie_secure
        main_module.SessionLocal = self._orig_session_local
        app.dependency_overrides.clear()
        self.engine.dispose()
        self.temp_dir.cleanup()

    def _seed_location(self, path_code: str = "R1/A1") -> StorageSubLocation:
        area_code, sub_code = path_code.split("/", 1)
        with self.SessionLocal() as db:
            area = StorageArea(project="private", code=area_code)
            db.add(area)
            db.flush()
            location = StorageSubLocation(
                project="private",
                area_id=area.id,
                code=sub_code,
                path_code=path_code,
            )
            db.add(location)
            db.commit()
            db.refresh(location)
            return location

    def test_create_supply_category(self):
        response = self.client.post(
            "/supplies/categories?project=private&lang=en",
            data={"name": "Adhesive"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Category saved.", response.text)

        with self.SessionLocal() as db:
            row = db.query(SupplyCategory).filter(SupplyCategory.project == "private").first()
            self.assertIsNotNone(row)
            self.assertEqual(row.name, "Adhesive")

    def test_create_supply_item_and_render_low_stock(self):
        location = self._seed_location("R2/B4")
        response = self.client.post(
            "/supplies?project=private&lang=en",
            data={
                "name": "Glue Stick",
                "category": "Adhesive",
                "storage_sub_location_id": str(location.id),
                "quantity": "2",
                "unit": "pcs",
                "min_quantity": "5",
                "notes": "3D print bed",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Supplies", response.text)
        self.assertIn("Glue Stick", response.text)
        self.assertIn("Low stock", response.text)

        with self.SessionLocal() as db:
            row = db.query(SupplyItem).first()
            self.assertIsNotNone(row)
            self.assertEqual(row.name, "Glue Stick")
            self.assertEqual(row.project, "private")
            self.assertEqual(row.location, "R2/B4")

    def test_update_adjust_and_delete_supply_item(self):
        with self.SessionLocal() as db:
            db.add(
                SupplyItem(
                    project="private",
                    name="Nozzle 0.4",
                    category="Parts",
                    quantity=3.0,
                    unit="pcs",
                )
            )
            db.commit()
            row = db.query(SupplyItem).first()
            supply_id = int(row.id)

        update_response = self.client.post(
            f"/supplies/{supply_id}/update?project=private&lang=en",
            data={
                "name": "Nozzle 0.6",
                "category": "Parts",
                "quantity": "4",
                "unit": "pcs",
                "min_quantity": "2",
                "notes": "Hardened",
            },
        )
        self.assertEqual(update_response.status_code, 200)
        self.assertIn("Item updated.", update_response.text)

        with self.SessionLocal() as db:
            row = db.query(SupplyItem).filter(SupplyItem.id == supply_id).first()
            self.assertIsNotNone(row)
            self.assertEqual(row.name, "Nozzle 0.6")
            self.assertEqual(float(row.quantity), 4.0)

        adjust_response = self.client.post(
            f"/supplies/{supply_id}/adjust?project=private&lang=en",
            data={"delta_quantity": "-1"},
        )
        self.assertEqual(adjust_response.status_code, 200)
        self.assertIn("Stock was adjusted.", adjust_response.text)

        with self.SessionLocal() as db:
            row = db.query(SupplyItem).filter(SupplyItem.id == supply_id).first()
            self.assertIsNotNone(row)
            self.assertEqual(float(row.quantity), 3.0)

        delete_response = self.client.post(
            f"/supplies/{supply_id}/delete?project=private&lang=en",
        )
        self.assertEqual(delete_response.status_code, 200)
        self.assertIn("Item deleted.", delete_response.text)

        with self.SessionLocal() as db:
            row = db.query(SupplyItem).filter(SupplyItem.id == supply_id).first()
            self.assertIsNone(row)


if __name__ == "__main__":
    unittest.main()
