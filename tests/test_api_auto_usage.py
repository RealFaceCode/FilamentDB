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
from app.models import Spool, UsageHistory, UsageBatchContext, User


class AutoUsageApiTests(unittest.TestCase):
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

        with self.SessionLocal() as db:
            spool = Spool(
                user_id=self.user_id,
                brand="Bambu",
                material="PLA",
                color="Schwarz",
                weight_g=1000.0,
                remaining_g=200.0,
                price=20.0,
                in_use=True,
                project=self.project_scope,
            )
            db.add(spool)
            db.commit()

    def tearDown(self):
        main_module.COOKIE_SECURE = self._orig_cookie_secure
        main_module.SessionLocal = self._orig_session_local
        app.dependency_overrides.clear()
        self.engine.dispose()
        self.temp_dir.cleanup()

    @staticmethod
    def _parser_result(grams: float = 25.0):
        return (
            grams,
            8000.0,
            {"__bambu_total_cost__": "1.23"},
            {"materials": ["PLA"], "colors": ["Schwarz"], "brands": ["Bambu"]},
            [{"material": "PLA", "grams": grams}],
        )

    def test_auto_usage_dry_run_does_not_change_db(self):
        with patch("app.main.parse_3mf_filament_usage", return_value=self._parser_result(25.0)):
            response = self.client.post(
                "/api/usage/auto-from-3mf",
                data={"project": "private", "dry_run": "1", "job_id": "job-dryrun-1"},
                files={"file": ("print.3mf", b"dummy-3mf-content", "application/octet-stream")},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload.get("ok"))
        self.assertTrue(payload.get("dry_run"))
        self.assertEqual(payload.get("changed_spools"), 1)

        with self.SessionLocal() as db:
            spool = db.query(Spool).filter(Spool.project == self.project_scope).first()
            history_count = db.query(UsageHistory).count()

            self.assertIsNotNone(spool)
            self.assertEqual(float(spool.remaining_g), 200.0)
            self.assertEqual(history_count, 0)

    def test_auto_usage_job_id_is_idempotent(self):
        with self.SessionLocal() as db:
            primary_spool = db.query(Spool).filter(Spool.project == self.project_scope).first()
            self.assertIsNotNone(primary_spool)
            primary_spool.ams_printer = "P1S-01"
            primary_spool.ams_slot = 2
            db.commit()

        parser_result = (
            30.0,
            8000.0,
            {"__bambu_total_cost__": "1.23"},
            {"materials": ["PLA"], "colors": ["Schwarz"], "brands": ["Bambu"]},
            [{"material": "PLA", "grams": 30.0, "slot": 2}],
        )

        with patch("app.main.parse_3mf_filament_usage", return_value=parser_result):
            first = self.client.post(
                "/api/usage/auto-from-3mf",
                data={
                    "project": "private",
                    "job_id": "job-unique-42",
                    "printer": "P1S-01",
                    "ams_slots": "2",
                },
                files={"file": ("print.3mf", b"dummy-3mf-content", "application/octet-stream")},
            )
            second = self.client.post(
                "/api/usage/auto-from-3mf",
                data={"project": "private", "job_id": "job-unique-42"},
                files={"file": ("print.3mf", b"dummy-3mf-content", "application/octet-stream")},
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)

        first_payload = first.json()
        second_payload = second.json()

        self.assertTrue(first_payload.get("ok"))
        self.assertFalse(first_payload.get("already_applied", False))
        self.assertEqual(first_payload.get("printer"), "P1S-01")
        self.assertEqual(first_payload.get("ams_slots"), [2])
        self.assertTrue(second_payload.get("ok"))
        self.assertTrue(second_payload.get("already_applied"))
        self.assertEqual(second_payload.get("printer"), "P1S-01")
        self.assertEqual(second_payload.get("ams_slots"), [2])

        with self.SessionLocal() as db:
            spool = db.query(Spool).filter(Spool.project == self.project_scope).first()
            history_rows = db.query(UsageHistory).all()

            self.assertIsNotNone(spool)
            self.assertEqual(float(spool.remaining_g), 170.0)
            self.assertEqual(len(history_rows), 1)
            self.assertEqual(history_rows[0].batch_id, "job-unique-42")
            self.assertEqual(float(history_rows[0].deducted_g), 30.0)

            context_rows = db.query(UsageBatchContext).all()
            self.assertEqual(len(context_rows), 1)
            self.assertEqual(context_rows[0].batch_id, "job-unique-42")
            self.assertEqual(context_rows[0].printer_name, "P1S-01")
            self.assertEqual(context_rows[0].ams_slots, "2")

    def test_auto_usage_from_gcode_file(self):
        gcode_content = b"\n".join(
            [
                b"; generated by PrusaSlicer",
                b"; filament_type = PLA",
                b"; filament_colour = Black",
                b"; filament used [g] = 12.5",
            ]
        )

        response = self.client.post(
            "/api/usage/auto-from-file",
            data={"project": "private", "job_id": "job-gcode-1"},
            files={"file": ("print.gcode", gcode_content, "text/plain")},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload.get("ok"))
        self.assertEqual(payload.get("changed_spools"), 1)
        self.assertEqual(float(payload.get("deducted_g")), 12.5)

        with self.SessionLocal() as db:
            spool = db.query(Spool).filter(Spool.project == self.project_scope).first()
            history_rows = db.query(UsageHistory).all()

            self.assertIsNotNone(spool)
            self.assertEqual(float(spool.remaining_g), 187.5)
            self.assertEqual(len(history_rows), 1)
            self.assertEqual(history_rows[0].mode, "auto_file")

    def test_inventory_value_updates_after_usage(self):
        dashboard_before = self.client.get("/dashboard?project=private&lang=en")
        self.assertEqual(dashboard_before.status_code, 200)
        self.assertIn("4.00 €", dashboard_before.text)

        with patch("app.main.parse_3mf_filament_usage", return_value=self._parser_result(50.0)):
            apply_usage = self.client.post(
                "/api/usage/auto-from-3mf",
                data={"project": "private", "job_id": "job-value-1"},
                files={"file": ("print.3mf", b"dummy-3mf-content", "application/octet-stream")},
            )

        self.assertEqual(apply_usage.status_code, 200)

        dashboard_after = self.client.get("/dashboard?project=private&lang=en")
        self.assertEqual(dashboard_after.status_code, 200)
        self.assertIn("3.00 €", dashboard_after.text)

    def test_auto_usage_prefers_exact_ams_slot_mapping(self):
        with self.SessionLocal() as db:
            db.add_all(
                [
                    Spool(
                        user_id=self.user_id,
                        brand="Bambu",
                        material="PLA",
                        color="Schwarz",
                        weight_g=1000.0,
                        remaining_g=190.0,
                        in_use=True,
                        project=self.project_scope,
                        ams_printer="P1S-01",
                        ams_slot=1,
                    ),
                    Spool(
                        user_id=self.user_id,
                        brand="Bambu",
                        material="PLA",
                        color="Schwarz",
                        weight_g=1000.0,
                        remaining_g=180.0,
                        in_use=True,
                        project=self.project_scope,
                        ams_printer="P1S-01",
                        ams_slot=4,
                    ),
                ]
            )
            db.commit()

        parser_result = (
            20.0,
            6000.0,
            {"__bambu_total_cost__": "1.23"},
            {"materials": ["PLA"], "colors": ["Schwarz"], "brands": ["Bambu"]},
            [{"material": "PLA", "grams": 20.0, "slot": 4}],
        )

        with patch("app.main.parse_3mf_filament_usage", return_value=parser_result):
            response = self.client.post(
                "/api/usage/auto-from-3mf",
                data={"project": "private", "job_id": "job-slot-1", "printer": "P1S-01"},
                files={"file": ("print.3mf", b"dummy-3mf-content", "application/octet-stream")},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload.get("ok"))

        with self.SessionLocal() as db:
            slot1_spool = (
                db.query(Spool)
                .filter(Spool.project == self.project_scope, Spool.ams_printer == "P1S-01", Spool.ams_slot == 1)
                .first()
            )
            slot4_spool = (
                db.query(Spool)
                .filter(Spool.project == self.project_scope, Spool.ams_printer == "P1S-01", Spool.ams_slot == 4)
                .first()
            )
            self.assertIsNotNone(slot1_spool)
            self.assertIsNotNone(slot4_spool)
            self.assertEqual(float(slot1_spool.remaining_g), 190.0)
            self.assertEqual(float(slot4_spool.remaining_g), 160.0)

            rows = db.query(UsageHistory).filter(UsageHistory.batch_id == "job-slot-1").all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].spool_id, slot4_spool.id)


if __name__ == "__main__":
    unittest.main()
