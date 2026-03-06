import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base, get_db
from app.main import app
import app.main as main_module
from app.models import Spool, UsageHistory, UsageBatchContext


class AnalysisPageTests(unittest.TestCase):
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

        now = main_module._utcnow()
        batch_id_slot = "analysis-batch-slot-1"
        with self.SessionLocal() as db:
            spool_1 = Spool(
                brand="Bambu",
                material="PLA",
                color="Black",
                weight_g=1000.0,
                remaining_g=120.0,
                low_stock_threshold_g=150.0,
                price=20.0,
                location="Rack A",
                ams_printer="P1S-01",
                ams_slot=1,
                project="private",
            )
            spool_2 = Spool(
                brand="Prusament",
                material="PETG",
                color="White",
                weight_g=1000.0,
                remaining_g=650.0,
                price=28.0,
                location="Rack B",
                ams_printer="P1S-01",
                ams_slot=2,
                project="private",
            )
            db.add_all([spool_1, spool_2])
            db.flush()

            db.add_all(
                [
                    UsageHistory(
                        mode="auto_3mf",
                        project="private",
                        batch_id=batch_id_slot,
                        spool_id=spool_1.id,
                        spool_material="PLA",
                        spool_color="Black",
                        deducted_g=30.0,
                        remaining_before_g=150.0,
                        remaining_after_g=120.0,
                        created_at=now - timedelta(days=2),
                    ),
                    UsageHistory(
                        mode="auto_3mf",
                        project="private",
                        batch_id="analysis-batch-slot-2",
                        spool_id=spool_2.id,
                        spool_material="PETG",
                        spool_color="White",
                        deducted_g=20.0,
                        remaining_before_g=670.0,
                        remaining_after_g=650.0,
                        created_at=now - timedelta(days=1),
                    ),
                    UsageHistory(
                        mode="manual",
                        project="private",
                        batch_id="analysis-batch-slot-3",
                        spool_id=spool_1.id,
                        spool_material="PLA",
                        spool_color="Black",
                        deducted_g=10.0,
                        remaining_before_g=130.0,
                        remaining_after_g=120.0,
                        created_at=now - timedelta(days=12),
                    ),
                ]
            )
            db.add_all(
                [
                    UsageBatchContext(
                        project="private",
                        batch_id=batch_id_slot,
                        printer_name=None,
                        ams_slots="1",
                    ),
                    UsageBatchContext(
                        project="private",
                        batch_id="analysis-batch-slot-2",
                        printer_name=None,
                        ams_slots="2",
                    ),
                    UsageBatchContext(
                        project="private",
                        batch_id="analysis-batch-slot-3",
                        printer_name=None,
                        ams_slots="1",
                    ),
                ]
            )
            db.commit()

    def tearDown(self):
        main_module.COOKIE_SECURE = self._orig_cookie_secure
        main_module.SessionLocal = self._orig_session_local
        app.dependency_overrides.clear()
        self.engine.dispose()
        self.temp_dir.cleanup()

    def test_analysis_page_renders_extended_sections(self):
        response = self.client.get("/analysis?project=private&lang=en&period_days=30&trend_months=6")
        self.assertEqual(response.status_code, 200)
        self.assertIn("analysis-usage-cost-chart", response.text)
        self.assertIn("analysis-top-material-chart", response.text)
        self.assertIn("analysis-printer-slot-chart", response.text)

    def test_api_analysis_usage_cost_trend(self):
        response = self.client.get("/api/analysis/usage-cost-trend?project=private&trend_months=6")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload.get("ok"))
        self.assertEqual(payload.get("trend_months"), 6)
        self.assertTrue(isinstance(payload.get("series"), list))
        self.assertGreaterEqual(len(payload.get("series")), 6)

    def test_api_analysis_top_usage_and_printer_slot(self):
        top_usage_response = self.client.get("/api/analysis/top-usage?project=private&group_by=material&period_days=30&limit=5")
        self.assertEqual(top_usage_response.status_code, 200)
        top_usage_payload = top_usage_response.json()
        self.assertTrue(top_usage_payload.get("ok"))
        names = {str(row.get("name")) for row in top_usage_payload.get("rows", [])}
        self.assertIn("PLA", names)

        printer_slot_response = self.client.get("/api/analysis/printer-slot-usage?project=private&period_days=30&limit=8")
        self.assertEqual(printer_slot_response.status_code, 200)
        printer_slot_payload = printer_slot_response.json()
        self.assertTrue(printer_slot_payload.get("ok"))
        self.assertTrue(any(row.get("printer") == "P1S-01" for row in printer_slot_payload.get("rows", [])))


if __name__ == "__main__":
    unittest.main()
