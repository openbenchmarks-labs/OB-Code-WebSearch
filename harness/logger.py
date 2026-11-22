"""Write redacted tool request and response hops under each attempt's directory."""

from __future__ import annotations

import json
import re
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone

try:
    from datetime import UTC
except ImportError:
    UTC = timezone.utc
from pathlib import Path
from typing import Any

# UTC, filesystem-safe, lexicographically sortable. Example: 2026-08-24T16-29-16Z
RUN_STAMP_FMT = "%Y-%m-%dT%H-%M-%SZ"
RUN_STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z$")
_REQUEST_DIR: ContextVar[Path | None] = ContextVar("request_evidence_dir", default=None)


@contextmanager
def request_scope(path: Path | None):
    token = _REQUEST_DIR.set(path)
    try:
        yield
    finally:
        _REQUEST_DIR.reset(token)


def begin_request(kind: str, payload: dict, reservation_id: str = "") -> Path | None:
    root = _REQUEST_DIR.get()
    if root is None:
        return None
    dest = root / "physical" / f"{kind}-{reservation_id or uuid.uuid4().hex}"
    dest.mkdir(parents=True, exist_ok=False)
    write_json(dest / "request.json", payload)
    return dest


def end_request(path: Path | None, payload: dict) -> None:
    if path is not None:
        write_json(path / "outcome.json", payload)


def new_run_stamp() -> str:
    return datetime.now(UTC).strftime(RUN_STAMP_FMT)


def split_label(allow_fetch: bool, split: str = "") -> str:
    name = (split or "").strip().lower().replace("_", "-")
    if name in {"search-fetch", "search-only"}:
        return name
    return "search-fetch" if allow_fetch else "search-only"


def parse_run_stamp(raw: str) -> str:
    stamp = (raw or "").strip()
    if not stamp:
        return ""
    if not RUN_STAMP_RE.match(stamp):
        raise SystemExit(f"invalid --run-stamp {stamp!r}; expected {RUN_STAMP_FMT} e.g. 2026-08-24T16-29-16Z")
    return stamp


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n")


def write_hop_pair(hops_dir: Path, stem: str, request: Any, response: Any) -> dict[str, str]:
    req_path = hops_dir / f"{stem}.request.json"
    res_path = hops_dir / f"{stem}.response.json"
    write_json(req_path, request)
    write_json(res_path, response)
    return {"request": str(req_path), "response": str(res_path)}


def write_arm_summary(*, run: Any, row: dict[str, Any]) -> None:
    dest = run.evidence_dir
    if dest is None:
        raise ValueError("raw evidence requires an attempt-bound directory")
    write_json(dest / "run.json", {key: value for key, value in row.items() if key != "source"})
    if run.notes:
        write_json(dest / "notes.json", list(run.notes))
