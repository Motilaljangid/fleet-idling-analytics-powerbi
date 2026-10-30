"""
Fleet Master (Excel) -> saaf karke -> SQL Server dbo.dim_vehicle

Run:
    python load_vehicle_master.py --dry-run     # DB me kuch nahi likhta, sirf summary + CSV banata hai
    python load_vehicle_master.py               # SQL me load karta hai (purani rows replace)
    python load_vehicle_master.py --file "path/to/your_master.xlsx"

Jab bhi master file update ho, bas ye script dobara chalao.
"""
import os, re, sys, argparse
import pandas as pd

DEFAULT_FILE = "Fleet_Master.xlsx"   # put your own fleet master file here (not included in repo)
CONN_STR = os.environ.get(
    "SQL_CONN",
    "DRIVER={ODBC Driver 17 for SQL Server};SERVER=localhost\\SQLEXPRESS;"
    "DATABASE=db_idling;Trusted_Connection=yes;TrustServerCertificate=yes;",
)


# ---------------- helpers ----------------
def s(v):
    """Text saaf: NaN -> None, extra spaces hatao."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    v = re.sub(r"\s+", " ", str(v)).strip()
    return v or None


# ---------------- cleaning rules (yahan se naam badal sakte ho) ----------------
TYPE_MAP = {
    "bus": "Bus", "electric bus": "Electric Bus", "tanker": "Tanker", "truck": "Truck",
    "car": "Car / SUV", "innova": "Car / SUV", "etios": "Car / SUV",
    "traveller": "Traveller", "winger": "Winger",
    "bol": "Bol", "other": "Other", "tata": "Other",
}

MAKE_MAP = {
    "ASHOK LEYLAND LTD": "Ashok Leyland",
    "TATA MOTORS LTD": "Tata Motors",
    "DAIMLER INDIA COMMERCIAL VEHICLES PVT. LTD": "BharatBenz (Daimler)",
    "BHARATBENZ": "BharatBenz (Daimler)",
    "VOLVO BUSES": "Volvo",
    "EKA EV": "Eka EV",
    "MAHINDRA & MAHINDRA LIMITED": "Mahindra",
    "VE COMMERCIAL VEHICLES LTD": "VE Commercial (Eicher)",
    "JBM AUTO LIMITED": "JBM Auto",
    "FORCE MOTORS LIMITED": "Force Motors",
    "PINNACLE MOBILITY SOLUTIONS PVT LTD": "Pinnacle Mobility",
    "TOYOTA KIRLOSKAR MOTOR PVT LTD": "Toyota",
    "TOYOTA": "Toyota",
    "EICHER MOTOR": "Eicher Motors",
}

STATUS_MAP = {
    "hold": "Hold", "maintenance": "Maintenance",
    "maintenance(engine work)": "Maintenance (engine work)",
    "will sell": "Will sell", "non usebel": "Non usable",
    "change engine bs3": "Change engine (BS3)",
    "sold_out": "Sold out", "rto document pending": "RTO document pending",
}

AC_GROUP = {"ac": "AC", "9m ac": "AC", "12m ac": "AC", "sleeper ac": "AC", "nac": "Non-AC"}
AC_DETAIL = {"ac": "AC", "9m ac": "AC (9 m)", "12m ac": "AC (12 m)",
             "sleeper ac": "Sleeper AC", "nac": "Non-AC"}


def clean_master(raw: pd.DataFrame) -> pd.DataFrame:
    raw = raw.loc[:, ~raw.columns.astype(str).str.startswith("Unnamed")].copy()
    raw.columns = [str(c).strip() for c in raw.columns]

    out = []
    for _, r in raw.iterrows():
        t_raw, ac_raw, eng_raw = s(r.get("Type")), s(r.get("AC Type")), s(r.get("Engine Type"))
        mk_raw, op_raw, st_raw = s(r.get("Make")), s(r.get("Operational Type")), s(r.get("Vehicle_Status"))
        fleet = s(r.get("Fleet Name")) or ""

        sold = any((x or "").lower() == "sold" for x in (t_raw, ac_raw, eng_raw)) \
            or (st_raw or "").lower() == "sold_out"

        vtype = "Unknown" if sold else TYPE_MAP.get((t_raw or "").lower(), "Unknown")

        ac_key = (ac_raw or "").lower()
        ac_type = AC_GROUP.get(ac_key, "Unknown")
        ac_detail = AC_DETAIL.get(ac_key, "Unknown")

        make = "Unknown"
        if mk_raw and mk_raw.lower() != "not available":
            make = MAKE_MAP.get(mk_raw.upper(), mk_raw.title())

        eng = (eng_raw or "").upper()
        if eng in ("BS3", "BS4", "BS6"):
            engine = eng
        elif vtype == "Electric Bus":
            engine = "Electric"
        else:
            engine = "Unknown"          # blank, 0, Sold

        if vtype == "Electric Bus":
            powertrain = "Electric"
        elif fleet == "Chartered CNG":
            powertrain = "CNG"
        else:
            powertrain = "Diesel / Other ICE"

        op = "Safe custody" if (op_raw or "").lower().startswith("safe cust") else (op_raw or "Unknown")
        status = "Sold out" if sold else STATUS_MAP.get((st_raw or "").lower(), st_raw or "No remark")
        is_active = int(op == "Operational" and not sold)

        fuel = (s(r.get("Fuel Data")) or "Unknown").title()
        cam_raw = s(r.get("Camera"))
        camera = "Unknown" if cam_raw is None else ("Yes" if cam_raw.lower() == "yes" else "No")
        data_type = "Tracking only" if (s(r.get("Data Type")) or "").lower() == "tracking only" else "Standard"

        out.append(dict(
            vehicle_id=s(r.get("id")),
            master_account_id=s(r.get("account_id")),
            rto_number=s(r.get("RTO")),
            plate=s(r.get("plate")),
            fleet_name_master=fleet,
            vehicle_type=vtype,
            ac_type=ac_type,
            ac_detail=ac_detail,
            make=make,
            engine_type=engine,
            powertrain=powertrain,
            operational_type=op,
            vehicle_status=status,
            is_active=is_active,
            fuel_data=fuel,
            camera=camera,
            data_type=data_type,
        ))
    df = pd.DataFrame(out)
    df = df[df["vehicle_id"].notna()].drop_duplicates("vehicle_id", keep="first")
    return df.reset_index(drop=True)


# ---------------- SQL ----------------
DDL = """
IF OBJECT_ID('dbo.dim_vehicle','U') IS NULL
CREATE TABLE dbo.dim_vehicle (
    vehicle_id         varchar(100)  NOT NULL CONSTRAINT PK_dim_vehicle PRIMARY KEY,
    master_account_id  varchar(100)  NULL,
    rto_number         varchar(50)   NULL,
    plate              varchar(50)   NULL,
    fleet_name_master  nvarchar(200) NULL,
    vehicle_type       nvarchar(50)  NOT NULL,
    ac_type            varchar(20)   NOT NULL,
    ac_detail          varchar(30)   NOT NULL,
    make               nvarchar(100) NOT NULL,
    engine_type        varchar(30)   NOT NULL,
    powertrain         varchar(30)   NOT NULL,
    operational_type   varchar(30)   NOT NULL,
    vehicle_status     nvarchar(60)  NOT NULL,
    is_active          bit           NOT NULL,
    fuel_data          varchar(10)   NOT NULL,
    camera             varchar(10)   NOT NULL,
    data_type          varchar(30)   NOT NULL,
    loaded_at          datetime2(0)  NOT NULL CONSTRAINT DF_dim_vehicle_loaded DEFAULT SYSUTCDATETIME()
);
"""

COLS = ["vehicle_id", "master_account_id", "rto_number", "plate", "fleet_name_master",
        "vehicle_type", "ac_type", "ac_detail", "make", "engine_type", "powertrain",
        "operational_type", "vehicle_status", "is_active", "fuel_data", "camera", "data_type"]
INSERT_SQL = f"INSERT INTO dbo.dim_vehicle ({', '.join(COLS)}) VALUES ({', '.join('?' * len(COLS))})"


def load_to_sql(df):
    import pyodbc
    conn = pyodbc.connect(CONN_STR)
    try:
        cur = conn.cursor()
        cur.execute(DDL)
        cur.execute("DELETE FROM dbo.dim_vehicle")
        rows = [tuple(None if pd.isna(v) else v for v in rec) for rec in df[COLS].itertuples(index=False, name=None)]
        cur.fast_executemany = True
        cur.executemany(INSERT_SQL, rows)
        conn.commit()
        print(f"Loaded {len(rows)} vehicles into dbo.dim_vehicle")

        try:
            cur.execute("""
                SELECT COUNT(DISTINCT s.vehicle_id),
                       COUNT(DISTINCT CASE WHEN v.vehicle_id IS NOT NULL THEN s.vehicle_id END)
                FROM dbo.silver_idling_alerts s
                LEFT JOIN dbo.dim_vehicle v ON v.vehicle_id = s.vehicle_id""")
            total, matched = cur.fetchone()
            print(f"Alerts me {total} vehicles, master se match: {matched} ({total - matched} match nahi hue)")
        except Exception as e:
            print("Match check skip:", e)
    finally:
        conn.close()


def summary(df):
    print(f"\nTotal vehicles: {len(df)}  |  Active: {int(df['is_active'].sum())}")
    for c in ["vehicle_type", "ac_type", "ac_detail", "make", "engine_type", "powertrain",
              "operational_type", "vehicle_status", "fuel_data", "camera", "data_type"]:
        print(f"\n--- {c}")
        print(df[c].value_counts().to_string())


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=DEFAULT_FILE)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", default="dim_vehicle_clean.csv", help="dry-run me CSV ka naam")
    args = ap.parse_args()

    if not os.path.exists(args.file):
        sys.exit(f"File nahi mili: {args.file}\n--file ke saath sahi path do.")

    raw = pd.read_excel(args.file, sheet_name=0)
    df = clean_master(raw)
    summary(df)

    if args.dry_run:
        df.to_csv(args.out, index=False, encoding="utf-8-sig")
        print(f"\nDry run: {args.out} bana diya, DB me kuch nahi likha.")
    else:
        load_to_sql(df)
