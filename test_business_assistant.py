import os
import tempfile
import unittest
from unittest.mock import patch

import business_assistant


class BusinessAssistantTests(unittest.TestCase):
    def test_default_config_has_required_fields(self):
        config = business_assistant.load_config()
        self.assertIn("business_name", config)
        self.assertIn("services", config)
        self.assertIn("working_hours", config)

    @patch.dict(os.environ, {}, clear=True)
    def test_missing_api_key_fails_cleanly(self):
        with self.assertRaises(RuntimeError):
            business_assistant.ask_gemini("Merhaba", business_assistant.DEFAULT_CONFIG)

    def test_create_appointment_requires_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "appointments.json")
            with patch.object(business_assistant, "APPOINTMENTS_PATH", path):
                appointment, error = business_assistant.create_appointment({})
                self.assertIsNone(appointment)
                self.assertIn("zorunlu", error)

    def test_create_appointment_blocks_same_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "appointments.json")
            data = {
                "customer_name": "Ali", "phone": "05550000000",
                "date": "2026-10-01", "time": "14:00", "service": "Saç Kesimi"
            }
            with patch.object(business_assistant, "APPOINTMENTS_PATH", path):
                first, error1 = business_assistant.create_appointment(data)
                second, error2 = business_assistant.create_appointment({**data, "customer_name": "Veli"})
                self.assertIsNotNone(first)
                self.assertIsNone(error1)
                self.assertIsNone(second)
                self.assertIn("başka bir randevu", error2)

    def test_parse_whatsapp_text(self):
        payload = {
            "entry": [{"changes": [{"value": {
                "contacts": [{"profile": {"name": "Ali"}}],
                "messages": [{"id": "wamid.test", "from": "905551112233", "type": "text", "text": {"body": "Merhaba"}}]
            }}]}]
        }
        parsed = business_assistant.parse_whatsapp_message(payload)
        self.assertEqual(parsed["message"], "Merhaba")
        self.assertEqual(parsed["phone"], "905551112233")
        self.assertEqual(parsed["message_id"], "wamid.test")


    def test_tenant_scoped_appointment_and_message_loaders(self):
        class FakeDatabase:
            def __init__(self):
                self.enabled_value = True
                self.appointments = {
                    1: [{"id":"a1","customer_name":"Ali"}],
                    2: [{"id":"b1","customer_name":"Veli"}],
                }
                self.messages = {
                    1: [{"id":"m1","message":"A"}],
                    2: [{"id":"m2","message":"B"}],
                }
            def enabled(self): return self.enabled_value
            def load_appointments_by_business(self, business_id): return self.appointments[business_id]
            def load_messages_by_business(self, business_id): return self.messages[business_id]

        fake = FakeDatabase()
        with patch.object(business_assistant, "database", fake):
            self.assertEqual(business_assistant.load_appointments((10, 1))[0]["id"], "a1")
            self.assertEqual(business_assistant.load_messages((10, 2))[0]["id"], "m2")
            self.assertNotEqual(
                business_assistant.load_appointments((10, 1))[0]["id"],
                business_assistant.load_appointments((10, 2))[0]["id"]
            )


    def test_appointment_time_parser(self):
        self.assertEqual(business_assistant.appointment_time_from_text("Saat 9:30 uygun mu?"), "09:30")
        self.assertEqual(business_assistant.appointment_time_from_text("14 uygun mu?"), "14:00")
        self.assertEqual(business_assistant.appointment_time_from_text("merhaba"), "")

    def test_closed_day_detection(self):
        config = {**business_assistant.DEFAULT_CONFIG, "closed_days": ["Pazar"]}
        self.assertTrue(business_assistant.is_closed_day("2026-10-04", config))
        self.assertFalse(business_assistant.is_closed_day("2026-10-05", config))

    def test_password_hash_and_verify(self):
        hashed = business_assistant.hash_password("NexoraTest123!")
        self.assertTrue(business_assistant.verify_password("NexoraTest123!", hashed))
        self.assertFalse(business_assistant.verify_password("wrong-password", hashed))

    def test_free_slots_do_not_expose_past_hours_today(self):
        config = {**business_assistant.DEFAULT_CONFIG, "working_hours": "09:00-18:00", "closed_days": []}
        with patch.object(business_assistant, "is_time_available", return_value=True):
            with patch("business_assistant.datetime") as dt:
                dt.now.return_value = type("D", (), {"hour": 14, "minute": 30, "strftime": lambda self, fmt: "2026-10-01"})()
                dt.strptime = __import__("datetime").datetime.strptime
                slots = business_assistant.get_free_slots("2026-10-01", config)
        self.assertNotIn("14:00", slots)
        self.assertIn("15:00", slots)

    def test_tenant_scoped_save_load_contract(self):
        class FakeDatabase:
            def enabled(self): return True
            def load_appointments_by_business(self, business_id):
                return [{"id": str(business_id)}]
            def load_messages_by_business(self, business_id):
                return [{"id": str(business_id)}]
        fake = FakeDatabase()
        with patch.object(business_assistant, "database", fake):
            self.assertEqual(business_assistant.load_appointments((1, 10))[0]["id"], "10")
            self.assertEqual(business_assistant.load_appointments((1, 20))[0]["id"], "20")
            self.assertEqual(business_assistant.load_messages((1, 10))[0]["id"], "10")
            self.assertEqual(business_assistant.load_messages((1, 20))[0]["id"], "20")


if __name__ == "__main__":
    unittest.main()
