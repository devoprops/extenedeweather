"""Build a national extended.json from the GFS quarter-degree forecast.

Days are local to each grid point. The phone keeps a National Weather Service
day when it already has one, and uses this file for the days after that.
"""

import json
import math
import re
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

USER_AGENT = "(ExtendedWeather, noahmagnuson@gmail.com)"
BUCKET = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"
GRID = 0.25
FIRST_HOUR = 144
LAST_HOUR = 384
HOUR_STEP = 3
MIN_SAMPLES = 6
PRECIP_TRACE_INCHES = 0.01
MPH_PER_MPS = 2.2369362920544
MM_PER_INCH = 25.4
OUTPUT = Path("extended.json")

# Zones the National Weather Service uses for US cities. A grid point outside
# this set is dropped, which also drops ocean, Canada, and Mexico.
ZONES = {
    "America/New_York",
    "America/Detroit",
    "America/Indiana/Indianapolis",
    "America/Indiana/Vincennes",
    "America/Indiana/Winamac",
    "America/Indiana/Marengo",
    "America/Indiana/Petersburg",
    "America/Indiana/Vevay",
    "America/Indiana/Tell_City",
    "America/Indiana/Knox",
    "America/Kentucky/Louisville",
    "America/Kentucky/Monticello",
    "America/Chicago",
    "America/Menominee",
    "America/North_Dakota/Center",
    "America/North_Dakota/New_Salem",
    "America/North_Dakota/Beulah",
    "America/Denver",
    "America/Boise",
    "America/Phoenix",
    "America/Los_Angeles",
    "America/Anchorage",
    "America/Juneau",
    "America/Sitka",
    "America/Metlakatla",
    "America/Yakutat",
    "America/Nome",
    "America/Adak",
    "Pacific/Honolulu",
}

def session():
    if getattr(session, "value", None) is None:
        import requests

        value = requests.Session()
        value.headers["User-Agent"] = USER_AGENT
        session.value = value
    return session.value


def in_domain(lat, lon):
    """Lower 48, Alaska, or Hawaii. The Aleutians stop at the date line."""
    if 24 <= lat <= 50 and -125 <= lon <= -66:
        return True
    if 51 <= lat <= 72 and -180 <= lon <= -129:
        return True
    if 18 <= lat <= 23 and -161 <= lon <= -154:
        return True
    return False


def condition(cloud, precip_inches, temp_f):
    if precip_inches >= PRECIP_TRACE_INCHES:
        return "snow" if temp_f <= 32 else "rain"
    if cloud < 10:
        return "skc"
    if cloud < 25:
        return "few"
    if cloud < 50:
        return "sct"
    if cloud < 75:
        return "bkn"
    return "ovc"


def wind_from_degrees(u, v):
    """Direction the wind comes from, degrees clockwise from north."""
    if u == 0 and v == 0:
        return 0
    return int(round((270 - math.degrees(math.atan2(v, u))) % 360))


def parse_idx(text):
    """Index rows as (byte start, byte end or None, field name)."""
    rows = []
    for line in text.splitlines():
        parts = line.split(":")
        if len(parts) < 6:
            continue
        try:
            start = int(parts[1])
        except ValueError:
            continue
        rows.append((start, ":".join(parts[3:6])))
    messages = []
    for index, (start, name) in enumerate(rows):
        end = rows[index + 1][0] if index + 1 < len(rows) else None
        messages.append((start, end, name))
    return messages


def select_messages(messages, hour):
    """Temperature, humidity, wind, and cloud every 3 hours.

    Precipitation is the 6-hour total that ends on a 6-hour mark. Those
    blocks tile the forecast from hour 144 without overlapping the 3-hour
    slices that sit inside them.
    """
    names = {
        "2t": f"TMP:2 m above ground:{hour} hour fcst",
        "2r": f"RH:2 m above ground:{hour} hour fcst",
        "10u": f"UGRD:10 m above ground:{hour} hour fcst",
        "10v": f"VGRD:10 m above ground:{hour} hour fcst",
        "tcc": f"TCDC:entire atmosphere:{hour} hour fcst",
    }
    if hour % 6 == 0 and hour > FIRST_HOUR:
        names["tp"] = f"APCP:surface:{hour - 6}-{hour} hour acc fcst"
    chosen = {}
    for start, end, name in messages:
        for key, expected in names.items():
            if name == expected:
                chosen[key] = (start, end, name)
    missing = [key for key in names if key not in chosen]
    if missing:
        raise RuntimeError(f"hour {hour} is missing {', '.join(missing)}")
    return chosen


