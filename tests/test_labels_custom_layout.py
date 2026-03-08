import json
import re
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
from app.models import Spool


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

        self.project_scope = "private"

        with self.SessionLocal() as db:
            spool = Spool(
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

    def test_can_delete_custom_label_layout(self):
        create_response = self.client.post(
            "/labels/layouts",
            data={
                "layout_name": "Delete Me",
                "cell_w_mm": "70.0",
                "cell_h_mm": "35.0",
            },
        )
        self.assertEqual(create_response.status_code, 200)
        self.assertIn("Delete Me", create_response.text)

        delete_response = self.client.post(
            "/labels/layouts/delete",
            data={"layout_key": "delete_me"},
        )
        self.assertEqual(delete_response.status_code, 200)
        self.assertNotIn("Delete Me", delete_response.text)

        labels_page = self.client.get("/labels")
        self.assertEqual(labels_page.status_code, 200)
        self.assertNotIn('value="delete_me"', labels_page.text)

    def test_can_add_custom_layout_when_presets_file_is_read_only(self):
        with patch("app.main.save_presets", side_effect=PermissionError("read-only")):
            create_response = self.client.post(
                "/labels/layouts",
                data={
                    "layout_name": "DB Only Layout",
                    "cell_w_mm": "71.0",
                    "cell_h_mm": "34.0",
                },
            )

        self.assertEqual(create_response.status_code, 200)
        self.assertIn("DB Only Layout", create_response.text)

        labels_page = self.client.get("/labels")
        self.assertEqual(labels_page.status_code, 200)
        self.assertIn('value="db_only_layout"', labels_page.text)

    def test_can_delete_custom_layout_when_presets_file_is_read_only(self):
        create_response = self.client.post(
            "/labels/layouts",
            data={
                "layout_name": "Delete RO",
                "cell_w_mm": "73.0",
                "cell_h_mm": "36.0",
            },
        )
        self.assertEqual(create_response.status_code, 200)
        self.assertIn("Delete RO", create_response.text)

        with patch("app.main.save_presets", side_effect=PermissionError("read-only")):
            delete_response = self.client.post(
                "/labels/layouts/delete",
                data={"layout_key": "delete_ro"},
            )

        self.assertEqual(delete_response.status_code, 200)
        self.assertNotIn("Delete RO", delete_response.text)

        labels_page = self.client.get("/labels")
        self.assertEqual(labels_page.status_code, 200)
        self.assertNotIn('value="delete_ro"', labels_page.text)

    def test_label_content_can_remain_fully_unchecked(self):
        response = self.client.post(
            "/labels/preferences",
            data={
                "label_target": "spool",
                "layout": "a4_3x8_63_5x33_9",
                "print_mode": "sheet",
                "label_orientation": "horizontal",
            },
        )
        self.assertEqual(response.status_code, 200)

        _ = self.client.get("/thresholds")
        labels_page = self.client.get("/labels")
        self.assertEqual(labels_page.status_code, 200)

        self.assertNotRegex(labels_page.text, r'name="show_spool_id"[^>]*\schecked')
        self.assertNotRegex(labels_page.text, r'name="show_brand"[^>]*\schecked')
        self.assertNotRegex(labels_page.text, r'name="show_material_color"[^>]*\schecked')
        self.assertNotRegex(labels_page.text, r'name="show_weight"[^>]*\schecked')
        self.assertNotRegex(labels_page.text, r'name="show_remaining"[^>]*\schecked')
        self.assertNotRegex(labels_page.text, r'name="show_location"[^>]*\schecked')

    def test_label_print_includes_preview_url_for_soft_refresh(self):
        print_page = self.client.post(
            "/labels",
            data={
                "label_target": "spool",
                "layout": "a4_3x8_63_5x33_9",
                "print_mode": "sheet",
                "label_orientation": "horizontal",
                "show_spool_id": "1",
                "spool_ids": str(self.spool_id),
            },
        )
        self.assertEqual(print_page.status_code, 200)
        self.assertIn('const LABEL_PREVIEW_URL = "/labels?preview=1', print_page.text)
        self.assertIn(f"spool_ids={self.spool_id}", print_page.text)

        preview_page = self.client.get(
            f"/labels?preview=1&target=spool&view=spool&layout=a4_3x8_63_5x33_9&print_mode=sheet&label_orientation=horizontal&show_spool_id=1&spool_ids={self.spool_id}"
        )
        self.assertEqual(preview_page.status_code, 200)
        self.assertIn("print-sheet", preview_page.text)
        self.assertNotIn('id="labels-page"', preview_page.text)

    def test_label_print_qr_cap_does_not_expand_to_legacy_oversize(self):
        print_page = self.client.post(
            "/labels",
            data={
                "label_target": "spool",
                "layout": "a4_3x8_63_5x33_9",
                "print_mode": "single",
                "label_orientation": "horizontal",
                "spool_ids": str(self.spool_id),
            },
        )
        self.assertEqual(print_page.status_code, 200)
        self.assertNotIn("--qr-size: 80mm", print_page.text)
        self.assertIn("data-max-font=", print_page.text)

    def test_labels_form_disables_hidden_content_inputs(self):
        response = self.client.get("/labels")
        self.assertEqual(response.status_code, 200)
        self.assertIn("function setContentPanelInputsEnabled(panel, enabled)", response.text)
        self.assertIn("field.disabled = !enabled;", response.text)

    def test_label_print_contains_dynamic_page_margin_and_hover_size_tooltip(self):
        print_page = self.client.post(
            "/labels",
            data={
                "label_target": "spool",
                "layout": "a4_2x8_99_1x33_9",
                "print_mode": "sheet",
                "label_orientation": "horizontal",
                "spool_ids": str(self.spool_id),
            },
            follow_redirects=True,
        )
        self.assertEqual(print_page.status_code, 200)
        self.assertIn("margin: 8mm", print_page.text)
        self.assertIn("id=\"label-size-tooltip\"", print_page.text)
        self.assertIn("data-label-size=", print_page.text)

    def test_single_print_mode_has_full_page_css_guard(self):
        css_response = self.client.get("/static/css/labels_print.css")
        self.assertEqual(css_response.status_code, 200)
        self.assertIn(".print-sheet.is-single", css_response.text)
        self.assertIn("position: fixed !important;", css_response.text)
        self.assertIn("inset: 0 !important;", css_response.text)
        self.assertIn("grid-template-columns: minmax(0, 1fr) !important;", css_response.text)


if __name__ == "__main__":
    unittest.main()
