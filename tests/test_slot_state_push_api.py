import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base, get_db
from app.main import app
import app.main as main_module
from app.models import DeviceSlotState, Printer


class SlotStatePushApiTests(unittest.TestCase):
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

    def test_push_slot_state_upserts_rows(self):
        payload = {
            "project": "private",
            "source": "local-slot-bridge",
            "printers": [
                {
                    "printer": "P1S-01",
                    "serial": "SERIAL-001",
                    "telemetry": {
                        "status": "online",
                        "progress": 55.2,
                        "nozzle_temp": 221.4,
                        "bed_temp": 59.8,
                        "firmware": "01.08.00.00",
                    },
                    "slots": [
                        {"slot": 1, "brand": "Bambu", "material": "PLA", "color": "Black"},
                        {"slot": 2, "brand": "Bambu", "material": "PETG", "color": "White"},
                    ],
                }
            ],
        }

        response = self.client.post("/api/slot-state/push", json=payload)
        self.assertEqual(response.status_code, 200)
        data = response.json()

        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("entries"), 2)
        self.assertEqual(data.get("updated"), 2)

        with self.SessionLocal() as db:
            rows = (
                db.query(DeviceSlotState)
                .filter(DeviceSlotState.project == self.project_scope, DeviceSlotState.printer_name == "P1S-01")
                .order_by(DeviceSlotState.slot.asc())
                .all()
            )
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0].slot, 101)
            self.assertEqual(rows[0].observed_material, "PLA")
            self.assertEqual(rows[1].slot, 102)
            self.assertEqual(rows[1].observed_material, "PETG")
            self.assertEqual(rows[0].printer_serial, "SERIAL-001")

            printers = db.query(Printer).filter(Printer.project == self.project_scope).all()
            self.assertEqual(len(printers), 1)
            self.assertEqual(printers[0].name, "P1S-01")
            self.assertEqual(printers[0].serial, "SERIAL-001")
            self.assertEqual(printers[0].status, "online")
            self.assertAlmostEqual(float(printers[0].telemetry_progress or 0.0), 55.2, places=1)

    def test_push_slot_state_rejects_invalid_json(self):
        response = self.client.post(
            "/api/slot-state/push",
            content="{bad-json",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data.get("ok"))
        self.assertEqual(data.get("error"), "invalid_json")

    def test_push_slot_state_keeps_multiple_ams_with_same_local_slot(self):
        payload = {
            "project": "private",
            "source": "local-slot-bridge",
            "printers": [
                {
                    "printer": "X1C-Multi-AMS",
                    "serial": "SERIAL-MULTI-AMS",
                    "telemetry": {"status": "online"},
                    "slots": [
                        {
                            "slot": 1,
                            "slot_local": 1,
                            "ams_unit": 1,
                            "ams_name": "AMS-A",
                            "brand": "Bambu",
                            "material": "PLA",
                            "color": "Black",
                        },
                        {
                            "slot": 201,
                            "slot_local": 1,
                            "ams_unit": 2,
                            "ams_name": "AMS-B",
                            "brand": "Bambu",
                            "material": "PETG",
                            "color": "White",
                        },
                    ],
                }
            ],
        }

        response = self.client.post("/api/slot-state/push", json=payload)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("entries"), 2)

        with self.SessionLocal() as db:
            rows = (
                db.query(DeviceSlotState)
                .filter(DeviceSlotState.project == self.project_scope, DeviceSlotState.printer_name == "X1C-Multi-AMS")
                .order_by(DeviceSlotState.slot.asc())
                .all()
            )
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0].slot, 101)
            self.assertEqual(rows[0].slot_local, 1)
            self.assertEqual(rows[0].ams_unit, 1)
            self.assertEqual(rows[0].ams_name, "AMS-A")

            self.assertEqual(rows[1].slot, 201)
            self.assertEqual(rows[1].slot_local, 1)
            self.assertEqual(rows[1].ams_unit, 2)
            self.assertEqual(rows[1].ams_name, "AMS-B")

    def test_push_slot_state_maps_raw_bambu_ams_ids_stably(self):
        payload = {
            "project": "private",
            "source": "local-slot-bridge",
            "printers": [
                {
                    "printer": "X1C-Raw-AMS",
                    "serial": "SERIAL-RAW-AMS",
                    "telemetry": {"status": "online"},
                    "slots": [
                        {
                            "slot_local": 1,
                            "ams_id": 128,
                            "brand": "Bambu",
                            "material": "PETG",
                            "color": "White",
                        },
                        {
                            "slot_local": 1,
                            "ams_id": 0,
                            "brand": "Bambu",
                            "material": "PLA",
                            "color": "Black",
                        },
                    ],
                }
            ],
        }

        response = self.client.post("/api/slot-state/push", json=payload)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("entries"), 2)

        with self.SessionLocal() as db:
            rows = (
                db.query(DeviceSlotState)
                .filter(DeviceSlotState.project == self.project_scope, DeviceSlotState.printer_name == "X1C-Raw-AMS")
                .order_by(DeviceSlotState.slot.asc())
                .all()
            )
            self.assertEqual(len(rows), 2)

            self.assertEqual(rows[0].slot, 101)
            self.assertEqual(rows[0].slot_local, 1)
            self.assertEqual(rows[0].ams_unit, 1)

            self.assertEqual(rows[1].slot, 201)
            self.assertEqual(rows[1].slot_local, 1)
            self.assertEqual(rows[1].ams_unit, 2)

    def test_printers_page_create_and_delete(self):
        create_response = self.client.post(
            "/printers?project=private&lang=en",
            data={
                "name": "X1C-Main",
                "serial": "X1C-ABC-001",
                "host": "192.168.1.31",
                "port": "8883",
                "access_code": "secret-code",
                "is_active": "1",
            },
        )
        self.assertEqual(create_response.status_code, 200)
        self.assertIn("Printer saved.", create_response.text)
        self.assertIn("X1C-Main", create_response.text)

        with self.SessionLocal() as db:
            printer = db.query(Printer).filter(Printer.project == self.project_scope, Printer.serial == "X1C-ABC-001").first()
            self.assertIsNotNone(printer)
            printer_id = int(printer.id)

        delete_response = self.client.post(f"/printers/{printer_id}/delete?project=private&lang=en")
        self.assertEqual(delete_response.status_code, 200)
        self.assertIn("Printer deleted.", delete_response.text)

        with self.SessionLocal() as db:
            deleted = db.query(Printer).filter(Printer.project == self.project_scope, Printer.id == printer_id).first()
            self.assertIsNone(deleted)

    def test_printers_page_applies_user_ams_name_mapping(self):
        create_response = self.client.post(
            "/printers?project=private&lang=en",
            data={
                "name": "X1C-Map",
                "serial": "X1C-MAP-001",
                "is_active": "1",
            },
        )
        self.assertEqual(create_response.status_code, 200)
        self.assertIn("Printer saved.", create_response.text)

        payload = {
            "project": "private",
            "source": "local-slot-bridge",
            "printers": [
                {
                    "printer": "X1C-Map",
                    "serial": "X1C-MAP-001",
                    "telemetry": {"status": "online"},
                    "slots": [
                        {"slot": 1, "slot_local": 1, "ams_unit": 1, "brand": "Bambu", "material": "PLA", "color": "Black"},
                        {"slot": 201, "slot_local": 1, "ams_unit": 2, "brand": "Bambu", "material": "PETG", "color": "White"},
                    ],
                }
            ],
        }
        push_response = self.client.post("/api/slot-state/push", json=payload)
        self.assertEqual(push_response.status_code, 200)
        self.assertTrue(push_response.json().get("ok"))

        with self.SessionLocal() as db:
            printer = db.query(Printer).filter(Printer.project == self.project_scope, Printer.serial == "X1C-MAP-001").first()
            self.assertIsNotNone(printer)
            printer_id = int(printer.id)

        map_first = self.client.post(
            f"/printers/{printer_id}/ams-mapping?project=private&lang=en",
            data={"ams_unit": "1", "ams_label": "TopLeft"},
        )
        self.assertEqual(map_first.status_code, 200)

        map_second = self.client.post(
            f"/printers/{printer_id}/ams-mapping?project=private&lang=en",
            data={"ams_unit": "2", "ams_label": "TopRight"},
        )
        self.assertEqual(map_second.status_code, 200)

        printers_response = self.client.get("/printers?project=private&lang=en")
        self.assertEqual(printers_response.status_code, 200)
        self.assertIn("TopLeft", printers_response.text)
        self.assertIn("TopRight", printers_response.text)
        self.assertIn("<td class=\"ui-td font-medium\">101</td>", printers_response.text)
        self.assertIn("<td class=\"ui-td font-medium\">201</td>", printers_response.text)


if __name__ == "__main__":
    unittest.main()
