"""
KARA Bot — Structured JSON Logging for Railway/Cloud Deployment.

Activated when env RAILWAY_ENVIRONMENT is set OR LOG_FORMAT=json.
Each log line becomes a single JSON object — parseable by Railway, Datadog, Better Stack, etc.
"""

import logging
import json
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    """Single-line JSON formatter for cloud log aggregators."""

    def format(self, record):
        log_obj = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "funcName": record.funcName,
            "lineno": record.lineno,
        }
        # Merge extra fields if present
        if hasattr(record, "asset"):
            log_obj["asset"] = record.asset
        if hasattr(record, "position_id"):
            log_obj["position_id"] = record.position_id
        if hasattr(record, "score"):
            log_obj["score"] = record.score
        if record.exc_info and record.exc_info[1]:
            log_obj["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_obj, default=str)


def setup_json_logging():
    """Replace all root handlers with a single JSON StreamHandler."""
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
