"""Call tasks must be due when the call is due and sit on the setter's list.
Run: python3 -m unittest test_call_task -v"""
import os, unittest
from datetime import datetime, timezone
os.environ.setdefault("GHL_KEY", "x"); os.environ.setdefault("GHL_LOCATION", "x")
import app

NOW = datetime(2026, 9, 29, 14, 0, tzinfo=timezone.utc)


class CallTask(unittest.TestCase):
    def test_new_lead_due_in_five_minutes(self):
        t = app._call_task("CALL NEW FUNNEL LEAD within 5 min: Ann", "n", minutes=5, now=NOW)
        self.assertEqual(t["dueDate"], "2026-09-29T14:05:00Z")

    def test_recovered_lead_due_now(self):
        self.assertEqual(app._call_task("CALL RECOVERED LEAD NOW: Ann", "n", minutes=0, now=NOW)["dueDate"],
                         "2026-09-29T14:00:00Z")

    def test_assigned_to_setter(self):
        self.assertEqual(app._call_task("t", "n", now=NOW)["assignedTo"], app.SETTER_GHL_USER_ID)
        self.assertTrue(app.SETTER_GHL_USER_ID)

    def test_never_far_future(self):
        self.assertNotIn("2099", app._call_task("t", "n", now=NOW)["dueDate"])
        self.assertFalse(app._call_task("t", "n", now=NOW)["completed"])


if __name__ == "__main__":
    unittest.main()
