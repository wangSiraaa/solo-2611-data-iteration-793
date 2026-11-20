"""SQLite persistence for revisioned power traces and append-only reports.

Two products are built on this store (see ``soc.py`` and ``warranty.py``):

* a *state-of-charge report* (:schema:`soc-report/v1`) derived from two power
  traces, a capacity, two efficiencies and an initial charge;
* a *warranty report* (:schema:`warranty-report/v1`) that classifies the
  charge swings of one of those SOC histories under a warranty contract.

Both are **immutable and content-addressed**.  A report's ``report_id`` is, by
default, a digest of the exact product, so *rebuilding the same physics can
never mint a different report* and a later power revision can never mutate an
already-cited report.  A later revision simply yields a different digest and
therefore a different report; the old document is still found by its old id.

Everything is stored as exact numerator/denominator text.  No float ever
reaches disk.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Mapping

from .errors import ConflictError, ValidationError
from .interval import Segment, frac_to_json
from .numbers import parse_frac

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS power_traces (
    trace_id TEXT NOT NULL,
    revision TEXT NOT NULL,
    segments_json TEXT NOT NULL,
    PRIMARY KEY (trace_id, revision)
);

CREATE TABLE IF NOT EXISTS current_traces (
    trace_id TEXT PRIMARY KEY,
    revision TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS soc_reports (
    report_id TEXT PRIMARY KEY,
    power_key TEXT NOT NULL,
    product_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS warranty_contracts (
    contract_id TEXT NOT NULL,
    revision TEXT NOT NULL,
    contract_json TEXT NOT NULL,
    PRIMARY KEY (contract_id, revision)
);

CREATE TABLE IF NOT EXISTS current_warranty_contracts (
    contract_id TEXT PRIMARY KEY,
    revision TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS warranty_reports (
    report_id TEXT PRIMARY KEY,
    soc_report_id TEXT NOT NULL,
    contract_key TEXT NOT NULL,
    product_json TEXT NOT NULL
);
"""


# --------------------------------------------------------------------------- #
# Exact (de)serialization for segments
# --------------------------------------------------------------------------- #
def _seg_to_obj(s: Segment) -> dict:
    return {
        "segment_id": s.segment_id,
        "start": frac_to_json(s.start),
        "end": frac_to_json(s.end),
        "power": frac_to_json(s.power),
    }


def _seg_from_obj(o: Mapping[str, Any]) -> Segment:
    return Segment(
        segment_id=str(o["segment_id"]),
        start=parse_frac(o["start"], "start"),
        end=parse_frac(o["end"], "end"),
        power=parse_frac(o["power"], "power"),
    )


def segments_from_json(obj: Mapping[str, Any]) -> list[Segment]:
    return [_seg_from_obj(node) for node in obj.get("segments", [])]


def _canon(segments: list[Segment]) -> str:
    """Stable text encoding used for content addressing."""
    return json.dumps(
        [_seg_to_obj(s) for s in segments],
        sort_keys=True,
        separators=(",", ":"),
    )


