/* ============================================================
   Idling Alerts - local SQL Server (Express) setup
   Run once in SSMS. Safe to re-run (IF OBJECT_ID ... IS NULL).
   Step 0 (separately, once):  CREATE DATABASE db_idling;
   ============================================================ */
USE db_idling;
GO

/* ---------- 1. DIMENSIONS ---------- */
IF OBJECT_ID('dbo.dim_fleet','U') IS NULL
CREATE TABLE dbo.dim_fleet (
    account_id  varchar(100)  NOT NULL CONSTRAINT PK_dim_fleet PRIMARY KEY,
    fleet_name  nvarchar(200) NOT NULL,
    is_active   bit           NOT NULL CONSTRAINT DF_dim_fleet_active DEFAULT 1
);
GO

IF OBJECT_ID('dbo.dim_date','U') IS NULL
BEGIN
    CREATE TABLE dbo.dim_date (
        date_key    date        NOT NULL CONSTRAINT PK_dim_date PRIMARY KEY,
        day_name    varchar(20) NOT NULL,
        month_name  varchar(20) NOT NULL,
        month_no    int         NOT NULL,
        year_no     int         NOT NULL
    );

    ;WITH d AS (
        SELECT CAST('2026-01-01' AS date) AS dt
        UNION ALL
        SELECT DATEADD(DAY, 1, dt) FROM d WHERE dt < '2029-12-31'
    )
    INSERT INTO dbo.dim_date (date_key, day_name, month_name, month_no, year_no)
    SELECT dt, DATENAME(WEEKDAY, dt), DATENAME(MONTH, dt), MONTH(dt), YEAR(dt)
    FROM d
    OPTION (MAXRECURSION 0);
END
GO

/* ---------- 2. CONTROL TABLES ---------- */
-- One row per fleet + alert type. last_end_ms = end of the last window loaded successfully.
IF OBJECT_ID('dbo.etl_watermark','U') IS NULL
CREATE TABLE dbo.etl_watermark (
    account_id   varchar(100) NOT NULL,
    alert_type   varchar(50)  NOT NULL,
    last_end_ms  bigint       NOT NULL,
    updated_at   datetime2(0) NOT NULL CONSTRAINT DF_wm_updated DEFAULT SYSUTCDATETIME(),
    CONSTRAINT PK_etl_watermark PRIMARY KEY (account_id, alert_type)
);
GO

-- One row per fleet per run (monitoring + data-quality checks)
IF OBJECT_ID('dbo.etl_log','U') IS NULL
CREATE TABLE dbo.etl_log (
    log_id        bigint IDENTITY(1,1) PRIMARY KEY,
    batch_id      varchar(20)   NOT NULL,
    account_id    varchar(100)  NULL,
    fleet_name    nvarchar(200) NULL,
    rows_fetched  int           NULL,
    status        varchar(30)   NOT NULL,   -- OK / WARN_ZERO_ROWS / ERROR
    message       nvarchar(1000) NULL,
    logged_at     datetime2(0)  NOT NULL CONSTRAINT DF_log_at DEFAULT SYSUTCDATETIME()
);
GO

