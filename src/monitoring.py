#!/usr/bin/env python3
"""
Ghost-Data Suite Monitoring & Observability Module
Enhanced with comprehensive metrics, health checks, and alerting
"""

import time
import logging
from datetime import datetime, timezone
from pathlib import Path
import threading

from prometheus_client import (
    Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST,
    CollectorRegistry
)
from flask import Response

# ---------------------------------------------------------------------------
# Metrics Registry
# ---------------------------------------------------------------------------

class GhostMetrics:
    """Centralized metrics collection for Ghost-Data Suite"""

    def __init__(self):
        self.registry = CollectorRegistry()

        # Request metrics
        self.http_requests_total = Counter(
            'ghost_http_requests_total',
            'Total HTTP requests',
            ['method', 'endpoint', 'status'],
            registry=self.registry
        )

        self.http_request_duration_seconds = Histogram(
            'ghost_http_request_duration_seconds',
            'HTTP request duration in seconds',
            ['method', 'endpoint'],
            buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
            registry=self.registry
        )

        # Scrape metrics
        self.scrape_requests_total = Counter(
            'ghost_scrape_requests_total',
            'Total scrape requests',
            ['status'],
            registry=self.registry
        )

        self.scrape_duration_seconds = Histogram(
            'ghost_scrape_duration_seconds',
            'Scrape operation duration',
            buckets=(1.0, 5.0, 10.0, 30.0, 60.0, 120.0),
            registry=self.registry
        )

        self.scrape_queue_size = Gauge(
            'ghost_scrape_queue_size',
            'Number of pending scrape targets',
            registry=self.registry
        )

        # Database metrics
        self.db_connections_active = Gauge(
            'ghost_db_connections_active',
            'Active database connections',
            registry=self.registry
        )

        self.db_query_duration_seconds = Histogram(
            'ghost_db_query_duration_seconds',
            'Database query duration',
            buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1.0),
            registry=self.registry
        )

        self.db_record_count = Gauge(
            'ghost_db_record_count',
            'Total records in database',
            ['table'],
            registry=self.registry
        )

        # Encryption metrics
        self.encryption_operations_total = Counter(
            'ghost_encryption_operations_total',
            'Encryption/decryption operations',
            ['operation'],
            registry=self.registry
        )

        self.encryption_key_rotation = Gauge(
            'ghost_encryption_key_last_rotation',
            'Timestamp of last key rotation',
            registry=self.registry
        )

        # Memory and performance
        self.process_memory_bytes = Gauge(
            'ghost_process_memory_bytes',
            'Process memory usage in bytes',
            registry=self.registry
        )

        self.process_cpu_seconds_total = Counter(
            'ghost_process_cpu_seconds_total',
            'Total CPU seconds consumed',
            registry=self.registry
        )

        # Socket connections
        self.active_sockets = Gauge(
            'ghost_active_sockets',
            'Active SocketIO connections',
            registry=self.registry
        )

        # Capture quality
        self.capture_size_bytes = Histogram(
            'ghost_capture_size_bytes',
            'Size of captured data',
            buckets=(100, 1000, 10000, 50000, 100000, 500000, 1000000),
            registry=self.registry
        )

        self.media_files_downloaded = Counter(
            'ghost_media_files_downloaded_total',
            'Media files downloaded',
            ['category'],
            registry=self.registry
        )

        self.media_download_failures = Counter(
            'ghost_media_download_failures_total',
            'Media download failures',
            ['category'],
            registry=self.registry
        )

        # Error tracking
        self.errors_total = Counter(
            'ghost_errors_total',
            'Total errors',
            ['type', 'severity'],
            registry=self.registry
        )

        # Plugin hooks
        self.hook_executions_total = Counter(
            'ghost_hook_executions_total',
            'Plugin hook executions',
            ['hook_name', 'stage'],
            registry=self.registry
        )

        # System info
        self.uptime_seconds = Gauge(
            'ghost_uptime_seconds',
            'Service uptime in seconds',
            registry=self.registry
        )

        self.start_time = time.time()

    def record_request(self, method: str, endpoint: str, status: int, duration: float):
        """Record an HTTP request"""
        self.http_requests_total.labels(method=method, endpoint=endpoint, status=status).inc()
        self.http_request_duration_seconds.labels(method=method, endpoint=endpoint).observe(duration)

    def record_scrape(self, status: str, duration: float, queue_size: int = 0):
        """Record a scrape operation"""
        self.scrape_requests_total.labels(status=status).inc()
        self.scrape_duration_seconds.observe(duration)
        self.scrape_queue_size.set(queue_size)

    def record_db_query(self, duration: float, table: str, record_count: int):
        """Record a database operation"""
        self.db_query_duration_seconds.observe(duration)
        self.db_record_count.labels(table=table).set(record_count)

    def record_capture(self, size_bytes: int):
        """Record a capture size"""
        self.capture_size_bytes.observe(size_bytes)

    def record_media_download(self, category: str, success: bool):
        """Record media download"""
        if success:
            self.media_files_downloaded.labels(category=category).inc()
        else:
            self.media_download_failures.labels(category=category).inc()

    def record_error(self, error_type: str, severity: str):
        """Record an error"""
        self.errors_total.labels(type=error_type, severity=severity).inc()

    def get_metrics(self) -> Response:
        """Return Prometheus metrics response"""
        return Response(
            generate_latest(self.registry),
            mimetype=CONTENT_TYPE_LATEST
        )

    def update_uptime(self):
        """Update uptime metric"""
        self.uptime_seconds.set(time.time() - self.start_time)


