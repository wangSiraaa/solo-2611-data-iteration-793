"""Command line for the power/demand library and the SOC/warranty products.

Two families of commands share one entry point:

* the original demand-billing material replay::

      python3 -m power_billing.cli materials/735 [--db /tmp/billing.db]

* exact state-of-charge and warranty reports (all arithmetic is rational)::

      python3 -m power_billing.cli soc --scenario FILE [--db report.db]
      python3 -m power_billing.cli warranty --scenario FILE [--contract FILE|--contracts A B]
      python3 -m power_billing.cli demo                # the worked examples below

A *scenario* document bundles the two traces, the SOC parameters, optional
anchors and (for warranty) contract tiers::

    {
      "charge":    {"trace_id": "C", "revision": "r1", "segments": [...]},
      "discharge": {"trace_id": "D", "revision": "r1", "segments": [...]},
      "params":    {"initial_charge": 0, "capacity": 6,
                    "charge_efficiency": 1, "discharge_efficiency": 1},
      "anchors":   [{"time": 4, "charge": 1}],           # optional
      "contracts": [ {...warranty contract...}, {...} ],  # optional
      "revise":    {"trace": "charge", "revision": "r2",
                    "segments": [...]}                   # optional rebuild demo
    }
"""

from __future__ import annotations

import argparse
import json
import sys
from fractions import Fraction
from pathlib import Path

from .demand import Contract, evaluate
from .ledger import Ledger, bill_document
from .power import TrajectoryStore
from .reports import (
    build_soc_report,
    build_warranty_report,
    rebuild_soc_report,
    register_trace_json,
    switch_contract,
)
from .store import ReportStore, segments_from_json
from .warranty import WarrantyContract


# --------------------------------------------------------------------------- #
# Demand-billing material replay (unchanged behaviour)
# --------------------------------------------------------------------------- #
def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def _print_receipt(label: str, receipt: dict) -> None:
    doc = bill_document(receipt)
    print(
        f"{label}: coverage={doc['coverage']} "
        f"window={doc['window']} demand={doc['demand']} charge={doc['charge']} "
        f"predecessor={doc['predecessor_id']} difference={doc['difference']} "
        f"balance_after={doc['balance_after']}"
    )


def run_materials(mdir: Path, db: Path | None) -> int:
    power = _load(mdir / "power.json")
    contract_obj = _load(mdir / "contract.json")
    correction = _load(mdir / "correction.json")
    ops = [
        json.loads(line)
        for line in (mdir / "operations.jsonl").read_text().splitlines()
        if line.strip()
    ]

    store = TrajectoryStore()
    store.register_json(power)
    contract = Contract.from_json(contract_obj)
    trace_id = power["trace_id"]
    revision = power["revision"]

    db_path: str = str(db) if db is not None else ":memory:"
    ledger = Ledger(db_path)
    ledger.register_contract(contract)
    ledger.set_current_power(trace_id, revision)

    last_settle: str | None = None
    for op in ops:
        kind = op["op"]
        if kind == "settle":
            sid = op["settlement_id"]
            revision = store.current(trace_id).revision
            target = evaluate(contract, store.trajectory(trace_id), trace_id, revision)
            if not target.bookable:
                info = ledger.record_analysis(sid, target)
                print(
                    f"ANALYSIS {sid}: coverage={info['coverage']} "
                    f"window={info['window']} demand={info['demand']} "
                    "(not booked)"
                )
                continue
            expected = None
            if op.get("expected_previous_bill") is not None:
                expected = op["expected_previous_bill"]
            elif last_settle is not None:
                expected = last_settle
            receipt = ledger.post_target(sid, target, expected_previous_bill=expected)
            _print_receipt(f"BILL {sid}", receipt)
            last_settle = sid
        elif kind == "correct_power":
            file = op.get("file")
            obj = _load(mdir / file) if file else op.get("correction")
            rev = store.revise_json(trace_id, obj)
            ledger.set_current_power(trace_id, rev.revision)
            print(f"POWER corrected to revision {rev.revision} (no amount booked)")
        elif kind == "read_settlement":
            sid = op["settlement_id"]
            receipt = ledger.get_bill(sid)
            if receipt is None:
                print(f"READ {sid}: not found")
            else:
                _print_receipt(f"READ {sid}", receipt)
        else:
            print(f"unknown op {kind!r}", file=sys.stderr)
            return 2

    ledger.close()
    return 0


