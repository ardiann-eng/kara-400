"""Read-only inventory for KARA production audit databases."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict


DATABASES = {
    "kara_data": "/data/kara_data.db",
    "kara_ml": "/data/kara_ml.db",
}


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def json_inventory(conn: sqlite3.Connection, table: str, column: str) -> dict:
    present = Counter()
    types: dict[str, Counter] = defaultdict(Counter)
    invalid = 0
    empty = 0
    rows = conn.execute(f'SELECT "{column}" FROM "{table}"').fetchall()
    for row in rows:
        raw = row[column]
        if raw in (None, "", "{}"):
            empty += 1
            continue
        try:
            value = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, json.JSONDecodeError):
            invalid += 1
            continue
        if not isinstance(value, dict):
            invalid += 1
            continue
        for key, item in value.items():
            present[key] += 1
            types[key][type(item).__name__] += 1
    return {
        "rows": len(rows),
        "empty": empty,
        "invalid": invalid,
        "fields": {
            key: {"present": count, "types": dict(types[key])}
            for key, count in sorted(present.items())
        },
    }


def inventory(name: str, path: str) -> dict:
    conn = connect(path)
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        )
    ]
    result = {
        "database": name,
        "integrity": conn.execute("PRAGMA integrity_check").fetchone()[0],
        "tables": {},
    }
    for table in tables:
        columns = [dict(row) for row in conn.execute(f'PRAGMA table_info("{table}")')]
        column_names = {column["name"] for column in columns}
        table_result = {
            "count": conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0],
            "columns": columns,
        }
        time_columns = [
            column
            for column in ("created_at", "timestamp", "closed_at", "updated_at")
            if column in column_names
        ]
        if time_columns:
            table_result["ranges"] = {
                column: dict(
                    conn.execute(
                        f'SELECT MIN("{column}") AS min, MAX("{column}") AS max '
                        f'FROM "{table}"'
                    ).fetchone()
                )
                for column in time_columns
            }
        json_columns = [
            column for column in ("data", "features", "metadata") if column in column_names
        ]
        if json_columns:
            table_result["json"] = {
                column: json_inventory(conn, table, column) for column in json_columns
            }
        result["tables"][table] = table_result
    conn.close()
    return result


print(
    json.dumps(
        [inventory(name, path) for name, path in DATABASES.items()],
        indent=2,
        sort_keys=True,
    )
)
