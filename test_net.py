"""Unit tests for the Net's pure rules. Run: python3 -m unittest test_net -v"""
import os, unittest
from datetime import datetime, timedelta, timezone
import net

ET = net.ET
NOW = datetime(2026, 9, 21, 15, 0, tzinfo=timezone.utc)  # Mon 11:00 ET


def msg(minutes_ago, direction, body="", user=None, mtype="TYPE_SMS", call_status=None):
    m = {"dateAdded": (NOW - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z"),
         "direction": direction, "body": body, "messageType": mtype}
    if user: m["userId"] = user
    if call_status: m["meta"] = {"call": {"status": call_status}}
    return m


USERS = {"u1": "Michelle Rusticus"}


def norm(*raw):
    return net.normalize_messages(list(raw), USERS)


class BusinessHours(unittest.TestCase):
    def test_saturday_counts_sunday_does_not(self):
        sat_noon = datetime(2026, 9, 19, 16, 0, tzinfo=timezone.utc)   # Sat 12:00 ET
        mon_9 = datetime(2026, 9, 21, 13, 0, tzinfo=timezone.utc)      # Mon 09:00 ET
        self.assertAlmostEqual(net.business_hours_between(sat_noon, mon_9), 8 + 1, places=1)  # Sat 12-8 + Mon 8-9

    def test_overnight_is_zero(self):
        a = datetime(2026, 9, 21, 1, 0, tzinfo=timezone.utc)   # Sun 21:00 ET
        b = datetime(2026, 9, 21, 11, 0, tzinfo=timezone.utc)  # Mon 07:00 ET
        self.assertEqual(net.business_hours_between(a, b), 0.0)


class Eligibility(unittest.TestCase):
    def test_ad_sourced_by_source_or_tag(self):
        self.assertTrue(net.is_ad_sourced({"source": "Facebook Ads"}))
        self.assertTrue(net.is_ad_sourced({"source": "", "tags": ["funnel-lead"]}))
        self.assertTrue(net.is_ad_sourced({"source": "meta_ads"}))
        self.assertFalse(net.is_ad_sourced({"source": "Master Estimate Request", "tags": []}))

    def test_opt_out(self):
        self.assertTrue(net.is_opted_out({"tags": ["Facebook Ads", "Customer Replied STOP"]}))
        self.assertTrue(net.is_opted_out({"tags": ["out of area"]}))
        self.assertTrue(net.is_opted_out({"dnd": True}))
        self.assertFalse(net.is_opted_out({"tags": ["facebook ads", None]}))


class WaitingOnHuman(unittest.TestCase):
    def test_yes_to_a_robot_is_a_gap(self):
        ms = norm(msg(200, "outbound", "Hi Tom, it's CFT! While you wait..."), msg(190, "inbound", "Yes"))
        self.assertIsNotNone(net.waiting_on_human(ms, NOW))

    def test_sounds_good_after_michelle_is_closed(self):
        ms = norm(msg(80, "outbound", "I'll see you at 2", user="u1"), msg(70, "inbound", "Sounds good"))
        self.assertIsNone(net.waiting_on_human(ms, NOW))

    def test_thank_you_two_days_after_michelle_is_a_gap(self):
        ms = norm(msg(3000, "outbound", "checking in", user="u1"), msg(2000, "inbound", "were you coming out?"),
                  msg(1500, "outbound", "Ever wonder why our lights...", user=None), msg(1400, "inbound", "thank you"))
        self.assertIsNotNone(net.waiting_on_human(ms, NOW))

    def test_automation_after_lead_does_not_hide_gap(self):
        ms = norm(msg(300, "inbound", "What info do you need before the call?"), msg(100, "outbound", "Solid brass..."))
        self.assertIsNotNone(net.waiting_on_human(ms, NOW))

    def test_human_reply_after_lead_clears(self):
        ms = norm(msg(300, "inbound", "What info do you need?"), msg(100, "outbound", "Just your address", user="u1"))
        self.assertIsNone(net.waiting_on_human(ms, NOW))

    def test_stop_and_proposal_are_not_gaps(self):
        self.assertIsNone(net.waiting_on_human(norm(msg(300, "inbound", "STOP")), NOW))
        self.assertIsNone(net.waiting_on_human(norm(msg(300, "inbound", "[PROPOSAL] Michelle: caller asking...")), NOW))

    def test_missed_call_is_gap_answered_call_is_not(self):
        self.assertIsNotNone(net.waiting_on_human(norm(msg(300, "inbound", "", mtype="TYPE_CALL", call_status="no-answer")), NOW))
        self.assertIsNone(net.waiting_on_human(norm(msg(300, "inbound", "", mtype="TYPE_CALL", call_status="completed")), NOW))

    def test_too_fresh_is_not_yet_a_gap(self):
        self.assertIsNone(net.waiting_on_human(norm(msg(30, "inbound", "2pm works")), NOW))

    def test_tapback_emoji_and_reminder_echo_are_noise(self):
        for body in ('Liked \u201cThis text is to confirm your appointment\u201d', "\U0001F44D", "\U0001F44D\U0001F3FC", "Hey Larry,   Your estimate with Central Florida Trimlight has been confirmed"):
            self.assertIsNone(net.waiting_on_human(norm(msg(300, "outbound", "reminder"), msg(200, "inbound", body)), NOW), body)

    def test_short_reply_to_robot_question_is_gap_but_to_reminder_is_not(self):
        self.assertIsNotNone(net.waiting_on_human(norm(msg(300, "outbound", "Is this a good number to reach you?"), msg(200, "inbound", "Yes")), NOW))
        self.assertIsNone(net.waiting_on_human(norm(msg(300, "outbound", "Reminder: your estimate is tomorrow at 2."), msg(200, "inbound", "Thank you.")), NOW))

    def test_late_short_answer_to_michelles_question_is_gap(self):
        ms = norm(msg(500, "outbound", "What time works for you?", user="u1"), msg(90, "inbound", "2pm"))
        self.assertIsNotNone(net.waiting_on_human(ms, NOW))

    def test_unanswered_question_then_thank_you_returns_the_question(self):
        ms = norm(msg(3000, "outbound", "hi", user="u1"), msg(2000, "inbound", "were you coming out?"), msg(1500, "outbound", "drip"), msg(1400, "inbound", "thank you"))
        self.assertEqual(net.waiting_on_human(ms, NOW)["body"], "were you coming out?")

    def test_old_tail_outside_window_is_dropped(self):
        ms = norm(msg(60 * 24 * 20, "inbound", "No please cancel"), msg(100, "outbound", "drip"))
        self.assertIsNone(net.waiting_on_human(ms, NOW))

    def test_live_exchange_and_internal_notes_are_not_gaps(self):
        self.assertIsNone(net.waiting_on_human(norm(msg(400, "outbound", "Morning or afternoon?", user="u1"), msg(399, "inbound", "That's good either morning or afternoon is fine.")), NOW))
        self.assertIsNone(net.waiting_on_human(norm(msg(300, "inbound", "[NEW LEAD] Michelle: Kittya (407) - caller is looking for lights")), NOW))
        self.assertIsNone(net.waiting_on_human(norm(msg(300, "inbound", 'Removed a like from \u201cGood morning Karl\u201d')), NOW))
        self.assertIsNotNone(net.waiting_on_human(norm(msg(400, "outbound", "Anything else?", user="u1"), msg(60, "inbound", "Michelle, we need the documentation for my HOA to approve the lights.")), NOW))

    def test_missed_callback_right_after_michelle_is_gap(self):
        ms = norm(msg(400, "outbound", "Call me when free", user="u1"), msg(390, "inbound", "", mtype="TYPE_CALL", call_status="no-answer"))
        self.assertIsNotNone(net.waiting_on_human(ms, NOW))
        self.assertIsNone(net.waiting_on_human(norm(msg(300, "inbound", "Sophia is transferring +1727 to you NOW — answer and press 1")), NOW))

    def test_later_substantive_message_after_live_exchange_is_gap(self):
        ms = norm(msg(400, "outbound", "See you at 2", user="u1"), msg(390, "inbound", "ok"),
                  msg(120, "inbound", "Hey I need to reschedule, something came up with my kid, tomorrow morning works better for me"))
        self.assertIsNotNone(net.waiting_on_human(ms, NOW))

    def test_real_message_starting_with_hi_is_not_echo(self):
        self.assertIsNotNone(net.waiting_on_human(norm(msg(300, "inbound", "Hi Michelle, your estimate was way too high, can we talk?")), NOW))
        self.assertIsNone(net.waiting_on_human(norm(msg(300, "inbound", "Hey Mark,   Your estimate is in 1 hour!   We'll call you at: (407) 878")), NOW))

    def test_naive_timestamp_is_treated_as_utc(self):
        self.assertIsNotNone(net.parse_ts("2026-09-21T10:00:00").tzinfo)

    def test_service_complaint_is_gap(self):
        self.assertIsNotNone(net.waiting_on_human(norm(msg(300, "inbound", "I've been putting in a ticket for this issue no less than four times")), NOW))

    def test_confirmed_reminder_reply_is_closed(self):
        self.assertIsNone(net.waiting_on_human(norm(msg(500, "outbound", "Reminder: estimate tomorrow"), msg(400, "inbound", "Confirmed. Thank you!")), NOW))


class Schedule(unittest.TestCase):
    def test_next_slot_skips_sunday(self):
        sat_evening = datetime(2026, 9, 19, 23, 30, tzinfo=timezone.utc)  # Sat 19:30 ET
        nxt = net.next_slot(sat_evening).astimezone(ET)
        self.assertEqual((nxt.weekday(), nxt.hour), (0, 9))  # Monday 9am

    def test_next_slot_same_day(self):
        mon_10 = datetime(2026, 9, 21, 14, 0, tzinfo=timezone.utc)
        self.assertEqual(net.next_slot(mon_10).astimezone(ET).hour, 13)


class Rendering(unittest.TestCase):
    def test_subject_and_body(self):
        os.environ["GHL_LOCATION"] = "LOC"
        r = {"generated_at": NOW.isoformat(), "errors": [],
             "untouched_leads": [{"contact_id": "c1", "name": "Tom Hoff", "phone": "+1555", "source": "meta_ads", "created": NOW.isoformat(), "business_hours_waiting": 5.5, "auto_messages": 2, "replied": "Yes", "conversation_id": "x"}],
             "waiting_on_reply": []}
        self.assertEqual(net.subject(r), "Net Mon 09/21 11:00 AM: 1 untouched lead · 0 waiting on a reply")
        body = net.render_text(r)
        self.assertIn("Tom Hoff", body); self.assertIn('REPLIED: "Yes"', body); self.assertIn("contacts/detail/c1", body)
        self.assertIn("Nobody is waiting on a reply.", body)

    def test_subject_never_clear_on_failure(self):
        r = {"generated_at": NOW.isoformat(), "untouched_leads": [], "waiting_on_reply": [], "errors": ["contacts: HTTP 429"]}
        self.assertIn("ERROR", net.subject(r)); self.assertNotIn("clear", net.subject(r))
        r2 = {"error": "boom", "generated_at": NOW.isoformat(), "untouched_leads": [], "waiting_on_reply": [], "errors": ["boom"]}
        self.assertIn("ERROR", net.subject(r2)); self.assertIn("COULD NOT RUN", net.render_text(r2))
        r3 = {"generated_at": NOW.isoformat(), "untouched_leads": [], "waiting_on_reply": [], "errors": []}
        self.assertTrue(net.subject(r3).endswith("clear"))

    def test_render_partial_result_does_not_raise(self):
        r = {"generated_at": NOW.isoformat(), "untouched_leads": [], "errors": [],
             "waiting_on_reply": [{"contact_id": "c", "name": "X", "phone": "", "when": NOW.isoformat(), "said": "hi", "after_automation": True, "last_human": "never", "conversation_id": None},
                                  {"contact_id": "d", "name": "Y", "phone": "", "when": NOW.isoformat(), "said": "hi", "after_automation": False, "last_human": NOW.isoformat(), "conversation_id": None}]}
        self.assertIn("last human: never", net.render_text(r))

    def test_recipients_default_and_override(self):
        os.environ.pop("NET_TO", None); os.environ["ALERT_EMAIL"] = "a@x.com"
        self.assertEqual(net.recipients(), ["a@x.com"])
        os.environ["NET_TO"] = "a@x.com, b@y.com"
        self.assertEqual(net.recipients(), ["a@x.com", "b@y.com"])


if __name__ == "__main__":
    unittest.main()
