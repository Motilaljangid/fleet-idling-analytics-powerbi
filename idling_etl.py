"""
Idling Alerts ETL: Intangles API -> SQL Server (db_idling)

Setup (once):
    pip install requests pyodbc
    Windows (cmd):   setx INTANGLES_TOKEN "your_token"
    Optional:        setx SQL_CONN "DRIVER={ODBC Driver 17 for SQL Server};SERVER=localhost\\SQLEXPRESS;DATABASE=db_idling;Trusted_Connection=yes;"
    (setx ke baad naya terminal kholo)

Run:
    python idling_etl.py --probe            # 1 fleet ka raw JSON dikhata hai (DB me kuch nahi likhta)
    python idling_etl.py                    # full run (sab active fleets)
    python idling_etl.py --days 3           # pehli baar kitne din peeche se (default 7)
"""
import os, sys, json, time, argparse
from datetime import datetime, timezone, timedelta

import requests
import pyodbc

BASE_URL = "https://indium-apis.intangles.io/api/v1/vendor/alert_logs/{acc}/list/{start}/{end}"
ALERT_TYPE = "idling"
PAGE_SIZE = 500
WINDOW_MS = 3 * 24 * 3600 * 1000  # API ko 3-3 din ke chunks me call karte hain
OVERLAP_MS = 24 * 3600 * 1000     # 24 ghante overlap (alert idling khatam hone ke baad aata hai); silver MERGE duplicates hata deta hai
THROTTLE_S = 0.5                  # har successful call ke baad ruko (rate limit se bachne ke liye)

TOKEN = os.environ.get("INTANGLES_TOKEN")
CONN_STR = os.environ.get(
    "SQL_CONN",
    "DRIVER={ODBC Driver 17 for SQL Server};SERVER=localhost\\SQLEXPRESS;"
    "DATABASE=db_idling;Trusted_Connection=yes;TrustServerCertificate=yes;",
)


# ---------------- helpers ----------------
def now_ms():
    return int(time.time() * 1000)


def to_ms(v):
    """Seconds ya milliseconds, dono handle karta hai."""
    if v is None:
        return None
    try:
        v = int(float(v))
    except (TypeError, ValueError):
        return None
    return v * 1000 if v < 100_000_000_000 else v


def pick(d, *keys):
    """Pehli non-None key ki value."""
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d[k]
    return None


