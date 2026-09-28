"""Tiny JSONL decision logger."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any


def json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"not json serializable: {type(value)}")


class DecisionLogger:
    def __init__(self, path: Path, level: str = "INFO") -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(
            level=getattr(logging, level.upper(), logging.INFO),
            format="%(asctime)s %(levelname)s %(message)s",
        )
        self._log = logging.getLogger("polymarket_bot")
        self._fh = self.path.open("a", encoding="utf-8")

    def log(self, action: str, **fields: Any) -> dict[str, Any]:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "action": action,
            **fields,
        }
        self._fh.write(json.dumps(record, default=json_default) + "\n")
        self._fh.flush()
        summary = {k: record[k] for k in list(record)[:8]}
        self._log.info("%s %s", action, summary)
        return record

    def close(self) -> None:
        self._fh.close()