# --------------------------------------------------------------------------- #
# Report helpers
# --------------------------------------------------------------------------- #
def _f(node) -> Fraction:
    if isinstance(node, dict):
        return Fraction(int(node["n"]), int(node["d"]))
    return Fraction(node)


def _show(node) -> str:
    if node is None:
        return "-"
    if isinstance(node, dict) and "n" in node and "d" in node:
        return str(_f(node))
    return str(node)


def _load_report_store(db: Path | None) -> ReportStore:
    return ReportStore(str(db) if db is not None else ":memory:")


def _register_scenario(store: ReportStore, scen: dict) -> None:
    register_trace_json(store, scen["charge"])
    register_trace_json(store, scen["discharge"])


def _apply_revision(store: ReportStore, scen: dict) -> dict | None:
    """If the scenario carries a ``revise`` block, register the new revision.

    Returns a rebuilt SOC product (from the *new* current traces) or ``None``.
    The earlier report stays retrievable by its own id.
    """
    rev = scen.get("revise")
    if not rev:
        return None
    which = rev.get("trace", "charge")
    trace = scen[which]
    store.register_trace(trace["trace_id"], str(rev["revision"]),
                         segments_from_json(rev))
    return rebuild_soc_report(
        store, scen["charge"]["trace_id"], scen["discharge"]["trace_id"],
        scen["params"], scen.get("anchors", []),
    )


def run_soc(scen: dict, db: Path | None) -> dict:
    store = _load_report_store(db)
    _register_scenario(store, scen)
    soc = build_soc_report(
        store, scen["charge"]["trace_id"], scen["discharge"]["trace_id"],
        scen["params"], scen.get("anchors", []),
    )
    _print_soc(soc)
    rebuilt = _apply_revision(store, scen)
    if rebuilt is not None:
        print(f"\nREBUILT after revision -> SOC report {rebuilt['report_id']}")
        _print_soc(rebuilt)
        print(f"  previous report still retrievable: "
              f"{store.get_soc_report(soc['report_id']) is not None}")
    store.close()
    return soc


def _print_soc(soc: dict) -> None:
    t = soc["totals"]
    print(f"SOC report {soc['report_id']}")
    print(
        "  charged={} discharged={} overflow={} unserved={} losses={}".format(
            _show(t["charged_into_store"]), _show(t["discharged_from_store"]),
            _show(t["overflow"]), _show(t["unserved"]), _show(t["losses"]),
        )
    )
    print(
        "  charge: {} -> {} (min {}, max {})".format(
            _show(t["initial_charge"]), _show(t["final_charge"]),
            _show(t["min_charge"]), _show(t["max_charge"]),
        )
    )
    for r in soc["runs"]:
        tp = " ".join(
            f"[{_show(p['time'])}→{_show(p['charge'])}]" for p in r["turning_points"]
        )
        print(f"  run [{_show(r['start'])},{_show(r['end'])}) turning: {tp}")
    for g in soc["gaps"]:
        print(f"  gap [{_show(g['start'])},{_show(g['end'])}) -> truncated")
    for b in soc["boundary_events"]:
        print(
            "  boundary {} at t={} kind={} amount={} state={}".format(
                b["boundary"], _show(b["time"]), b["kind"], _show(b["amount"]),
                b["state"],
            )
        )


def run_warranty(scen: dict, contracts: list[dict], db: Path | None) -> None:
    store = _load_report_store(db)
    _register_scenario(store, scen)
    soc = build_soc_report(
        store, scen["charge"]["trace_id"], scen["discharge"]["trace_id"],
        scen["params"], scen.get("anchors", []),
    )
    print(f"SOC report {soc['report_id']}")
    for obj in contracts:
        rep = build_warranty_report(store, soc["report_id"], obj)
        _print_warranty(rep)
    rebuilt = _apply_revision(store, scen)
    if rebuilt is not None:
        print(f"\nREBUILT after revision -> SOC report {rebuilt['report_id']}")
        for obj in contracts:
            _print_warranty(build_warranty_report(store, rebuilt["report_id"], obj))
        print(f"  previous report still retrievable: "
              f"{store.get_soc_report(soc['report_id']) is not None}")
    store.close()


