"""Structured JSON logs and Prometheus metrics."""

import json
import logging
import time
from collections.abc import Awaitable, Callable, Iterator

from fastapi import Request, Response
from prometheus_client import Counter, Histogram
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

from app import ipam
from app.db import get_sessionmaker

REQUESTS = Counter("netlab_http_requests_total", "HTTP requests", ["method", "route", "status"])
LATENCY = Histogram("netlab_http_request_duration_seconds", "HTTP latency", ["method", "route"])
ALLOCATIONS = Counter("netlab_ip_allocations_total", "Addresses allocated", ["result"])

log = logging.getLogger("netlab")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        payload.update(getattr(record, "extra_fields", {}))
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    logging.getLogger("uvicorn.access").disabled = True  # replaced by access_log middleware


async def access_log(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    start = time.perf_counter()
    response = await call_next(request)
    elapsed = time.perf_counter() - start
    route = getattr(request.scope.get("route"), "path", "unmatched")
    REQUESTS.labels(request.method, route, str(response.status_code)).inc()
    LATENCY.labels(request.method, route).observe(elapsed)
    if route != "/metrics":
        log.info(
            "request",
            extra={
                "extra_fields": {
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "duration_ms": round(elapsed * 1000, 2),
                }
            },
        )
    return response


class SubnetCollector(Collector):
    """Subnet utilisation gauges, computed from the database at scrape time."""

    def collect(self) -> Iterator[GaugeMetricFamily]:
        util = GaugeMetricFamily(
            "netlab_subnet_utilization_ratio", "Used/usable addresses", labels=["subnet", "prefix"]
        )
        free = GaugeMetricFamily(
            "netlab_subnet_free_addresses", "Free usable addresses", labels=["subnet", "prefix"]
        )
        try:
            with get_sessionmaker()() as session:
                for s in ipam.list_subnets(session):
                    o = ipam.subnet_out(s)
                    util.add_metric([o.name, str(o.prefix)], o.utilization)
                    free.add_metric([o.name, str(o.prefix)], o.size - o.used)
        except Exception:  # metrics must never break the scrape
            log.exception("subnet collector failed")
        yield util
        yield free
