"""Partition, rollup, and retention maintenance for live telemetry."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone

from sqlalchemy import Engine, text

ADVISORY_LOCK_KEY = 0x454E45524759  # "ENERGY"

ROLLUP_SQL = text(
    """
    WITH bucketed AS (
        SELECT
            date_bin(make_interval(secs => :resolution), sampled_at, timestamptz '1970-01-01') AS bucket_start,
            source_id,
            metric_code,
            sampled_at,
            value_number,
            value_text,
            value_boolean
        FROM telemetry_samples
        WHERE sampled_at >= :range_start AND sampled_at < :range_end
    ), ordered AS (
        SELECT *,
            row_number() OVER (PARTITION BY bucket_start, source_id, metric_code ORDER BY sampled_at) AS position,
            lag(value_number) OVER (PARTITION BY bucket_start, source_id, metric_code ORDER BY sampled_at) AS previous_number,
            lag(value_text) OVER (PARTITION BY bucket_start, source_id, metric_code ORDER BY sampled_at) AS previous_text,
            lag(value_boolean) OVER (PARTITION BY bucket_start, source_id, metric_code ORDER BY sampled_at) AS previous_boolean
        FROM bucketed
    ), aggregate_rows AS (
        SELECT
            bucket_start,
            source_id,
            metric_code,
            min(value_number) AS minimum_number,
            max(value_number) AS maximum_number,
            avg(value_number) AS average_number,
            (array_agg(value_number ORDER BY sampled_at DESC) FILTER (WHERE value_number IS NOT NULL))[1] AS last_number,
            (array_agg(value_text ORDER BY sampled_at DESC) FILTER (WHERE value_text IS NOT NULL))[1] AS last_text,
            (array_agg(value_boolean ORDER BY sampled_at DESC) FILTER (WHERE value_boolean IS NOT NULL))[1] AS last_boolean,
            count(*) AS sample_count,
            count(*) FILTER (
                WHERE position > 1 AND
                ROW(value_number, value_text, value_boolean)
                    IS DISTINCT FROM ROW(previous_number, previous_text, previous_boolean)
            ) AS change_count
        FROM ordered
        GROUP BY bucket_start, source_id, metric_code
    )
    INSERT INTO telemetry_rollups (
        bucket_start, resolution_seconds, source_id, metric_code, bucket_end,
        minimum_number, maximum_number, average_number,
        last_number, last_text, last_boolean, sample_count, change_count, generated_at
    )
    SELECT
        bucket_start, :resolution, source_id, metric_code,
        bucket_start + make_interval(secs => :resolution),
        minimum_number, maximum_number, average_number,
        last_number, last_text, last_boolean, sample_count, change_count, now()
    FROM aggregate_rows
    ON CONFLICT (bucket_start, resolution_seconds, source_id, metric_code) DO UPDATE SET
        bucket_end = excluded.bucket_end,
        minimum_number = excluded.minimum_number,
        maximum_number = excluded.maximum_number,
        average_number = excluded.average_number,
        last_number = excluded.last_number,
        last_text = excluded.last_text,
        last_boolean = excluded.last_boolean,
        sample_count = excluded.sample_count,
        change_count = excluded.change_count,
        generated_at = now()
    """
)


@dataclass(frozen=True, slots=True)
class TelemetryStorageStats:
    raw_row_count: int
    oldest_sample: datetime | None
    rollup_row_count: int


class TelemetryMaintenance:
    def __init__(self, engine: Engine, *, retention_days: int = 30, partition_days_ahead: int = 7):
        self.engine = engine
        self.retention_days = retention_days
        self.partition_days_ahead = partition_days_ahead

    def run(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("maintenance time must be timezone-aware")
        now = now.astimezone(timezone.utc)
        with self.engine.begin() as connection:
            locked = connection.execute(
                text("SELECT pg_try_advisory_xact_lock(:lock_key)"), {"lock_key": ADVISORY_LOCK_KEY}
            ).scalar_one()
            if not locked:
                return False
            for offset in range(-1, self.partition_days_ahead + 1):
                connection.execute(text("SELECT ensure_telemetry_partition(:day)"), {"day": now.date() + timedelta(days=offset)})

            closed_minute = now.replace(second=0, microsecond=0)
            self._roll_up(
                connection,
                closed_minute - timedelta(hours=2),
                closed_minute,
                resolutions=(60,),
            )
            closed_hour = closed_minute.replace(minute=0)
            self._roll_up(
                connection,
                closed_hour - timedelta(hours=2),
                closed_hour,
                resolutions=(3600,),
            )

            cutoff = now.date() - timedelta(days=self.retention_days)
            partition_names = connection.execute(
                text(
                    """
                    SELECT child.relname
                    FROM pg_inherits
                    JOIN pg_class parent ON pg_inherits.inhparent = parent.oid
                    JOIN pg_class child ON pg_inherits.inhrelid = child.oid
                    WHERE parent.relname = 'telemetry_samples'
                      AND child.relname ~ '^telemetry_samples_[0-9]{8}$'
                    """
                )
            ).scalars()
            for partition_name in partition_names:
                partition_day = datetime.strptime(partition_name.removeprefix("telemetry_samples_"), "%Y%m%d").date()
                if partition_day >= cutoff:
                    continue
                day_start = datetime.combine(partition_day, time.min, timezone.utc)
                day_end = day_start + timedelta(days=1)
                self._roll_up(connection, day_start, day_end)
                uncovered = connection.execute(
                    text(
                        """
                        SELECT EXISTS (
                            SELECT 1 FROM telemetry_samples raw
                            WHERE raw.sampled_at >= :day_start AND raw.sampled_at < :day_end
                              AND (
                                NOT EXISTS (
                                    SELECT 1 FROM telemetry_rollups minute
                                    WHERE minute.resolution_seconds = 60
                                      AND minute.bucket_start = date_bin(interval '1 minute', raw.sampled_at, timestamptz '1970-01-01')
                                      AND minute.source_id = raw.source_id AND minute.metric_code = raw.metric_code
                                ) OR NOT EXISTS (
                                    SELECT 1 FROM telemetry_rollups hour
                                    WHERE hour.resolution_seconds = 3600
                                      AND hour.bucket_start = date_bin(interval '1 hour', raw.sampled_at, timestamptz '1970-01-01')
                                      AND hour.source_id = raw.source_id AND hour.metric_code = raw.metric_code
                                )
                              )
                        )
                        """
                    ),
                    {"day_start": day_start, "day_end": day_end},
                ).scalar_one()
                if not uncovered:
                    # partition_name is derived only from the catalog and validated by the regex above.
                    connection.exec_driver_sql(f'DROP TABLE "{partition_name}"')
        return True

    @staticmethod
    def _roll_up(
        connection,
        start: datetime,
        end: datetime,
        *,
        resolutions: tuple[int, ...] = (60, 3600),
    ) -> None:
        for resolution in resolutions:
            connection.execute(
                ROLLUP_SQL,
                {"resolution": resolution, "range_start": start, "range_end": end},
            )

    def storage_stats(self) -> TelemetryStorageStats:
        with self.engine.connect() as connection:
            raw_count, oldest = connection.execute(
                text("SELECT count(*), min(sampled_at) FROM telemetry_samples")
            ).one()
            rollup_count = connection.execute(text("SELECT count(*) FROM telemetry_rollups")).scalar_one()
        return TelemetryStorageStats(raw_count, oldest, rollup_count)
