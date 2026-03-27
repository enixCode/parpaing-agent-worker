"""Prometheus metrics - shared across tower modules."""

from prometheus_client import Counter, Gauge, Histogram

JOBS_TOTAL = Counter("tower_jobs_total", "Total jobs created", ["profile"])
JOBS_ACTIVE = Gauge("tower_jobs_active", "Currently running jobs")
JOBS_BY_STATUS = Gauge("tower_jobs_by_status", "Jobs per status", ["status"])
POOL_READY = Gauge("tower_pool_ready", "Warm containers ready in pool")
JOB_DURATION = Histogram(
    "tower_job_duration_seconds", "Job execution time",
    buckets=[5, 15, 30, 60, 120, 300, 600, 1800, 3600],
)
