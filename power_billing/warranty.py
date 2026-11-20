"""Warranty cycle accounting over a state-of-charge history.

A warranty contract assigns a *complete-cycle consumption* to depth tiers whose
upper bounds increase and together cover the capacity.  Given the turning points
of one provable history run, the cycle decomposition is:

#. Take the sequence of turning values (equal plateaus and same-direction
   intermediate points already removed, endpoints kept).
#. Repeatedly take the **leftmost four** consecutive turning values ``A,B,C,D``.
   When ``|B-C|`` is no greater than both ``|A-B|`` and ``|C-D|``, ``B`` and
   ``C`` form one **complete cycle** of depth ``|B-C|`` and both are removed.
   Otherwise skip and advance by one.  After a removal, the search restarts.
#. When nothing more can be removed, every adjacent pair of remaining endpoints
   is one **half cycle** of depth ``|difference|``.

Usage of a complete cycle of depth ``d`` is the tier fee prorated linearly by
depth against the tier's upper bound: ``fee * d / tier.max_depth``.  A half cycle
consumes half of that.  (With tiers ``[(2, 1), (6, 3)]`` this reproduces the
worked example: a depth-2 complete cycle costs 1; a depth-6 half costs 1.5; a
depth-4 half costs 1.)

The decomposition depends only on the charge history, never on the contract, so
**switching contracts recomputes only the costs**, reusing the same endpoints.

Each history *run* is processed independently: the two sides of a data gap are
never joined into one cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Iterable, Mapping

from .errors import ContractError
from .interval import ZERO, frac_to_json
from .numbers import parse_frac

SCHEMA = "warranty-report/v1"


@dataclass(frozen=True, slots=True)
class Tier:
    max_depth: Fraction
    full_cycle_cost: Fraction

    @classmethod
    def from_json(cls, obj: Mapping) -> "Tier":
        try:
            t = cls(parse_frac(obj["max_depth"], "max_depth"),
                    parse_frac(obj["full_cycle_cost"], "full_cycle_cost"))
        except KeyError as exc:
            raise ContractError(f"tier missing field {exc}") from exc
        if t.max_depth <= 0:
            raise ContractError("tier max_depth must be positive")
        if t.full_cycle_cost < 0:
            raise ContractError("tier full_cycle_cost must be non-negative")
        return t


@dataclass(frozen=True, slots=True)
class WarrantyContract:
    contract_id: str
    revision: str
    tiers: tuple[Tier, ...]

    @classmethod
    def from_json(cls, obj: Mapping) -> "WarrantyContract":
        if "tiers" not in obj:
            raise ContractError("warranty contract requires 'tiers'")
        tiers = tuple(Tier.from_json(t) for t in obj["tiers"])
        c = cls(str(obj["contract_id"]), str(obj["revision"]), tiers)
        c.validate_structure()
        return c

    def validate_structure(self) -> None:
        if not self.contract_id:
            raise ContractError("contract_id is required")
        if not self.tiers:
            raise ContractError("at least one depth tier is required")
        prev = ZERO
        for t in self.tiers:
            if t.max_depth <= prev:
                raise ContractError("tier max_depth bounds must strictly increase")
            prev = t.max_depth

    def validate_covers(self, capacity: Fraction) -> None:
        self.validate_structure()
        if self.tiers[-1].max_depth < capacity:
            raise ContractError(
                f"tiers cover up to {self.tiers[-1].max_depth}, less than "
                f"capacity {capacity}")

    def tier_for(self, depth: Fraction) -> Tier:
        for t in self.tiers:
            if depth <= t.max_depth:
                return t
        return self.tiers[-1]

    def full_cycle_cost(self, depth: Fraction) -> Fraction:
        """Consumption of one complete cycle of the given depth."""
        if depth <= 0:
            return ZERO
        t = self.tier_for(depth)
        return t.full_cycle_cost * depth / t.max_depth

    def half_cycle_cost(self, depth: Fraction) -> Fraction:
        return self.full_cycle_cost(depth) / 2

    def to_json(self) -> dict:
        return {
            "contract_id": self.contract_id,
            "revision": self.revision,
            "tiers": [
                {"max_depth": frac_to_json(t.max_depth),
                 "full_cycle_cost": frac_to_json(t.full_cycle_cost)}
                for t in self.tiers
            ],
        }


# --------------------------------------------------------------------------- #
# Cycle decomposition
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class _Node:
    time: Fraction
    charge: Fraction


def decompose(points: Iterable[tuple[Fraction, Fraction]]) -> tuple[list[dict], list[dict]]:
    """Return ``(complete_cycles, half_cycles)`` as raw endpoint records.

    ``points`` are the turning points of one run, already compressed.
    """
    nodes = [_Node(t, s) for t, s in points]
    complete: list[dict] = []

    while True:
        removed = False
        i = 0
        while i + 3 < len(nodes):
            a, b, c, d = nodes[i:i + 4]
            ab = abs(b.charge - a.charge)
            bc = abs(c.charge - b.charge)
            cd = abs(d.charge - c.charge)
            if bc <= ab and bc <= cd:
                complete.append({
                    "depth": bc,
                    "start": (b.time, b.charge),
                    "end": (c.time, c.charge),
                })
                del nodes[i + 1:i + 3]
                removed = True
                break
            i += 1
        if not removed:
            break

    half: list[dict] = []
    for x, y in zip(nodes, nodes[1:]):
        half.append({
            "depth": abs(y.charge - x.charge),
            "start": (x.time, x.charge),
            "end": (y.time, y.charge),
        })
    return complete, half


def _endpoint_json(node: tuple[Fraction, Fraction]) -> dict:
    return {"time": frac_to_json(node[0]), "charge": frac_to_json(node[1])}


def _count_cycles(complete, half, contract: WarrantyContract) -> dict:
    by_depth: dict[Fraction, dict] = {}
    total = ZERO
    full_records = []
    half_records = []
    for cyc in complete:
        usage = contract.full_cycle_cost(cyc["depth"])
        total += usage
        full_records.append({
            "depth": frac_to_json(cyc["depth"]),
            "usage": frac_to_json(usage),
            "start": _endpoint_json(cyc["start"]),
            "end": _endpoint_json(cyc["end"]),
        })
        slot = by_depth.setdefault(cyc["depth"], {"full": 0, "half": 0, "usage": ZERO})
        slot["full"] += 1
        slot["usage"] += usage
    for cyc in half:
        usage = contract.half_cycle_cost(cyc["depth"])
        total += usage
        half_records.append({
            "depth": frac_to_json(cyc["depth"]),
            "usage": frac_to_json(usage),
            "start": _endpoint_json(cyc["start"]),
            "end": _endpoint_json(cyc["end"]),
        })
        slot = by_depth.setdefault(cyc["depth"], {"full": 0, "half": 0, "usage": ZERO})
        slot["half"] += 1
        slot["usage"] += usage

    by_depth_json = [
        {
            "depth": frac_to_json(depth),
            "full_cycles": by_depth[depth]["full"],
            "half_cycles": by_depth[depth]["half"],
            "usage": frac_to_json(by_depth[depth]["usage"]),
        }
        for depth in sorted(by_depth)
    ]
    return {
        "complete_cycles": full_records,
        "half_cycles": half_records,
        "by_depth": by_depth_json,
        "full_count": len(complete),
        "half_count": len(half),
        "usage": total,
    }


# --------------------------------------------------------------------------- #
# Product
# --------------------------------------------------------------------------- #
def evaluate(contract: WarrantyContract, soc_product: Mapping) -> dict:
    """Compute the warranty report for a (stored) SOC product.

    The report references the SOC report id so a reader can trace every endpoint
    back to the charge history, and is itself content-addressed on save.
    """
    cap = parse_frac(soc_product["params"]["capacity"], "capacity")
    contract.validate_covers(cap)
    soc_report_id = soc_product.get("report_id")

    run_reports = []
    grand_total = ZERO
    grand_full = 0
    grand_half = 0
    agg: dict[Fraction, dict] = {}
    for idx, run in enumerate(soc_product.get("runs", [])):
        tps = [
            (parse_frac(p["time"], "time"), parse_frac(p["charge"], "charge"))
            for p in run.get("turning_points", [])
        ]
        complete, half = decompose(tps)
        counted = _count_cycles(complete, half, contract)
        grand_total += counted["usage"]
        grand_full += counted["full_count"]
        grand_half += counted["half_count"]
        for rec in counted["by_depth"]:
            depth = parse_frac(rec["depth"], "depth")
            slot = agg.setdefault(depth, {"full": 0, "half": 0, "usage": ZERO})
            slot["full"] += rec["full_cycles"]
            slot["half"] += rec["half_cycles"]
            slot["usage"] += parse_frac(rec["usage"], "usage")
        run_reports.append({
            "run_index": idx,
            "start": run.get("start"),
            "end": run.get("end"),
            "turning_points": run.get("turning_points", []),
            **{k: v for k, v in counted.items() if k != "usage"},
            "usage": frac_to_json(counted["usage"]),
        })

    totals_by_depth = [
        {
            "depth": frac_to_json(depth),
            "full_cycles": agg[depth]["full"],
            "half_cycles": agg[depth]["half"],
            "usage": frac_to_json(agg[depth]["usage"]),
        }
        for depth in sorted(agg)
    ]
    return {
        "schema": SCHEMA,
        "report_id": None,
        "soc_report_id": soc_report_id,
        "capacity": frac_to_json(cap),
        "contract": contract.to_json(),
        "runs": run_reports,
        "totals": {
            "complete_cycles": grand_full,
            "half_cycles": grand_half,
            "usage": frac_to_json(grand_total),
        },
        "by_depth": totals_by_depth,
        "runs_processed": len(run_reports),
    }