def split_precip(start, end, inches, zone):
    """Share a precipitation block across the local dates it covers."""
    total = (end - start).total_seconds()
    if total <= 0 or inches <= 0:
        return {}
    shares = {}
    cursor = start
    while cursor < end:
        nxt = min(cursor + timedelta(hours=1), end)
        day = cursor.astimezone(zone).date()
        shares[day] = shares.get(day, 0.0) + inches * (nxt - cursor).total_seconds() / total
        cursor = nxt
    return shares


def summarize(samples, precip, zone):
    """One local day needs at least six temperature hours, so the edges are dropped."""
    days = {}

    def slot(day):
        return days.setdefault(day, {"temps": [], "rh": [], "cloud": [], "u": [], "v": [], "precip": 0.0})

    for when, temp, rh, cloud, u, v in samples:
        record = slot(when.astimezone(zone).date())
        record["temps"].append(temp)
        record["rh"].append(rh)
        record["cloud"].append(cloud)
        record["u"].append(u)
        record["v"].append(v)
    for start, end, inches in precip:
        for day, amount in split_precip(start, end, inches, zone).items():
            slot(day)["precip"] += amount
    summary = []
    for day in sorted(days):
        record = days[day]
        if len(record["temps"]) < MIN_SAMPLES:
            continue
        high = round(max(record["temps"]))
        low = round(min(record["temps"]))
        mean_temp = sum(record["temps"]) / len(record["temps"])
        cloud = round(sum(record["cloud"]) / len(record["cloud"]))
        humidity = round(sum(record["rh"]) / len(record["rh"]))
        mean_u = sum(record["u"]) / len(record["u"])
        mean_v = sum(record["v"]) / len(record["v"])
        speeds = [math.hypot(u, v) * MPH_PER_MPS for u, v in zip(record["u"], record["v"])]
        wind = round(sum(speeds) / len(speeds))
        summary.append([
            int(day.strftime("%Y%m%d")),
            high,
            low,
            round(record["precip"] * 100),
            cloud,
            humidity,
            wind,
            wind_from_degrees(mean_u, mean_v),
            condition(cloud, record["precip"], mean_temp),
        ])
    return summary


def latest_complete_run(now):
    """Newest cycle whose 384-hour index is already published."""
    candidates = []
    for shift in range(0, 3):
        day = now.date() - timedelta(days=shift)
        for hour in (18, 12, 6, 0):
            cycle = datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc)
            if cycle <= now:
                candidates.append(cycle)
    candidates.sort(reverse=True)
    for cycle in candidates:
        url = grib_url(cycle, LAST_HOUR) + ".idx"
        response = session().head(url, timeout=30)
        if response.status_code == 200:
            return cycle
    raise RuntimeError("no complete GFS run found")


def grib_url(cycle, hour):
    stamp = cycle.strftime("%Y%m%d")
    hh = cycle.strftime("%H")
    return f"{BUCKET}/gfs.{stamp}/{hh}/atmos/gfs.t{hh}z.pgrb2.0p25.f{hour:03d}"


def fetch_bytes(url, start, end):
    response = session().get(url, headers={"Range": f"bytes={start}-{end - 1}"}, timeout=180)
    if response.status_code != 206:
        raise RuntimeError(f"{url} range {start}-{end} returned {response.status_code}")
    return response.content


def download_hour(cycle, hour):
    index = session().get(grib_url(cycle, hour) + ".idx", timeout=60)
    index.raise_for_status()
    chosen = select_messages(parse_idx(index.text), hour)
    blob = bytearray()
    for start, end, _name in chosen.values():
        if end is None:
            raise RuntimeError(f"hour {hour} field has no end byte")
        blob.extend(fetch_bytes(grib_url(cycle, hour), start, end))
    return bytes(blob)


def lon_to_signed(values):
    import numpy as np

    signed = np.where(values > 180, values - 360, values)
    return signed