def to_float(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def to_bit(v):
    if v is None:
        return None
    if isinstance(v, str):
        return 1 if v.strip().lower() in ("1", "true", "yes") else 0
    return 1 if v else 0


def find_list(payload):
    """Response list ho ya dict ke andar list, dono chalega."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        if isinstance(payload.get("result"), (dict, list)):
            return find_list(payload["result"])
        for k in ("logs", "alert_logs", "alerts", "data", "results", "items"):
            if isinstance(payload.get(k), list):
                return payload[k]
        for v in payload.values():
            if isinstance(v, list):
                return v
    return []


# ---------------- API ----------------
def fetch_page(acc, start, end, pnum):
    url = BASE_URL.format(acc=acc, start=start, end=end)
    params = {"types": ALERT_TYPE, "psize": PAGE_SIZE, "pnum": pnum}
    headers = {"vendor-access-token": TOKEN}
    last = None
    for attempt in range(6):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=90)
            if r.status_code == 200:
                time.sleep(THROTTLE_S)          # API ko saans lene do
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504):
                last = f"HTTP {r.status_code}: {r.text[:200]}"
                ra = r.headers.get("Retry-After")
                wait = float(ra) if ra and ra.isdigit() else min(5 * 2 ** attempt, 60)
                print(f"    retry {attempt + 1}/6 after {wait:.0f}s ({last[:80]})")
                time.sleep(wait)
                continue
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
        except requests.RequestException as e:
            last = f"Request failed: {e}"
            print(f"    retry {attempt + 1}/6 ({last[:80]})")
            time.sleep(min(5 * 2 ** attempt, 60))
    raise RuntimeError(f"Retries exhausted. Last error: {last}")


def fetch_window(acc, start, end):
    """Ek window ke saare pages."""
    out, seen, pnum = [], set(), 1
    while True:
        rows = find_list(fetch_page(acc, start, end, pnum))
        new = [r for r in rows if r.get("id") not in seen]
        if not new:          # khali page ya repeat page => khatam
            break
        seen.update(r.get("id") for r in new)
        out.extend(new)
        pnum += 1
    return out


# ---------------- mapping (response -> bronze row) ----------------
def map_row(a):
    av = a.get("alert_values") or a   # is API me duration, hub_name etc. top level par hain
    loc = a.get("location") or {}
    return dict(
        alert_id=str(pick(a, "id", "_id", "alert_id", "alert_log_id") or "") or None,
        alert_type=pick(a, "type", "alert_type") or ALERT_TYPE,
        ts_ms=to_ms(pick(a, "timestamp", "ts", "time", "created_at", "alert_time")),
        vehicle_id=str(pick(a, "vehicle_id", "vehicle", "vid") or "") or None,
        vehicle_plate=pick(a, "reg_number", "registration", "plate", "vehicle_plate",
                           "vehicle_number", "tag"),
        vehicle_tag=pick(a, "tag", "vehicle_tag"),
        latitude=to_float(pick(a, "lat", "latitude") if pick(a, "lat", "latitude") is not None
                          else pick(loc, "lat", "latitude")),
        longitude=to_float(pick(a, "lng", "lon", "longitude") if pick(a, "lng", "lon", "longitude") is not None
                           else pick(loc, "lng", "lon", "longitude")),
        address=pick(a, "address", "location_name") or pick(av, "address"),
        duration_sec=to_float(pick(av, "duration", "idle_duration")),
        idle_threshold_sec=to_float(pick(av, "idle_threshold", "threshold", "idling_threshold")),
        fuel_consumed=to_float(pick(av, "fuel_consumed", "fuel", "fuel_used")),
        hub_name=pick(av, "hub_name", "hub"),
        hub_distance=to_float(pick(av, "hub_distance", "distance_from_hub")),
        inside_hub=to_bit(pick(av, "inside_hub", "in_hub", "is_inside_hub")),
        start_time_ms=to_ms(pick(av, "start_time", "start")),
        end_time_ms=to_ms(pick(av, "end_time", "end")),
        raw_json=json.dumps(a, ensure_ascii=False),
    )


COLS = ["alert_id", "alert_type", "ts_ms", "vehicle_id", "vehicle_plate", "vehicle_tag",
        "latitude", "longitude", "address", "duration_sec", "idle_threshold_sec",
        "fuel_consumed", "hub_name", "hub_distance", "inside_hub",
        "start_time_ms", "end_time_ms", "raw_json"]

INSERT_SQL = (
    "INSERT INTO dbo.bronze_idling_alerts (_batch_id, account_id, "
    + ", ".join(COLS)
    + ") VALUES (?, ?, " + ", ".join("?" * len(COLS)) + ")"
)


# ---------------- DB ----------------
def log(cur, batch, acc, name, rows, status, msg=None):
    cur.execute(
        "INSERT INTO dbo.etl_log (batch_id, account_id, fleet_name, rows_fetched, status, message) "
        "VALUES (?,?,?,?,?,?)",
        batch, acc, name, rows, status, (msg or "")[:1000],
    )


def get_watermark(cur, acc, default_days):
    cur.execute("SELECT last_end_ms FROM dbo.etl_watermark WHERE account_id=? AND alert_type=?",
                acc, ALERT_TYPE)
    row = cur.fetchone()
    if row:
        return max(0, int(row[0]) - OVERLAP_MS)
    return now_ms() - default_days * 24 * 3600 * 1000


def set_watermark(cur, acc, end_ms):
    cur.execute(
        """MERGE dbo.etl_watermark AS t
           USING (SELECT ? AS account_id, ? AS alert_type, ? AS last_end_ms) AS s
             ON t.account_id = s.account_id AND t.alert_type = s.alert_type
           WHEN MATCHED THEN UPDATE SET last_end_ms = s.last_end_ms, updated_at = SYSUTCDATETIME()
           WHEN NOT MATCHED THEN INSERT (account_id, alert_type, last_end_ms)
                VALUES (s.account_id, s.alert_type, s.last_end_ms);""",
        acc, ALERT_TYPE, end_ms,
    )


# ---------------- main flows ----------------
def probe(conn):
    cur = conn.cursor()
    cur.execute("SELECT account_id, fleet_name FROM dbo.dim_fleet WHERE is_active=1")
    fleets = cur.fetchall()
    end = now_ms()
    start = end - 30 * 24 * 3600 * 1000
    print(f"Probe: {len(fleets)} fleets, last 30 days (sirf page 1)\n")
    shown = False
    for acc, name in fleets:
        try:
            payload = fetch_page(acc, start, end, 1)
        except Exception as e:
            print(f"{name}: ERROR {e}")
            continue
        rows = find_list(payload)
        status = payload.get("status") if isinstance(payload, dict) else None
        res = payload.get("result") if isinstance(payload, dict) else None
        rkeys = list(res.keys()) if isinstance(res, dict) else None
        print(f"{name}: status={status!r}, rows={len(rows)}, result_keys={rkeys}")
        if rows and not shown:
            shown = True
            print("\n--- Sample alert (raw) ---")
            print(json.dumps(rows[0], indent=2, ensure_ascii=False))
            print("\n--- Mapped sample ---")
            m = map_row(rows[0]); m.pop("raw_json")
            print(json.dumps(m, indent=2, ensure_ascii=False, default=str))
            print("-" * 40)
        elif not shown:
            pass


def run(conn, default_days, backfill=None):
    batch = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    cur = conn.cursor()
    cur.execute("SELECT account_id, fleet_name FROM dbo.dim_fleet WHERE is_active = 1")
    fleets = cur.fetchall()
    print(f"Batch {batch}: {len(fleets)} fleets")

    for acc, name in fleets:
        try:
            if backfill:
                start, end = backfill          # fixed range, watermark ko nahi chhoona
            else:
                start = get_watermark(cur, acc, default_days)
                end = now_ms()
            total = 0
            t = start
            while t < end:
                t2 = min(t + WINDOW_MS, end)
                alerts = fetch_window(acc, t, t2)
                if alerts:
                    data = []
                    for a in alerts:
                        m = map_row(a)
                        data.append([batch, acc] + [m[c] for c in COLS])
                    cur.fast_executemany = True
                    cur.executemany(INSERT_SQL, data)
                    total += len(alerts)
                # har window ke baad progress save: beech me fail ho to wahin se resume
                if not backfill:
                    set_watermark(cur, acc, t2)
                conn.commit()
                print(f"    {name}: till {datetime.fromtimestamp(t2 / 1000):%Y-%m-%d} "
                      f"(+{len(alerts)}, total {total})")
                t = t2
            if not backfill:
                set_watermark(cur, acc, end)
            log(cur, batch, acc, name, total, "OK" if total else "WARN_ZERO_ROWS",
                "backfill" if backfill else None)
            conn.commit()
            print(f"  {name}: {total} rows")
        except Exception as e:
            conn.rollback()
            log(cur, batch, acc, name, None, "ERROR", str(e))
            conn.commit()
            print(f"  {name}: ERROR {e}")

    cur.execute("EXEC dbo.usp_bronze_to_silver @batch_id = ?", batch)
    conn.commit()
    cur.execute("EXEC dbo.usp_build_gold")
    conn.commit()
    print("Silver + Gold updated.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--from-date", help="backfill start, YYYY-MM-DD (IST)")
    ap.add_argument("--to-date", help="backfill end, YYYY-MM-DD (IST)")
    args = ap.parse_args()

    if not TOKEN:
        sys.exit("INTANGLES_TOKEN environment variable set nahi hai.")
    conn = pyodbc.connect(CONN_STR)
    try:
        backfill = None
        if args.from_date or args.to_date:
            if not (args.from_date and args.to_date):
                sys.exit("--from-date aur --to-date dono do.")
            def ist_ms(d):   # IST midnight -> UTC epoch ms
                dt = datetime.strptime(d, "%Y-%m-%d") - timedelta(minutes=330)
                return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)
            backfill = (ist_ms(args.from_date), ist_ms(args.to_date))
            if backfill[0] >= backfill[1]:
                sys.exit("from-date, to-date se pehle honi chahiye.")
            print(f"BACKFILL: {args.from_date} -> {args.to_date} (watermark change nahi hoga)")
        probe(conn) if args.probe else run(conn, args.days, backfill)
    finally:
        conn.close()
