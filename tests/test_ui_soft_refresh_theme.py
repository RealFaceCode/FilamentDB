import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base, get_db
from app.main import app
import app.main as main_module
from app.models import Printer


class UiSoftRefreshThemeRegressionTests(unittest.TestCase):
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

    def test_base_soft_refresh_reapplies_overlay_state_and_scroll(self):
        response = self.client.get("/dashboard?project=private&lang=en")

        self.assertEqual(response.status_code, 200)
        self.assertIn("const windowScroll = {", response.text)
        self.assertIn("window.scrollTo(windowScroll.x, windowScroll.y)", response.text)
        self.assertIn("globalThis.__captureUiOverlayState", response.text)
        self.assertIn("globalThis.__applyUiOverlayState", response.text)
        self.assertIn("trigger.setAttribute('aria-controls', menuId)", response.text)

    def test_printers_page_has_dark_variants_for_status_badges(self):
        with self.SessionLocal() as db:
            db.add(
                Printer(
                    project="private",
                    name="P1S-Test",
                    serial="P1S-TEST-001",
                    is_active=True,
                    telemetry_progress=37.5,
                )
            )
            db.commit()

        response = self.client.get("/printers?project=private&lang=en")

        self.assertEqual(response.status_code, 200)
        self.assertIn("dark:bg-slate-800", response.text)
        self.assertIn("dark:border-slate-700", response.text)
        self.assertIn("dark:hover:border-slate-600", response.text)
        self.assertIn("dark:bg-slate-700", response.text)

    def test_slot_status_summary_has_dark_text_variants(self):
        response = self.client.get("/slot-status?project=private&lang=en")

        self.assertEqual(response.status_code, 200)
        self.assertIn("text-emerald-700 dark:text-emerald-300", response.text)
        self.assertIn("text-rose-700 dark:text-rose-300", response.text)
        self.assertIn("text-amber-700 dark:text-amber-300", response.text)
        self.assertIn("text-orange-700 dark:text-orange-300", response.text)

    def test_settings_popup_has_dark_theme_container_classes(self):
        response = self.client.get("/dashboard?project=private&lang=en")

        self.assertEqual(response.status_code, 200)
        self.assertIn("id=\"settings-popover-panel\"", response.text)
        self.assertIn("ui-card ui-card-sm", response.text)
        self.assertIn("dark:text-slate-100", response.text)


if __name__ == "__main__":
    unittest.main()