/* ---------- 3. BRONZE (raw, append only) ---------- */
IF OBJECT_ID('dbo.bronze_idling_alerts','U') IS NULL
CREATE TABLE dbo.bronze_idling_alerts (
    bronze_id           bigint IDENTITY(1,1) PRIMARY KEY,
    _batch_id           varchar(20)    NOT NULL,   -- yyyyMMddHHmmss, one per run
    _loaded_at          datetime2(0)   NOT NULL CONSTRAINT DF_bronze_loaded DEFAULT SYSUTCDATETIME(),
    account_id          varchar(100)   NOT NULL,   -- the fleet account we queried
    alert_id            varchar(100)   NULL,
    alert_type          varchar(50)    NULL,
    ts_ms               bigint         NULL,
    vehicle_id          varchar(100)   NULL,
    vehicle_plate       varchar(50)    NULL,
    vehicle_tag         varchar(100)   NULL,
    latitude            float          NULL,
    longitude           float          NULL,
    address             nvarchar(500)  NULL,
    duration_sec        float          NULL,
    idle_threshold_sec  float          NULL,
    fuel_consumed       float          NULL,
    hub_name            nvarchar(200)  NULL,
    hub_distance        float          NULL,
    inside_hub          bit            NULL,
    start_time_ms       bigint         NULL,
    end_time_ms         bigint         NULL,
    raw_json            nvarchar(max)  NULL
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_bronze_batch' AND object_id = OBJECT_ID('dbo.bronze_idling_alerts'))
    CREATE INDEX IX_bronze_batch ON dbo.bronze_idling_alerts (_batch_id) INCLUDE (alert_id);
GO

/* ---------- 4. SILVER (clean, one row per alert) ---------- */
IF OBJECT_ID('dbo.silver_idling_alerts','U') IS NULL
CREATE TABLE dbo.silver_idling_alerts (
    alert_id            varchar(100)  NOT NULL CONSTRAINT PK_silver_idling PRIMARY KEY,
    account_id          varchar(100)  NOT NULL,
    alert_type          varchar(50)   NULL,
    vehicle_id          varchar(100)  NULL,
    vehicle_plate       varchar(50)   NULL,
    alert_ts_utc        datetime2(3)  NOT NULL,
    alert_ts_ist        datetime2(3)  NOT NULL,
    alert_date          date          NOT NULL,     -- IST date
    alert_hour          tinyint       NOT NULL,     -- IST hour 0-23
    latitude            float         NULL,
    longitude           float         NULL,
    address             nvarchar(500) NULL,
    duration_sec        float         NULL,         -- vendor "duration" (seconds; > idle_threshold)
    duration_min        float         NULL,
    idle_threshold_sec  float         NULL,
    fuel_litres         float         NULL,
    hub_name            nvarchar(200) NULL,
    hub_distance_m      float         NULL,
    inside_hub          bit           NULL,
    _batch_id           varchar(20)   NOT NULL,
    _updated_at         datetime2(0)  NOT NULL CONSTRAINT DF_silver_upd DEFAULT SYSUTCDATETIME()
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_silver_date' AND object_id = OBJECT_ID('dbo.silver_idling_alerts'))
    CREATE INDEX IX_silver_date ON dbo.silver_idling_alerts (alert_date, account_id);
GO

/* ---------- 5. GOLD (what Power BI reads) ---------- */
IF OBJECT_ID('dbo.gold_idling_daily','U') IS NULL
CREATE TABLE dbo.gold_idling_daily (
    alert_date        date          NOT NULL,
    fleet_name        nvarchar(200) NOT NULL,
    alerts            int           NOT NULL,
    vehicles_affected int           NOT NULL,
    total_idle_min    float         NULL,
    avg_idle_min      float         NULL,
    total_fuel_l      float         NULL,
    alerts_in_hub     int           NOT NULL
);
GO
IF OBJECT_ID('dbo.gold_idling_vehicle','U') IS NULL
CREATE TABLE dbo.gold_idling_vehicle (
    fleet_name      nvarchar(200) NOT NULL,
    vehicle_plate   varchar(50)   NULL,
    alerts          int           NOT NULL,
    total_idle_min  float         NULL,
    avg_idle_min    float         NULL,
    total_fuel_l    float         NULL,
    last_alert_ist  datetime2(3)  NULL
);
GO
IF OBJECT_ID('dbo.gold_idling_hub','U') IS NULL
CREATE TABLE dbo.gold_idling_hub (
    fleet_name      nvarchar(200) NOT NULL,
    hub_name        nvarchar(200) NULL,
    alerts          int           NOT NULL,
    total_idle_min  float         NULL,
    avg_hub_dist_m  float         NULL
);
GO
IF OBJECT_ID('dbo.gold_idling_hourly','U') IS NULL
CREATE TABLE dbo.gold_idling_hourly (
    fleet_name      nvarchar(200) NOT NULL,
    alert_hour      tinyint       NOT NULL,
    alerts          int           NOT NULL,
    total_idle_min  float         NULL
);
GO

/* ---------- 6. STORED PROCEDURES ---------- */

-- Bronze -> Silver: reads ONLY the given batch (default: latest), MERGE on alert_id => no duplicates
CREATE OR ALTER PROCEDURE dbo.usp_bronze_to_silver
    @batch_id varchar(20) = NULL
AS
BEGIN
    SET NOCOUNT ON;

    IF @batch_id IS NULL
        SELECT @batch_id = MAX(_batch_id) FROM dbo.bronze_idling_alerts;
    IF @batch_id IS NULL RETURN;   -- nothing loaded yet

    ;WITH src AS (
        SELECT b.*,
               ROW_NUMBER() OVER (PARTITION BY b.alert_id ORDER BY b.bronze_id DESC) AS rn
        FROM dbo.bronze_idling_alerts AS b
        WHERE b._batch_id = @batch_id
          AND b.alert_id IS NOT NULL
          AND b.ts_ms    IS NOT NULL
    ),
    clean AS (
        SELECT
            s.alert_id,
            s.account_id,
            s.alert_type,
            s.vehicle_id,
            s.vehicle_plate,
            DATEADD(MILLISECOND, CAST(s.ts_ms % 1000 AS int),
                DATEADD(SECOND, CAST(s.ts_ms / 1000 AS int), CAST('19700101' AS datetime2(3)))) AS alert_ts_utc,
            s.latitude, s.longitude, s.address,
            s.duration_sec,
            s.duration_sec / 60.0 AS duration_min,
            s.idle_threshold_sec,
            s.fuel_consumed AS fuel_litres,
            NULLIF(LTRIM(RTRIM(s.hub_name)), '') AS hub_name,   -- vendor sends a leading space
            s.hub_distance AS hub_distance_m,
            s.inside_hub,
            s._batch_id
        FROM src AS s
        WHERE s.rn = 1
    )
    MERGE dbo.silver_idling_alerts AS t
    USING (
        SELECT c.*,
               DATEADD(MINUTE, 330, c.alert_ts_utc)                        AS alert_ts_ist,
               CAST(DATEADD(MINUTE, 330, c.alert_ts_utc) AS date)          AS alert_date,
               CAST(DATEPART(HOUR, DATEADD(MINUTE, 330, c.alert_ts_utc)) AS tinyint) AS alert_hour
        FROM clean AS c
    ) AS s
      ON t.alert_id = s.alert_id
    WHEN MATCHED THEN UPDATE SET
        t.account_id         = s.account_id,
        t.alert_type         = s.alert_type,
        t.vehicle_id         = s.vehicle_id,
        t.vehicle_plate      = s.vehicle_plate,
        t.alert_ts_utc       = s.alert_ts_utc,
        t.alert_ts_ist       = s.alert_ts_ist,
        t.alert_date         = s.alert_date,
        t.alert_hour         = s.alert_hour,
        t.latitude           = s.latitude,
        t.longitude          = s.longitude,
        t.address            = s.address,
        t.duration_sec       = s.duration_sec,
        t.duration_min       = s.duration_min,
        t.idle_threshold_sec = s.idle_threshold_sec,
        t.fuel_litres        = s.fuel_litres,
        t.hub_name           = s.hub_name,
        t.hub_distance_m     = s.hub_distance_m,
        t.inside_hub         = s.inside_hub,
        t._batch_id          = s._batch_id,
        t._updated_at        = SYSUTCDATETIME()
    WHEN NOT MATCHED BY TARGET THEN INSERT (
        alert_id, account_id, alert_type, vehicle_id, vehicle_plate,
        alert_ts_utc, alert_ts_ist, alert_date, alert_hour,
        latitude, longitude, address, duration_sec, duration_min, idle_threshold_sec,
        fuel_litres, hub_name, hub_distance_m, inside_hub, _batch_id)
    VALUES (
        s.alert_id, s.account_id, s.alert_type, s.vehicle_id, s.vehicle_plate,
        s.alert_ts_utc, s.alert_ts_ist, s.alert_date, s.alert_hour,
        s.latitude, s.longitude, s.address, s.duration_sec, s.duration_min, s.idle_threshold_sec,
        s.fuel_litres, s.hub_name, s.hub_distance_m, s.inside_hub, s._batch_id);
END
GO

-- Silver -> Gold: rebuilt in one transaction so Power BI never sees half-empty tables
CREATE OR ALTER PROCEDURE dbo.usp_build_gold
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;
    BEGIN TRAN;

    DELETE FROM dbo.gold_idling_daily;
    INSERT INTO dbo.gold_idling_daily
        (alert_date, fleet_name, alerts, vehicles_affected, total_idle_min, avg_idle_min, total_fuel_l, alerts_in_hub)
    SELECT s.alert_date, f.fleet_name,
           COUNT(*), COUNT(DISTINCT s.vehicle_plate),
           SUM(s.duration_min), AVG(s.duration_min), SUM(s.fuel_litres),
           SUM(CASE WHEN s.inside_hub = 1 THEN 1 ELSE 0 END)
    FROM dbo.silver_idling_alerts AS s
    JOIN dbo.dim_fleet AS f ON f.account_id = s.account_id
    GROUP BY s.alert_date, f.fleet_name;

    DELETE FROM dbo.gold_idling_vehicle;
    INSERT INTO dbo.gold_idling_vehicle
        (fleet_name, vehicle_plate, alerts, total_idle_min, avg_idle_min, total_fuel_l, last_alert_ist)
    SELECT f.fleet_name, s.vehicle_plate,
           COUNT(*), SUM(s.duration_min), AVG(s.duration_min), SUM(s.fuel_litres), MAX(s.alert_ts_ist)
    FROM dbo.silver_idling_alerts AS s
    JOIN dbo.dim_fleet AS f ON f.account_id = s.account_id
    GROUP BY f.fleet_name, s.vehicle_plate;

    DELETE FROM dbo.gold_idling_hub;
    INSERT INTO dbo.gold_idling_hub (fleet_name, hub_name, alerts, total_idle_min, avg_hub_dist_m)
    SELECT f.fleet_name, s.hub_name,
           COUNT(*), SUM(s.duration_min), AVG(s.hub_distance_m)
    FROM dbo.silver_idling_alerts AS s
    JOIN dbo.dim_fleet AS f ON f.account_id = s.account_id
    GROUP BY f.fleet_name, s.hub_name;

    DELETE FROM dbo.gold_idling_hourly;
    INSERT INTO dbo.gold_idling_hourly (fleet_name, alert_hour, alerts, total_idle_min)
    SELECT f.fleet_name, s.alert_hour, COUNT(*), SUM(s.duration_min)
    FROM dbo.silver_idling_alerts AS s
    JOIN dbo.dim_fleet AS f ON f.account_id = s.account_id
    GROUP BY f.fleet_name, s.alert_hour;

    COMMIT;
END
GO

/* ---------- 7. SEED YOUR FLEETS (edit, then run) ----------
   Copy real account_id / fleet_name from your Fabric dim_fleet.
   Only is_active = 1 fleets are loaded by the ETL.

INSERT INTO dbo.dim_fleet (account_id, fleet_name, is_active) VALUES
 ('ACCOUNT_ID_1', N'Fleet name 1', 1),
 ('ACCOUNT_ID_2', N'Fleet name 2', 1);
------------------------------------------------------------ */
