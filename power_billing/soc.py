"""Exact state-of-charge reconstruction from two non-negative power traces.

Physics
-------
For each elementary time bracket where **both** traces are known, the net
inventory slope is

    dsoc/dt = eta_charge * p_charge - p_discharge / eta_discharge

with ``0 < eta_charge, eta_discharge <= 1`` and initial ``0 <= q0 <= capacity``.
Power is piecewise constant and every trace breakpoint is an event boundary, so
integrating the slope over a bracket is exact.  The resulting *actual* charge
curve is clamped to ``[0, capacity]``:

* charging past ``capacity`` -> the excess is **overflow** (inventory side,
  ``eta_charge * p_charge * extra_time``);
* discharging past ``0``     -> the deficit is **unserved** (inventory side,
  ``p_discharge / eta_discharge * extra_time``).

A later flow back inside the band continues from the *true* endpoint (0 or
capacity), never from the unclamped line.  Every time the curve meets a boundary
inside a bracket is reported with its exact time and the "already there" vs
"entering" distinction -- that is the "段内到边界的时刻".

Unknown data
------------
A bracket is *known* only if **both** traces cover it.  A maximal run of unknown
brackets truncates the provable history.  A new run starts again only at a
supplied independent charge measurement (:class:`Anchor`) whose time coincides
with the resumption of known power; a gap's two sides are never stitched into
one swing/cycle.

All arithmetic is :class:`fractions.Fraction`; nothing is rounded.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from fractions import Fraction
from typing import Iterable, Mapping

from .errors import ValidationError
from .interval import Segment, ZERO, frac_to_json
from .numbers import parse_frac

SCHEMA = "soc-report/v1"

FLOW_CHARGING = "charging"
FLOW_DISCHARGING = "discharging"
FLOW_IDLE = "idle"
FLOW_OVERFLOW = "overflow"
FLOW_UNSERVED = "unserved"

BOUND_CAPACITY = "capacity"
BOUND_ZERO = "zero"

STATE_ENTER_SATURATION = "enter_saturation"
STATE_ALREADY_SATURATED = "already_saturated"
STATE_ENTER_DEPLETION = "enter_depletion"
STATE_ALREADY_EMPTY = "already_empty"


# --------------------------------------------------------------------------- #
# Parameters / anchors
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class SocParams:
    initial_charge: Fraction
    capacity: Fraction
    charge_efficiency: Fraction
    discharge_efficiency: Fraction

    @classmethod
    def from_json(cls, obj: Mapping) -> "SocParams":
        try:
            p = cls(
                initial_charge=parse_frac(obj["initial_charge"], "initial_charge"),
                capacity=parse_frac(obj["capacity"], "capacity"),
                charge_efficiency=parse_frac(obj["charge_efficiency"], "charge_efficiency"),
                discharge_efficiency=parse_frac(obj["discharge_efficiency"], "discharge_efficiency"),
            )
        except KeyError as exc:
            raise ValidationError(f"soc params missing field {exc}") from exc
        p.validate()
        return p

    def validate(self) -> None:
        if self.capacity <= 0:
            raise ValidationError("capacity must be positive")
        if not (ZERO < self.charge_efficiency <= 1):
            raise ValidationError("charge_efficiency must be in (0, 1]")
        if not (ZERO < self.discharge_efficiency <= 1):
            raise ValidationError("discharge_efficiency must be in (0, 1]")
        if not (ZERO <= self.initial_charge <= self.capacity):
            raise ValidationError("initial_charge must be within [0, capacity]")

    def to_json(self) -> dict:
        return {
            "initial_charge": frac_to_json(self.initial_charge),
            "capacity": frac_to_json(self.capacity),
            "charge_efficiency": frac_to_json(self.charge_efficiency),
            "discharge_efficiency": frac_to_json(self.discharge_efficiency),
        }


@dataclass(frozen=True, slots=True)
class Anchor:
    """An independent, timed charge measurement used to restart after a gap."""

    time: Fraction
    charge: Fraction

    @classmethod
    def from_json(cls, obj: Mapping) -> "Anchor":
        try:
            return cls(parse_frac(obj["time"], "anchor time"),
                       parse_frac(obj["charge"], "anchor charge"))
        except KeyError as exc:
            raise ValidationError(f"anchor missing field {exc}") from exc

    def to_json(self) -> dict:
        return {"time": frac_to_json(self.time), "charge": frac_to_json(self.charge)}


# --------------------------------------------------------------------------- #
# Internal records
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class _Piece:
    start: Fraction
    end: Fraction
    flow: str
    soc_start: Fraction
    soc_end: Fraction
    charge_power: Fraction
    discharge_power: Fraction
    rate: Fraction = ZERO


@dataclass(slots=True)
class _Bound:
    time: Fraction
    kind: str
    rate: Fraction
    amount: Fraction
    state: str


# --------------------------------------------------------------------------- #
# Simulation
# --------------------------------------------------------------------------- #
def simulate(
    charge_segments: Iterable[Segment],
    discharge_segments: Iterable[Segment],
    params: SocParams,
    anchors: Iterable[Anchor] = (),
    *,
    charge_ref: tuple[str, str] = ("", ""),
    discharge_ref: tuple[str, str] = ("", ""),
) -> dict:
    """Run the exact simulation and return a JSON-able product (no report_id)."""
    params.validate()
    anchors = tuple(anchors)
    for a in anchors:
        if not (ZERO <= a.charge <= params.capacity):
            raise ValidationError(
                f"anchor at t={a.time} has charge outside [0, capacity]")

    charge = sorted(charge_segments, key=lambda s: s.start)
    discharge = sorted(discharge_segments, key=lambda s: s.start)
    c_starts = [s.start for s in charge]
    d_starts = [s.start for s in discharge]

    anchor_map: dict[Fraction, Fraction] = {a.time: a.charge for a in anchors}

    events: set[Fraction] = set(anchor_map)
    for s in charge:
        events.add(s.start)
        events.add(s.end)
    for s in discharge:
        events.add(s.start)
        events.add(s.end)
    if not events:
        return _product(params, charge_ref, discharge_ref, anchors,
                        pieces=[], bounds=[], intervals=[], runs=[], gaps=[])
    ev = sorted(events)

    eta_c = params.charge_efficiency
    eta_d = params.discharge_efficiency
    cap = params.capacity

    pieces: list[_Piece] = []
    bounds: list[_Bound] = []
    runs: list[dict] = []
    gaps: list[dict] = []
    intervals: list[dict] = []

    soc = params.initial_charge
    live = True
    run_open = False
    run_start: Fraction | None = None
    run_points: list[tuple[Fraction, Fraction]] = []

    def close_run(end: Fraction) -> None:
        nonlocal run_open
        if run_open:
            runs.append({"start": run_start, "end": end,
                         "points": list(run_points)})
            run_points.clear()
            run_open = False

    def record_unknown(l: Fraction, r: Fraction, c_pow, d_pow) -> None:
        intervals.append({"start": l, "end": r, "known": False,
                          "charge_power": c_pow, "discharge_power": d_pow})
        if gaps and gaps[-1]["end"] == l:
            gaps[-1]["end"] = r
        else:
            gaps.append({"start": l, "end": r})

    for idx in range(len(ev) - 1):
        l, r = ev[idx], ev[idx + 1]
        c_seg = _at(charge, c_starts, l)
        d_seg = _at(discharge, d_starts, l)
        c_pow = c_seg.power if c_seg is not None else ZERO
        d_pow = d_seg.power if d_seg is not None else ZERO
        known = c_seg is not None and d_seg is not None

        if not live:
            if known and l in anchor_map:
                soc = anchor_map[l]
                live = True
            else:
                record_unknown(l, r, c_pow, d_pow)
                continue

        if not known:
            close_run(l)
            live = False
            record_unknown(l, r, c_pow, d_pow)
            continue

        if not run_open:
            run_open = True
            run_start = l
            run_points.append((l, soc))

        soc_at_start = soc
        dt = r - l
        slope = eta_c * c_pow - d_pow / eta_d
        interval = {"start": l, "end": r, "known": True,
                    "charge_power": c_pow, "discharge_power": d_pow,
                    "soc_start": soc_at_start, "boundaries": []}

        if slope == 0:
            pieces.append(_Piece(l, r, FLOW_IDLE, soc, soc, c_pow, d_pow))
            interval["status"] = FLOW_IDLE
            soc = soc  # unchanged
            run_points.append((r, soc))
        elif slope > 0:
            soc = _charge_bracket(l, r, dt, c_pow, d_pow, slope, soc, cap,
                                  pieces, bounds, interval, run_points)
        else:
            soc = _discharge_bracket(l, r, dt, c_pow, d_pow, -slope, soc,
                                     pieces, bounds, interval, run_points)

        interval["soc_end"] = soc
        intervals.append(interval)

    close_run(ev[-1])

    return _product(params, charge_ref, discharge_ref, anchors,
                    pieces=pieces, bounds=bounds, intervals=intervals,
                    runs=runs, gaps=gaps)


def _charge_bracket(l, r, dt, c_pow, d_pow, rate, soc, cap,
                    pieces, bounds, interval, run_points) -> Fraction:
    room = cap - soc
    if room <= 0:
        amount = rate * dt
        pieces.append(_Piece(l, r, FLOW_OVERFLOW, cap, cap, c_pow, d_pow, rate))
        bounds.append(_Bound(l, BOUND_CAPACITY, rate, amount, STATE_ALREADY_SATURATED))
        interval["status"] = FLOW_OVERFLOW
        interval["boundaries"].append(_bound_json(l, BOUND_CAPACITY, rate, amount,
                                                  STATE_ALREADY_SATURATED))
        run_points.append((r, cap))
        return cap
    t_hit = l + room / rate
    if t_hit < r:
        amount = rate * (r - t_hit)
        pieces.append(_Piece(l, t_hit, FLOW_CHARGING, soc, cap, c_pow, d_pow))
        pieces.append(_Piece(t_hit, r, FLOW_OVERFLOW, cap, cap, c_pow, d_pow, rate))
        bounds.append(_Bound(t_hit, BOUND_CAPACITY, rate, amount, STATE_ENTER_SATURATION))
        interval["status"] = FLOW_CHARGING
        interval["boundaries"].append(_bound_json(t_hit, BOUND_CAPACITY, rate, amount,
                                                  STATE_ENTER_SATURATION))
        run_points.append((t_hit, cap))
        run_points.append((r, cap))
        return cap
    new_soc = soc + rate * dt
    pieces.append(_Piece(l, r, FLOW_CHARGING, soc, new_soc, c_pow, d_pow))
    interval["status"] = FLOW_CHARGING
    run_points.append((r, new_soc))
    if t_hit == r:  # exactly reaches the boundary at the right edge (amount 0)
        bounds.append(_Bound(r, BOUND_CAPACITY, rate, ZERO, STATE_ENTER_SATURATION))
        interval["boundaries"].append(_bound_json(r, BOUND_CAPACITY, rate, ZERO,
                                                  STATE_ENTER_SATURATION))
    return new_soc


def _discharge_bracket(l, r, dt, c_pow, d_pow, rate, soc,
                       pieces, bounds, interval, run_points) -> Fraction:
    avail = soc
    if avail <= 0:
        amount = rate * dt
        pieces.append(_Piece(l, r, FLOW_UNSERVED, ZERO, ZERO, c_pow, d_pow, rate))
        bounds.append(_Bound(l, BOUND_ZERO, rate, amount, STATE_ALREADY_EMPTY))
        interval["status"] = FLOW_UNSERVED
        interval["boundaries"].append(_bound_json(l, BOUND_ZERO, rate, amount,
                                                  STATE_ALREADY_EMPTY))
        run_points.append((r, ZERO))
        return ZERO
    t_hit = l + avail / rate
    if t_hit < r:
        amount = rate * (r - t_hit)
        pieces.append(_Piece(l, t_hit, FLOW_DISCHARGING, soc, ZERO, c_pow, d_pow))
        pieces.append(_Piece(t_hit, r, FLOW_UNSERVED, ZERO, ZERO, c_pow, d_pow, rate))
        bounds.append(_Bound(t_hit, BOUND_ZERO, rate, amount, STATE_ENTER_DEPLETION))
        interval["status"] = FLOW_DISCHARGING
        interval["boundaries"].append(_bound_json(t_hit, BOUND_ZERO, rate, amount,
                                                  STATE_ENTER_DEPLETION))
        run_points.append((t_hit, ZERO))
        run_points.append((r, ZERO))
        return ZERO
    new_soc = soc - rate * dt
    pieces.append(_Piece(l, r, FLOW_DISCHARGING, soc, new_soc, c_pow, d_pow))
    interval["status"] = FLOW_DISCHARGING
    run_points.append((r, new_soc))
    if t_hit == r:
        bounds.append(_Bound(r, BOUND_ZERO, rate, ZERO, STATE_ENTER_DEPLETION))
        interval["boundaries"].append(_bound_json(r, BOUND_ZERO, rate, ZERO,
                                                  STATE_ENTER_DEPLETION))
    return new_soc


def _bound_json(time, kind, rate, amount, state) -> dict:
    return {
        "time": frac_to_json(time),
        "boundary": kind,
        "kind": "overflow" if kind == BOUND_CAPACITY else "unserved",
        "amount": frac_to_json(amount),
        "state": state,
        "rate": frac_to_json(rate),
    }


def _at(segs, starts, t):
    i = bisect_right(starts, t) - 1
    if i >= 0 and segs[i].start <= t < segs[i].end:
        return segs[i]
    return None


# --------------------------------------------------------------------------- #
# Turning points (equal plateaus / same-direction intermediates removed)
# --------------------------------------------------------------------------- #
def compress_points(points: Iterable[tuple[Fraction, Fraction]]) -> list[tuple[Fraction, Fraction]]:
    """Keep local extrema and both endpoints; drop plateaus and monotone middles.

    A repeated level keeps the time at which it was *first* reached, so a
    plateau contributes a single vertex.  Times are exact.
    """
    pts: list[tuple[Fraction, Fraction]] = []
    for t, s in points:
        if pts and pts[-1][1] == s:
            continue
        pts.append((t, s))
    changed = True
    while changed and len(pts) >= 3:
        changed = False
        i = 1
        while i < len(pts) - 1:
            a, b, c = pts[i - 1][1], pts[i][1], pts[i + 1][1]
            if (b - a) * (c - b) > 0 or b == a or b == c:
                del pts[i]
                while i < len(pts) and i > 0 and pts[i][1] == pts[i - 1][1]:
                    del pts[i]
                changed = True
                continue
            i += 1
    out: list[tuple[Fraction, Fraction]] = []
    for t, s in pts:
        if out and out[-1][1] == s:
            continue
        out.append((t, s))
    return out


# --------------------------------------------------------------------------- #
# Product assembly
# --------------------------------------------------------------------------- #
def _product(params, charge_ref, discharge_ref, anchors, *, pieces, bounds,
             intervals, runs, gaps) -> dict:
    cap = params.capacity
    eta_c = params.charge_efficiency
    eta_d = params.discharge_efficiency

    grid_charge = ZERO
    grid_discharge = ZERO
    charged = ZERO
    discharged = ZERO
    overflow = ZERO
    unserved = ZERO
    for p in pieces:
        dt = p.end - p.start
        grid_charge += p.charge_power * dt
        if p.flow == FLOW_CHARGING:
            charged += p.soc_end - p.soc_start
            grid_discharge += p.discharge_power * dt
        elif p.flow == FLOW_DISCHARGING:
            discharged += p.soc_start - p.soc_end
            grid_discharge += p.discharge_power * dt
        elif p.flow == FLOW_IDLE:
            grid_discharge += p.discharge_power * dt
        elif p.flow == FLOW_OVERFLOW:
            overflow += p.rate * dt
            grid_discharge += p.discharge_power * dt
        elif p.flow == FLOW_UNSERVED:
            unserved += p.rate * dt
            # commanded discharge that never left the store -> not delivered

    charge_loss = grid_charge * (1 - eta_c)
    discharge_loss = grid_discharge * (1 / eta_d - 1)

    final_soc = pieces[-1].soc_end if pieces else params.initial_charge
    levels = [params.initial_charge]
    for p in pieces:
        levels.append(p.soc_start)
        levels.append(p.soc_end)
    for run in runs:
        levels.extend(s for _, s in run["points"])
    max_soc = max(levels)
    min_soc = min(levels)

    curve = [
        {
            "start": frac_to_json(p.start),
            "end": frac_to_json(p.end),
            "flow": p.flow,
            "soc_start": frac_to_json(p.soc_start),
            "soc_end": frac_to_json(p.soc_end),
            "charge_power": frac_to_json(p.charge_power),
            "discharge_power": frac_to_json(p.discharge_power),
        }
        for p in pieces
    ]
    boundary_events = [
        {
            "time": frac_to_json(b.time),
            "boundary": b.kind,
            "kind": "overflow" if b.kind == BOUND_CAPACITY else "unserved",
            "amount": frac_to_json(b.amount),
            "state": b.state,
            "rate": frac_to_json(b.rate),
            "charge": frac_to_json(cap if b.kind == BOUND_CAPACITY else ZERO),
        }
        for b in sorted(bounds, key=lambda b: (b.time, b.kind))
    ]
    runs_json = [
        {
            "start": frac_to_json(run["start"]),
            "end": frac_to_json(run["end"]),
            "points": [{"time": frac_to_json(t), "charge": frac_to_json(s)}
                       for t, s in run["points"]],
            "turning_points": [{"time": frac_to_json(t), "charge": frac_to_json(s)}
                               for t, s in compress_points(run["points"])],
        }
        for run in runs
    ]
    gaps_json = [{"start": frac_to_json(g["start"]), "end": frac_to_json(g["end"])}
                 for g in gaps]
    intervals_json = [_interval_json(iv) for iv in intervals]

    return {
        "schema": SCHEMA,
        "report_id": None,
        "params": params.to_json(),
        "traces": {
            "charge": {"trace_id": charge_ref[0], "revision": charge_ref[1]},
            "discharge": {"trace_id": discharge_ref[0], "revision": discharge_ref[1]},
        },
        "anchors": [a.to_json() for a in sorted(anchors, key=lambda a: a.time)],
        "totals": {
            "initial_charge": frac_to_json(params.initial_charge),
            "final_charge": frac_to_json(final_soc),
            "max_charge": frac_to_json(max_soc),
            "min_charge": frac_to_json(min_soc),
            "charged_into_store": frac_to_json(charged),
            "discharged_from_store": frac_to_json(discharged),
            "overflow": frac_to_json(overflow),
            "unserved": frac_to_json(unserved),
            "grid_charge_energy": frac_to_json(grid_charge),
            "grid_discharge_energy": frac_to_json(grid_discharge),
            "charge_conversion_loss": frac_to_json(charge_loss),
            "discharge_conversion_loss": frac_to_json(discharge_loss),
            "losses": frac_to_json(charge_loss + discharge_loss),
        },
        "soc_curve": curve,
        "boundary_events": boundary_events,
        "intervals": intervals_json,
        "runs": runs_json,
        "gaps": gaps_json,
        "truncated": bool(gaps),
        "proven_runs": len(runs_json),
    }


def _interval_json(iv) -> dict:
    item = {
        "start": frac_to_json(iv["start"]),
        "end": frac_to_json(iv["end"]),
        "known": iv["known"],
        "charge_power": frac_to_json(iv["charge_power"]),
        "discharge_power": frac_to_json(iv["discharge_power"]),
    }
    if "status" in iv:
        item["status"] = iv["status"]
        item["soc_start"] = frac_to_json(iv["soc_start"])
        item["soc_end"] = frac_to_json(iv["soc_end"])
        item["boundaries"] = iv.get("boundaries", [])
    return item