class HealthCheck:
    """Health check management"""

    def __init__(self):
        self.checks = {}
        self.last_check = {}
        self._lock = threading.Lock()

    def register(self, name: str, check_fn):
        """Register a health check function"""
        self.checks[name] = check_fn

    def run_all(self) -> dict:
        """Run all health checks"""
        results = {}
        with self._lock:
            for name, check_fn in self.checks.items():
                try:
                    result = check_fn()
                    results[name] = {
                        "status": "healthy" if result else "unhealthy",
                        "timestamp": datetime.now(timezone.utc).isoformat()
                    }
                except Exception as exc:
                    results[name] = {
                        "status": "error",
                        "error": str(exc),
                        "timestamp": datetime.now(timezone.utc).isoformat()
                    }
                self.last_check[name] = datetime.now(timezone.utc).isoformat()
        return results


class Logger:
    """Structured logging for Ghost-Data Suite"""

    def __init__(self, name: str, log_file: str = None, level: str = "INFO"):
        self.logger = logging.getLogger(name)
        self.logger.setLevel(getattr(logging, level.upper()))

        # Console handler
        console = logging.StreamHandler()
        console.setLevel(logging.INFO)
        formatter = logging.Formatter(
            '%(asctime)s | %(levelname)s | %(name)s | %(message)s'
        )
        console.setFormatter(formatter)
        self.logger.addHandler(console)

        # File handler
        if log_file:
            # Ensure the log directory exists; a fresh checkout has no logs/ dir
            # and logging.FileHandler raises FileNotFoundError without it.
            log_path = Path(log_file)
            try:
                log_path.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                log_path = None
            if log_path is not None:
                file_handler = logging.FileHandler(log_path)
                file_handler.setLevel(logging.DEBUG)
                file_formatter = logging.Formatter(
                    '%(asctime)s | %(levelname)s | %(name)s | %(message)s | %(filename)s:%(lineno)d'
                )
                file_handler.setFormatter(file_formatter)
                self.logger.addHandler(file_handler)

    def info(self, msg: str, **kwargs):
        self.logger.info(msg, extra=kwargs)

    def error(self, msg: str, **kwargs):
        self.logger.error(msg, extra=kwargs)

    def warning(self, msg: str, **kwargs):
        self.logger.warning(msg, extra=kwargs)

    def debug(self, msg: str, **kwargs):
        self.logger.debug(msg, extra=kwargs)


# Global instances
metrics = GhostMetrics()
health = HealthCheck()
logger = Logger("ghost-suite", log_file="logs/alchemist.log")