def segments_digest(segments: list[Segment]) -> str:
    import hashlib

    return hashlib.sha256(_canon(segments).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #
@dataclass
class PowerContext:
    """A resolved pair of current traces plus their identity key."""

    charge_trace_id: str
    charge_revision: str
    discharge_trace_id: str
    discharge_revision: str
    charge_segments: list[Segment]
    discharge_segments: list[Segment]

    @property
    def key(self) -> str:
        return json.dumps(
            {
                "charge": [self.charge_trace_id, self.charge_revision,
                           segments_digest(self.charge_segments)],
                "discharge": [self.discharge_trace_id, self.discharge_revision,
                              segments_digest(self.discharge_segments)],
            },
            sort_keys=True,
            separators=(",", ":"),
        )


class ReportStore:
    """A single SQLite-backed store for traces, SOC reports and warranty reports."""

    def __init__(self, path: str = ":memory:", *, busy_timeout_ms: int = 15000):
        self.path = path
        self._conn = sqlite3.connect(path, isolation_level=None,
                                     check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
        self._conn.executescript(SCHEMA_SQL)

    # -- lifecycle -------------------------------------------------------- #
    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "ReportStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @contextmanager
    def _write_tx(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    # ------------------------------------------------------------------ #
    # Power traces
    # ------------------------------------------------------------------ #
    def register_trace(self, trace_id: str, revision: str,
                       segments: list[Segment], *, make_current: bool = True) -> None:
        """Record a trace revision exactly (validated by the caller's physics).

        Re-registering identical content is a no-op; registering the *same*
        (trace_id, revision) with *different* content is a conflict, so a
        revision id can never silently mean two different trajectories.
        """
        if not trace_id or not revision:
            raise ValidationError("trace_id and revision are required")
        payload = _canon(segments)
        with self._write_tx() as conn:
            row = conn.execute(
                "SELECT segments_json FROM power_traces WHERE trace_id=? AND revision=?",
                (trace_id, revision),
            ).fetchone()
            if row is not None:
                if row["segments_json"] != payload:
                    raise ConflictError(
                        f"trace {trace_id!r} revision {revision!r} already exists "
                        "with different segments"
                    )
            else:
                conn.execute(
                    "INSERT INTO power_traces(trace_id, revision, segments_json) "
                    "VALUES(?,?,?)",
                    (trace_id, revision, payload),
                )
            if make_current:
                conn.execute(
                    "INSERT INTO current_traces(trace_id, revision) VALUES(?,?) "
                    "ON CONFLICT(trace_id) DO UPDATE SET revision=excluded.revision",
                    (trace_id, revision),
                )

    def set_current_trace(self, trace_id: str, revision: str) -> None:
        with self._write_tx() as conn:
            row = conn.execute(
                "SELECT 1 FROM power_traces WHERE trace_id=? AND revision=?",
                (trace_id, revision),
            ).fetchone()
            if row is None:
                raise ValidationError(f"unknown trace revision {trace_id!r}/{revision!r}")
            conn.execute(
                "INSERT INTO current_traces(trace_id, revision) VALUES(?,?) "
                "ON CONFLICT(trace_id) DO UPDATE SET revision=excluded.revision",
                (trace_id, revision),
            )

    def current_trace_revision(self, trace_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT revision FROM current_traces WHERE trace_id=?", (trace_id,)
        ).fetchone()
        return None if row is None else row["revision"]

    def get_trace(self, trace_id: str, revision: str) -> list[Segment]:
        row = self._conn.execute(
            "SELECT segments_json FROM power_traces WHERE trace_id=? AND revision=?",
            (trace_id, revision),
        ).fetchone()
        if row is None:
            raise ValidationError(f"unknown trace revision {trace_id!r}/{revision!r}")
        return [_seg_from_obj(o) for o in json.loads(row["segments_json"])]

    def resolve_context(self, charge_trace_id: str, discharge_trace_id: str, *,
                        charge_revision: str | None = None,
                        discharge_revision: str | None = None) -> PowerContext:
        """Resolve current (or explicit) revisions into a :class:`PowerContext`."""
        cr = charge_revision or self.current_trace_revision(charge_trace_id)
        dr = discharge_revision or self.current_trace_revision(discharge_trace_id)
        if cr is None:
            raise ValidationError(f"trace {charge_trace_id!r} has no current revision")
        if dr is None:
            raise ValidationError(f"trace {discharge_trace_id!r} has no current revision")
        return PowerContext(
            charge_trace_id=charge_trace_id,
            charge_revision=cr,
            discharge_trace_id=discharge_trace_id,
            discharge_revision=dr,
            charge_segments=self.get_trace(charge_trace_id, cr),
            discharge_segments=self.get_trace(discharge_trace_id, dr),
        )

    # ------------------------------------------------------------------ #
    # SOC reports
    # ------------------------------------------------------------------ #
    def save_soc_report(self, product: dict, *, power_key: str) -> str:
        """Store an immutable SOC report; return its stable report_id."""
        report_id = product.get("report_id")
        if not report_id:
            report_id = _digest(product)
            product = {**product, "report_id": report_id}
        payload = json.dumps(product, sort_keys=True, separators=(",", ":"))
        with self._write_tx() as conn:
            row = conn.execute(
                "SELECT product_json FROM soc_reports WHERE report_id=?",
                (report_id,),
            ).fetchone()
            if row is not None:
                if row["product_json"] != payload:
                    raise ConflictError(
                        f"soc report {report_id!r} already exists with different content"
                    )
                return report_id
            conn.execute(
                "INSERT INTO soc_reports(report_id, power_key, product_json) "
                "VALUES(?,?,?)",
                (report_id, power_key, payload),
            )
        return report_id

    def get_soc_report(self, report_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT product_json FROM soc_reports WHERE report_id=?", (report_id,)
        ).fetchone()
        return None if row is None else json.loads(row["product_json"])

    def find_soc_report(self, product: dict) -> str | None:
        """Look up a report id for the same exact product, ignoring report_id."""
        rid = _digest({k: v for k, v in product.items() if k != "report_id"})
        return rid if self.get_soc_report(rid) is not None else None

    # ------------------------------------------------------------------ #
    # Warranty contracts
    # ------------------------------------------------------------------ #
    def register_warranty_contract(self, contract, *, make_current: bool = True) -> None:
        payload = json.dumps(contract.to_json(), sort_keys=True,
                             separators=(",", ":"))
        with self._write_tx() as conn:
            row = conn.execute(
                "SELECT contract_json FROM warranty_contracts "
                "WHERE contract_id=? AND revision=?",
                (contract.contract_id, contract.revision),
            ).fetchone()
            if row is not None:
                if row["contract_json"] != payload:
                    raise ConflictError(
                        f"warranty contract {contract.contract_id!r} revision "
                        f"{contract.revision!r} already exists with different content"
                    )
            else:
                conn.execute(
                    "INSERT INTO warranty_contracts(contract_id, revision, "
                    "contract_json) VALUES(?,?,?)",
                    (contract.contract_id, contract.revision, payload),
                )
            if make_current:
                conn.execute(
                    "INSERT INTO current_warranty_contracts(contract_id, revision) "
                    "VALUES(?,?) ON CONFLICT(contract_id) DO UPDATE SET "
                    "revision=excluded.revision",
                    (contract.contract_id, contract.revision),
                )

    def current_warranty_contract_revision(self, contract_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT revision FROM current_warranty_contracts WHERE contract_id=?",
            (contract_id,),
        ).fetchone()
        return None if row is None else row["revision"]

    def get_warranty_contract_json(self, contract_id: str, revision: str) -> dict:
        row = self._conn.execute(
            "SELECT contract_json FROM warranty_contracts "
            "WHERE contract_id=? AND revision=?",
            (contract_id, revision),
        ).fetchone()
        if row is None:
            raise ValidationError(
                f"unknown warranty contract {contract_id!r}/{revision!r}")
        return json.loads(row["contract_json"])

    # ------------------------------------------------------------------ #
    # Warranty reports
    # ------------------------------------------------------------------ #
    def save_warranty_report(self, product: dict, *, soc_report_id: str,
                             contract_key: str) -> str:
        report_id = product.get("report_id")
        if not report_id:
            report_id = _digest(product)
            product = {**product, "report_id": report_id}
        payload = json.dumps(product, sort_keys=True, separators=(",", ":"))
        with self._write_tx() as conn:
            row = conn.execute(
                "SELECT product_json FROM warranty_reports WHERE report_id=?",
                (report_id,),
            ).fetchone()
            if row is not None:
                if row["product_json"] != payload:
                    raise ConflictError(
                        f"warranty report {report_id!r} already exists with "
                        "different content"
                    )
                return report_id
            conn.execute(
                "INSERT INTO warranty_reports(report_id, soc_report_id, "
                "contract_key, product_json) VALUES(?,?,?,?)",
                (report_id, soc_report_id, contract_key, payload),
            )
        return report_id

    def get_warranty_report(self, report_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT product_json FROM warranty_reports WHERE report_id=?",
            (report_id,),
        ).fetchone()
        return None if row is None else json.loads(row["product_json"])

    def warranty_reports_for_soc(self, soc_report_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT product_json FROM warranty_reports WHERE soc_report_id=? "
            "ORDER BY report_id",
            (soc_report_id,),
        ).fetchall()
        return [json.loads(r["product_json"]) for r in rows]


def _digest(product: dict) -> str:
    import hashlib

    payload = {k: v for k, v in product.items() if k != "report_id"}
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