def field_points(data):
    """Cropped (lat, lon, value) rows for the three regions."""
    import numpy as np

    values = np.asarray(data.squeeze().values)
    lats = np.asarray(data.latitude.values).squeeze()
    lons = lon_to_signed(np.asarray(data.longitude.values).squeeze())
    if lats.ndim != 1 or lons.ndim != 1:
        raise RuntimeError("expected a regular latitude and longitude grid")
    lat_index = np.where((lats >= 18) & (lats <= 72))[0]
    lon_index = np.where((lons >= -180) & (lons <= -66))[0]
    rows = []
    for i in lat_index:
        for j in lon_index:
            lat = round(float(lats[i]), 2)
            lon = round(float(lons[j]), 2)
            if not in_domain(lat, lon):
                continue
            raw = float(values[i, j])
            if math.isnan(raw):
                continue
            rows.append((lat, lon, raw))
    return rows


def read_fields(path):
    import cfgrib

    found = {}
    for dataset in cfgrib.open_datasets(path, backend_kwargs={"indexpath": ""}):
        for name in dataset.data_vars:
            short = dataset[name].attrs.get("GRIB_shortName")
            if short in {"2t", "2r", "10u", "10v", "tcc", "tp"}:
                found[short] = dataset[name].load()
        dataset.close()
    return found


def convert(short, raw):
    if short == "2t":
        return (raw - 273.15) * 9.0 / 5.0 + 32.0
    if short == "tp":
        return raw / MM_PER_INCH
    if short == "tcc" and raw <= 1.5:
        return raw * 100.0
    if short in {"10u", "10v"}:
        return raw
    return raw


def main():
    now = datetime.now(timezone.utc)
    cycle = latest_complete_run(now)
    print(f"using GFS {cycle.strftime('%Y%m%d %Hz')}", flush=True)
    from timezonefinder import TimezoneFinder

    finder = TimezoneFinder(in_memory=True)
    points = {}
    zone_of = {}
    skipped = set()

    def zone_for(lat, lon):
        key = (lat, lon)
        if key in zone_of:
            return zone_of[key]
        if key in skipped:
            return None
        zone = finder.timezone_at(lat=lat, lng=lon)
        if zone in ZONES:
            zone_of[key] = zone
            return zone
        skipped.add(key)
        return None

    for hour in range(FIRST_HOUR, LAST_HOUR + 1, HOUR_STEP):
        blob = download_hour(cycle, hour)
        with tempfile.NamedTemporaryFile(suffix=".grib2", delete=False) as handle:
            handle.write(blob)
            path = handle.name
        try:
            fields = read_fields(path)
        finally:
            Path(path).unlink(missing_ok=True)
        valid = cycle + timedelta(hours=hour)
        if "2t" not in fields:
            raise RuntimeError(f"hour {hour} has no temperature")
        columns = {}
        for short in ("2t", "2r", "10u", "10v", "tcc", "tp"):
            if short not in fields:
                continue
            columns[short] = {
                (lat, lon): convert(short, raw)
                for lat, lon, raw in field_points(fields[short])
            }
        for lat, lon in columns["2t"]:
            zone = zone_for(lat, lon)
            if zone is None:
                continue
            key = (lat, lon)
            record = points.setdefault(key, {"zone": zone, "samples": [], "precip": []})
            if all(key in columns[name] for name in ("2t", "2r", "10u", "10v", "tcc")):
                record["samples"].append((
                    valid,
                    columns["2t"][key],
                    columns["2r"][key],
                    columns["tcc"][key],
                    columns["10u"][key],
                    columns["10v"][key],
                ))
            if "tp" in columns and key in columns["tp"]:
                record["precip"].append((valid - timedelta(hours=6), valid, max(0.0, columns["tp"][key])))
        print(f"hour {hour} points {len(points)}", flush=True)
        time.sleep(0.5)
    if len(points) < 1000:
        raise RuntimeError(f"only {len(points)} grid points, refusing to publish")
    zones = {}
    for (lat, lon), record in points.items():
        zone = ZoneInfo(record["zone"])
        days = summarize(record["samples"], record["precip"], zone)
        if not days:
            continue
        zones.setdefault(record["zone"], {})[f"{lat:.2f},{lon:.2f}"] = days
    document = {
        "v": 1,
        "run": cycle.strftime("%Y%m%d%H"),
        "grid": GRID,
        "generated": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "zones": zones,
    }
    temporary = OUTPUT.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(document, separators=(",", ":")), encoding="utf-8")
    temporary.replace(OUTPUT)
    kept = sum(len(points_in_zone) for points_in_zone in zones.values())
    print(f"wrote {OUTPUT} with {kept} points in {len(zones)} zones", flush=True)


if __name__ == "__main__":
    main()
