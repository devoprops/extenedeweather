import unittest
from datetime import datetime, timedelta, timezone
from fetch_gfs import (
    condition,
    in_domain,
    parse_idx,
    select_messages,
    split_precip,
    summarize,
    wind_from_degrees,
)


class ForecastTests(unittest.TestCase):
    def test_domain_includes_alaska_and_hawaii_only(self):
        self.assertTrue(in_domain(34.75, -86.75))
        self.assertTrue(in_domain(61.25, -150.0))
        self.assertTrue(in_domain(21.25, -157.75))
        self.assertFalse(in_domain(15.0, -157.0))
        self.assertFalse(in_domain(61.0, -120.0))
        self.assertFalse(in_domain(48.0, -130.0))

    def test_condition_uses_precip_before_cloud(self):
        self.assertEqual("rain", condition(10, 0.2, 40))
        self.assertEqual("snow", condition(90, 0.2, 30))
        self.assertEqual("skc", condition(5, 0.0, 70))
        self.assertEqual("bkn", condition(60, 0.0, 70))

    def test_wind_from_the_east_is_90(self):
        self.assertEqual(90, wind_from_degrees(-1, 0))

    def test_six_hour_precip_is_selected_only_after_hour_144(self):
        text = "\n".join([
            "1:100:d=2026112312:TMP:2 m above ground:150 hour fcst:",
            "2:200:d=2026112312:RH:2 m above ground:150 hour fcst:",
            "3:300:d=2026112312:UGRD:10 m above ground:150 hour fcst:",
            "4:400:d=2026112312:VGRD:10 m above ground:150 hour fcst:",
            "5:500:d=2026112312:TCDC:entire atmosphere:150 hour fcst:",
            "6:600:d=2026112312:APCP:surface:144-150 hour acc fcst:",
            "7:700:d=2026112312:APCP:surface:0-150 hour acc fcst:",
            "8:800:d=2026112312:TMP:2 m above ground:151 hour fcst:",
        ])
        chosen = select_messages(parse_idx(text), 150)
        self.assertEqual((600, 700), chosen["tp"][:2])
        self.assertNotIn("tp", select_messages(parse_idx(text.replace("150", "144")), 144))

    def test_precip_block_splits_across_local_midnight(self):
        zone = timezone(timedelta(hours=-6))
        start = datetime(2026, 12, 2, 4, tzinfo=timezone.utc)
        end = start + timedelta(hours=6)
        shares = split_precip(start, end, 0.6, zone)
        self.assertEqual(2, len(shares))
        self.assertAlmostEqual(0.6, sum(shares.values()))

    def test_short_day_is_dropped(self):
        zone = timezone(timedelta(hours=-6))
        start = datetime(2026, 12, 2, 18, tzinfo=timezone.utc)
        samples = [
            (start + timedelta(hours=3 * i), 40, 50, 20, 1, 0)
            for i in range(5)
        ]
        self.assertEqual([], summarize(samples, [], zone))


if __name__ == "__main__":
    unittest.main()
