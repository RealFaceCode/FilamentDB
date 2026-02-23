import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base, get_db
from app.main import app
import app.main as main_module
from app.models import Spool, User


class CustomLabelLayoutTests(unittest.TestCase):
    def setUp(self):
        self._orig_cookie_secure = main_module.COOKIE_SECURE
        main_module.COOKIE_SECURE = False

        self.temp_dir = tempfile.TemporaryDirectory()
        temp_root = Path(self.temp_dir.name)

        self.db_path = temp_root / "test_app.db"
        self.presets_path = temp_root / "presets.json"
        self.presets_path.write_text(json.dumps({}), encoding="utf-8")

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

        self.presets_patch = patch("app.main.PRESETS_PATH", self.presets_path)
        self.presets_patch.start()

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

        with self.SessionLocal() as db:
            spool = Spool(
                user_id=self.user_id,
                brand="Bambu",
                material="PLA",
                color="White",
                weight_g=1000.0,
                remaining_g=800.0,
                project=self.project_scope,
            )
            db.add(spool)
            db.commit()
            self.spool_id = spool.id

    def tearDown(self):
        main_module.COOKIE_SECURE = self._orig_cookie_secure
        main_module.SessionLocal = self._orig_session_local
        self.presets_patch.stop()
        app.dependency_overrides.clear()
        self.engine.dispose()
        self.temp_dir.cleanup()

    def test_can_add_and_use_custom_label_layout(self):
        create_response = self.client.post(
            "/labels/layouts",
            data={
                "layout_name": "My 3x6 Sheet",
                "cell_w_mm": "70.5",
                "cell_h_mm": "35.0",
            },
        )
        self.assertEqual(create_response.status_code, 200)
        self.assertIn("My 3x6 Sheet", create_response.text)

        labels_page = self.client.get("/labels")
        self.assertEqual(labels_page.status_code, 200)
        self.assertIn('value="my_3x6_sheet"', labels_page.text)

        print_page = self.client.post(
            "/labels",
            data={"layout": "my_3x6_sheet", "spool_ids": str(self.spool_id)},
        )
        self.assertEqual(print_page.status_code, 200)
        self.assertIn("--cell-w: 70.5mm;", print_page.text)
        self.assertIn("--cols: 2;", print_page.text)

    def test_custom_label_name_with_umlaut_is_accepted(self):
        create_response = self.client.post(
            "/labels/layouts",
            data={
                "layout_name": "Größe Spezial",
                "cell_w_mm": "68.0",
                "cell_h_mm": "30.0",
            },
        )
        self.assertEqual(create_response.status_code, 200)
        self.assertNotIn("Please enter a valid name.", create_response.text)
        self.assertNotIn("Bitte einen gültigen Namen angeben.", create_response.text)
        self.assertIn("Größe Spezial", create_response.text)


if __name__ == "__main__":
    unittest.main()