def _print_warranty(rep: dict) -> None:
    c = rep["contract"]
    print(f"WARRANTY contract {c['contract_id']}@{c['revision']}")
    for run in rep["runs"]:
        print(f"  run {run['run_index']} [{_show(run['start'])},{_show(run['end'])})")
        for cyc in run["complete_cycles"]:
            print(
                "    full depth={} usage={} from {}→{}".format(
                    _show(cyc["depth"]), _show(cyc["usage"]),
                    _show(cyc["start"]["time"]), _show(cyc["end"]["time"]),
                )
            )
        for cyc in run["half_cycles"]:
            print(
                "    half depth={} usage={} from {}→{}".format(
                    _show(cyc["depth"]), _show(cyc["usage"]),
                    _show(cyc["start"]["time"]), _show(cyc["end"]["time"]),
                )
            )
    t = rep["totals"]
    print(
        "  totals: full={} half={} usage={}".format(
            t["complete_cycles"], t["half_cycles"], _show(t["usage"])
        )
    )


# --------------------------------------------------------------------------- #
# Demo scenarios (self-contained, re-runnable)
# --------------------------------------------------------------------------- #
def _zero(cid, start, end):
    return {"segment_id": cid, "start": start, "end": end, "power": 0}


def demo_scenarios() -> list[tuple[str, dict, list[dict]]]:
    tiers = [
        {"max_depth": 2, "full_cycle_cost": 1},
        {"max_depth": 6, "full_cycle_cost": 3},
    ]
    tiers_free_shallow = [
        {"max_depth": 2, "full_cycle_cost": 0},
        {"max_depth": 6, "full_cycle_cost": 3},
    ]
    params6 = {"initial_charge": 0, "capacity": 6,
               "charge_efficiency": 1, "discharge_efficiency": 1}
    c_w1 = {"contract_id": "W", "revision": "w1", "tiers": tiers}
    c_w2 = {"contract_id": "W", "revision": "w2", "tiers": tiers_free_shallow}

    worked = {
        "charge": {"trace_id": "C", "revision": "r1", "segments": [
            {"segment_id": "c1", "start": 0, "end": 1, "power": 6},
            _zero("c2", 1, 2),
            {"segment_id": "c3", "start": 2, "end": 3, "power": 2},
            _zero("c4", 3, 4),
        ]},
        "discharge": {"trace_id": "D", "revision": "r1", "segments": [
            _zero("d1", 0, 1),
            {"segment_id": "d2", "start": 1, "end": 2, "power": 4},
            _zero("d3", 2, 3),
            {"segment_id": "d4", "start": 3, "end": 4, "power": 4},
        ]},
        "params": params6,
    }
    worked_c3zero = json.loads(json.dumps(worked))
    worked_c3zero["charge"]["segments"][2]["power"] = 0
    worked_c3zero["charge"]["revision"] = "r2"

    saturation = {
        "charge": {"trace_id": "C", "revision": "r1", "segments": [
            {"segment_id": "c1", "start": 0, "end": 1, "power": 3},
            {"segment_id": "c2", "start": 1, "end": 3, "power": 9},
        ]},
        "discharge": {"trace_id": "D", "revision": "r1", "segments": [
            _zero("d1", 0, 3),
        ]},
        "params": {"initial_charge": 0, "capacity": 5,
                   "charge_efficiency": 1, "discharge_efficiency": 1},
        "contracts": [{"contract_id": "W", "revision": "w1",
                       "tiers": [{"max_depth": 5, "full_cycle_cost": 1}]}],
    }

    gap = {
        "charge": {"trace_id": "C", "revision": "r1", "segments": [
            {"segment_id": "a", "start": 0, "end": 2, "power": 4},
            {"segment_id": "b", "start": 4, "end": 6, "power": 3},
        ]},
        "discharge": {"trace_id": "D", "revision": "r1", "segments": [
            _zero("z1", 0, 2),
            _zero("z2", 4, 6),
        ]},
        "params": {"initial_charge": 0, "capacity": 10,
                   "charge_efficiency": 1, "discharge_efficiency": 1},
        "anchors": [{"time": 4, "charge": 1}],
        "contracts": [{"contract_id": "W", "revision": "w1",
                       "tiers": [{"max_depth": 10, "full_cycle_cost": 2}]}],
    }
    return [
        ("worked example + contract switch", worked, [c_w1, c_w2]),
        ("third segment zero (shortfall)", worked_c3zero, [c_w1]),
        ("in-segment saturation + gap restart", saturation, saturation["contracts"]),
        ("gap with anchor restart", gap, gap["contracts"]),
    ]


