import datetime as dt
import unittest

from edition_profiles import is_schedule_slot, resolve_edition_context


class EditionProfileTests(unittest.TestCase):
    def test_morning_and_evening_have_distinct_prompt_and_session(self):
        morning = resolve_edition_context("morning_close_review")
        evening = resolve_edition_context("evening_premarket_watch")
        self.assertEqual(morning.scheduled_local_time, "06:30")
        self.assertEqual(evening.scheduled_local_time, "17:30")
        self.assertNotEqual(morning.prompt_hash, evening.prompt_hash)
        self.assertNotEqual(morning.market_session, evening.market_session)

    def test_unknown_edition_is_rejected(self):
        with self.assertRaises(ValueError):
            resolve_edition_context("unknown")

    def test_schedule_windows_are_edition_specific(self):
        tokyo = dt.timezone(dt.timedelta(hours=9))
        self.assertTrue(is_schedule_slot("morning_close_review", dt.datetime(2026, 7, 19, 6, 30, tzinfo=tokyo)))
        self.assertTrue(is_schedule_slot("evening_premarket_watch", dt.datetime(2026, 7, 19, 17, 30, tzinfo=tokyo)))
        self.assertFalse(is_schedule_slot("morning_close_review", dt.datetime(2026, 7, 19, 17, 30, tzinfo=tokyo)))


if __name__ == "__main__":
    unittest.main()