def run_demo(db: Path | None) -> int:
    print("=" * 68)
    for label, scen, contracts in demo_scenarios():
        print(f"\n## {label}")
        store = _load_report_store(db)
        _register_scenario(store, scen)
        soc = build_soc_report(
            store, scen["charge"]["trace_id"], scen["discharge"]["trace_id"],
            scen["params"], scen.get("anchors", []),
        )
        _print_soc(soc)
        for obj in contracts:
            rep = switch_contract(store, soc["report_id"], obj)
            _print_warranty(rep)
        store.close()
    print("\n" + "=" * 68)
    print("## rebuild after revising a power segment")
    store = _load_report_store(db)
    _, scen, _ = demo_scenarios()[0]
    _register_scenario(store, scen)
    soc1 = build_soc_report(store, scen["charge"]["trace_id"],
                            scen["discharge"]["trace_id"], scen["params"])
    w = {"contract_id": "W", "revision": "w1",
         "tiers": [{"max_depth": 2, "full_cycle_cost": 1},
                   {"max_depth": 6, "full_cycle_cost": 3}]}
    rep1 = build_warranty_report(store, soc1["report_id"], w)
    print(f"  before: soc={soc1['report_id'][:12]} usage={_show(rep1['totals']['usage'])}")
    store.register_trace(
        scen["charge"]["trace_id"], "r2",
        segments_from_json({"segments": [
            {"segment_id": "c1", "start": 0, "end": 1, "power": 6},
            _zero("c2", 1, 2), _zero("c3", 2, 3), _zero("c4", 3, 4),
        ]}),
    )
    soc2 = rebuild_soc_report(store, scen["charge"]["trace_id"],
                              scen["discharge"]["trace_id"], scen["params"])
    rep2 = build_warranty_report(store, soc2["report_id"], w)
    print(f"  after:  soc={soc2['report_id'][:12]} usage={_show(rep2['totals']['usage'])} "
          f"unserved={_show(soc2['totals']['unserved'])}")
    print(f"  old report still retrievable: {store.get_soc_report(soc1['report_id']) is not None}")
    store.close()
    return 0


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # Backward compatible: first arg a directory (or missing) -> material replay.
    if not argv or argv[0] not in {"soc", "warranty", "demo"}:
        ap = argparse.ArgumentParser(prog="power_billing")
        ap.add_argument("materials", type=Path)
        ap.add_argument("--db", type=Path, default=None)
        args = ap.parse_args(argv)
        return run_materials(args.materials, args.db)

    cmd, rest = argv[0], argv[1:]
    if cmd == "demo":
        ap = argparse.ArgumentParser(prog="power_billing demo")
        ap.add_argument("--db", type=Path, default=None)
        return run_demo(ap.parse_args(rest).db)

    ap = argparse.ArgumentParser(prog=f"power_billing {cmd}")
    ap.add_argument("--scenario", type=Path, required=True)
    ap.add_argument("--db", type=Path, default=None)
    ap.add_argument("--contract", type=Path, default=None,
                    help="warranty contract JSON (single)")
    ap.add_argument("--contracts", type=Path, nargs="+", default=None,
                    help="several contract files: recompute and switch")
    args = ap.parse_args(rest)
    scen = _load(args.scenario)
    if cmd == "soc":
        run_soc(scen, args.db)
        return 0

    contracts = list(scen.get("contracts", []))
    if args.contracts:
        contracts = [_load(p) for p in args.contracts]
    elif args.contract:
        contracts = [_load(args.contract)]
    elif not contracts:
        print("warranty needs a contract (--contract/--contracts or 'contracts' "
              "in the scenario)", file=sys.stderr)
        return 2
    run_warranty(scen, contracts, args.db)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
