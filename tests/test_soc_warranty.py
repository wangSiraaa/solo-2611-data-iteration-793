"""Tests for the exact SOC / warranty products (soc-report/v1, warranty-report/v1).

Run with::

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from fractions import Fraction as F

from power_billing import (
    Anchor,
    ReportStore,
    SocParams,
    Tier,
    WarrantyContract,
    build_soc_report,
    build_warranty_report,
    compress_points,
    decompose,
    rebuild_soc_report,
    register_trace_json,
    segments_from_json,
    simulate_soc,
    switch_contract,
)
from power_billing.errors import (
    ConflictError,
    ContractError,
    ValidationError,
)
from power_billing.interval import Segment

WORKED_TIERS = [
    {"max_depth": 2, "full_cycle_cost": 1},
    {"max_depth": 6, "full_cycle_cost": 3},
]
FREE_SHALLOW_TIERS = [
    {"max_depth": 2, "full_cycle_cost": 0},
    {"max_depth": 6, "full_cycle_cost": 3},
]


def seg(sid, start, end, power):
    return Segment(sid, F(start), F(end), F(power))


def worked_traces(c3_power=2):
    charge = [
        seg("c1", 0, 1, 6), seg("c2", 1, 2, 0),
        seg("c3", 2, 3, c3_power), seg("c4", 3, 4, 0),
    ]
    discharge = [
        seg("d1", 0, 1, 0), seg("d2", 1, 2, 4),
        seg("d3", 2, 3, 0), seg("d4", 3, 4, 4),
    ]
    return charge, discharge


def unit_params(cap=6, q0=0):
    return SocParams(F(q0), F(cap), F(1), F(1))


def contract(tiers=WORKED_TIERS, revision="w1", cid="W"):
    return WarrantyContract.from_json(
        {"contract_id": cid, "revision": revision, "tiers": tiers})


def store_with(charge, discharge):
    st = ReportStore()
    st.register_trace("C", "r1", charge)
    st.register_trace("D", "r1", discharge)
    return st


def tp_values(soc_product):
    return [F(p["charge"]["n"], p["charge"]["d"])
            for p in soc_product["runs"][0]["turning_points"]]


def tp_times(soc_product):
    return [F(p["time"]["n"], p["time"]["d"])
            for p in soc_product["runs"][0]["turning_points"]]


# --------------------------------------------------------------------------- #
# SOC physics
# --------------------------------------------------------------------------- #
class SocPhysicsTests(unittest.TestCase):
    def test_worked_example_turning_points(self):
        c, d = worked_traces()
        prod = simulate_soc(c, d, unit_params(6))
        # +6,-4,+2,-4 from 0, cap 6 -> 0,6,2,4,0
        self.assertEqual(tp_values(prod), [F(0), F(6), F(2), F(4), F(0)])
        self.assertEqual(tp_times(prod), [F(0), F(1), F(2), F(3), F(4)])
        self.assertEqual(prod["totals"]["charged_into_store"], {"n": 8, "d": 1})
        self.assertEqual(prod["totals"]["discharged_from_store"], {"n": 8, "d": 1})
        self.assertEqual(prod["totals"]["overflow"], {"n": 0, "d": 1})
        self.assertEqual(prod["totals"]["unserved"], {"n": 0, "d": 1})

    def test_efficiencies_and_losses(self):
        # Both routes must cover a bracket; capacity is large enough that the
        # store never saturates or depletes.  eta_c=eta_d=1/2:
        #   [0,1) charge 16 -> store +8 ; [1,2) discharge 4 draws 8 -> store 0.
        c = [seg("c1", 0, 1, 16), seg("c2", 1, 2, 0)]
        d = [seg("d1", 0, 1, 0), seg("d2", 1, 2, 4)]
        p = SocParams(F(0), F(20), F(1, 2), F(1, 2))
        prod = simulate_soc(c, d, p)
        self.assertEqual(prod["totals"]["charged_into_store"], {"n": 8, "d": 1})
        self.assertEqual(prod["totals"]["discharged_from_store"], {"n": 8, "d": 1})
        # charge conversion loss = (1-1/2)*16 = 8; discharge loss = 4/(1/2)-4 = 4
        self.assertEqual(prod["totals"]["charge_conversion_loss"], {"n": 8, "d": 1})
        self.assertEqual(prod["totals"]["discharge_conversion_loss"], {"n": 4, "d": 1})
        self.assertEqual(prod["totals"]["losses"], {"n": 12, "d": 1})

    def test_in_segment_saturation_exact_time_and_overflow(self):
        # cap 5, q0 0.  charge 3 in [0,1) -> 3.  then 9 in [1,3): slope 9,
        # room 2 -> hits at 1 + 2/9 = 11/9; overflow = 9*(3 - 11/9) = 16.
        c = [seg("c1", 0, 1, 3), seg("c2", 1, 3, 9)]
        d = [seg("d1", 0, 3, 0)]
        prod = simulate_soc(c, d, unit_params(5))
        self.assertEqual(prod["totals"]["overflow"], {"n": 16, "d": 1})
        sat = [b for b in prod["boundary_events"] if b["boundary"] == "capacity"]
        self.assertEqual(len(sat), 1)
        self.assertEqual(sat[0]["time"], {"n": 11, "d": 9})
        self.assertEqual(sat[0]["state"], "enter_saturation")
        # The curve continues from the true endpoint (capacity), not the line.
        self.assertEqual(prod["soc_curve"][-1]["soc_end"], {"n": 5, "d": 1})

    def test_already_saturated_bracket(self):
        # Starting already full, a pure-charge bracket is all overflow.
        c = [seg("c1", 0, 2, 4)]
        d = [seg("d1", 0, 2, 0)]
        prod = simulate_soc(c, d, unit_params(5, q0=5))
        self.assertEqual(prod["totals"]["overflow"], {"n": 8, "d": 1})
        sat = [b for b in prod["boundary_events"] if b["boundary"] == "capacity"]
        self.assertEqual(sat[0]["state"], "already_saturated")
        self.assertEqual(sat[0]["time"], {"n": 0, "d": 1})

    def test_in_segment_depletion_and_unserved(self):
        # q0=3, discharge 9 over [0,2): hits 0 at 3/9=1/3; unserved=9*(2-1/3)=15
        c = [seg("c1", 0, 2, 0)]
        d = [seg("d1", 0, 2, 9)]
        prod = simulate_soc(c, d, unit_params(6, q0=3))
        self.assertEqual(prod["totals"]["unserved"], {"n": 15, "d": 1})
        dep = [b for b in prod["boundary_events"] if b["boundary"] == "zero"]
        self.assertEqual(dep[0]["time"], {"n": 1, "d": 3})
        self.assertEqual(dep[0]["state"], "enter_depletion")

    def test_reverse_flow_continues_from_true_endpoint(self):
        # q0=3, discharge 6 over [0,1): rate 6 -> empty at t=1/2, then unserved
        # for the rest.  [1,2) idle, [2,3) charges from the true 0 endpoint.
        c = [seg("c1", 0, 2, 0), seg("c2", 2, 3, 4)]
        d = [seg("d1", 0, 1, 6), seg("d2", 1, 2, 0), seg("d3", 2, 3, 0)]
        prod = simulate_soc(c, d, unit_params(6, q0=3))
        self.assertEqual(tp_values(prod), [F(3), F(0), F(4)])
        self.assertEqual(prod["boundary_events"][0]["time"], {"n": 1, "d": 2})
        self.assertEqual(prod["totals"]["unserved"], {"n": 3, "d": 1})

    def test_net_slope_reduces_charge_without_flow_mislabel(self):
        # Simultaneous charge 2 and discharge 1, eta 1: net slope +1, idle-
        # labelled only when slope exactly 0.  Here it charges.
        c = [seg("c1", 0, 2, 2)]
        d = [seg("d1", 0, 2, 1)]
        prod = simulate_soc(c, d, unit_params(10))
        self.assertEqual(tp_values(prod), [F(0), F(2)])

    def test_zero_initial_validated(self):
        with self.assertRaises(ValidationError):
            unit_params(6, q0=7).validate()
        with self.assertRaises(ValidationError):
            SocParams(F(0), F(0), F(1), F(1)).validate()
        with self.assertRaises(ValidationError):
            SocParams(F(0), F(6), F(0), F(1)).validate()
        with self.assertRaises(ValidationError):
            SocParams(F(0), F(6), F(1), F(2)).validate()


# --------------------------------------------------------------------------- #
# Gaps / truncation / anchors
# --------------------------------------------------------------------------- #
class GapAndAnchorTests(unittest.TestCase):
    def test_missing_route_truncates_history(self):
        # Discharge known only on [0,2); charge known over [0,4).  The bracket
        # [2,4) is unknown -> history truncated to [0,2).
        c = [seg("c1", 0, 4, 1)]
        d = [seg("d1", 0, 2, 0)]
        prod = simulate_soc(c, d, unit_params(10))
        self.assertTrue(prod["truncated"])
        self.assertEqual(prod["gaps"], [{"start": {"n": 2, "d": 1},
                                         "end": {"n": 4, "d": 1}}])
        self.assertEqual(prod["proven_runs"], 1)

    def test_anchor_restarts_a_new_run(self):
        c = [seg("a", 0, 2, 4), seg("b", 4, 6, 3)]
        d = [seg("z1", 0, 2, 0), seg("z2", 4, 6, 0)]
        prod = simulate_soc(c, d, unit_params(10), [Anchor(F(4), F(1))])
        self.assertEqual(prod["proven_runs"], 2)
        self.assertEqual(prod["gaps"], [{"start": {"n": 2, "d": 1},
                                         "end": {"n": 4, "d": 1}}])
        runs = prod["runs"]
        self.assertEqual(F(runs[0]["start"]["n"]), F(0))
        self.assertEqual(F(runs[1]["start"]["n"]), F(4))
        # Second run starts from the anchor value 1, not from the first run.
        self.assertEqual(tp_values(prod), tp_values(prod))
        self.assertEqual(F(runs[1]["points"][0]["charge"]["n"]), F(1))

    def test_no_anchor_no_restart(self):
        c = [seg("a", 0, 2, 4), seg("b", 4, 6, 3)]
        d = [seg("z1", 0, 2, 0), seg("z2", 4, 6, 0)]
        prod = simulate_soc(c, d, unit_params(10))  # no anchor
        self.assertEqual(prod["proven_runs"], 1)
        self.assertTrue(prod["truncated"])
        # Only the first run is provable; its tail is the truncation.
        self.assertEqual(F(prod["runs"][0]["end"]["n"]), F(2))

    def test_gap_sides_not_joined_into_one_cycle(self):
        # Two charge pulses separated by a gap, with an anchor restarting the
        # second run at 0.  If the sides were joined one would see a full
        # 0->5->0->5 swing; here they must stay two independent half cycles.
        c = [seg("a", 0, 1, 5), seg("b", 3, 4, 5)]
        d = [seg("z1", 0, 1, 0), seg("z2", 3, 4, 0)]
        prod = simulate_soc(c, d, unit_params(10), [Anchor(F(3), F(0))])
        self.assertEqual(prod["proven_runs"], 2)
        st = ReportStore()
        rid = st.save_soc_report({**prod, "report_id": None}, power_key="k")
        rep = build_warranty_report(st, rid, contract(
            [{"max_depth": 10, "full_cycle_cost": 2}], cid="G"))
        self.assertEqual(rep["totals"]["half_cycles"], 2)
        self.assertEqual(rep["totals"]["complete_cycles"], 0)
        self.assertEqual(rep["runs_processed"], 2)

    def test_anchor_must_be_in_range(self):
        c = [seg("a", 0, 2, 4)]
        d = [seg("z1", 0, 2, 0)]
        with self.assertRaises(ValidationError):
            simulate_soc(c, d, unit_params(10), [Anchor(F(0), F(50))])


# --------------------------------------------------------------------------- #
# Warranty decomposition
# --------------------------------------------------------------------------- #
class WarrantyDecompositionTests(unittest.TestCase):
    def test_worked_example_one_full_two_half(self):
        pts = [(F(0), F(0)), (F(1), F(6)), (F(2), F(2)), (F(3), F(4)), (F(4), F(0))]
        complete, half = decompose(pts)
        self.assertEqual([c["depth"] for c in complete], [F(2)])
        self.assertEqual([c["depth"] for c in half], [F(6), F(6)])

    def test_worked_usage_four_and_free_shallow_three(self):
        c, d = worked_traces()
        st = store_with(c, d)
        soc = build_soc_report(st, "C", "D", unit_params(6))
        w1 = build_warranty_report(st, soc["report_id"], contract(WORKED_TIERS))
        self.assertEqual(w1["totals"]["usage"], {"n": 4, "d": 1})
        self.assertEqual(w1["totals"]["complete_cycles"], 1)
        self.assertEqual(w1["totals"]["half_cycles"], 2)

        w2 = switch_contract(st, soc["report_id"], contract(
            FREE_SHALLOW_TIERS, revision="w2"))
        self.assertEqual(w2["totals"]["usage"], {"n": 3, "d": 1})
        # Same charge history, same decomposition, only costs change.
        self.assertEqual(w2["soc_report_id"], w1["soc_report_id"])

    def test_third_segment_zero_usage_three_and_unserved_two(self):
        c, d = worked_traces(c3_power=0)
        st = store_with(c, d)
        soc = build_soc_report(st, "C", "D", unit_params(6))
        self.assertEqual(soc["totals"]["unserved"], {"n": 2, "d": 1})
        rep = build_warranty_report(st, soc["report_id"], contract(WORKED_TIERS))
        self.assertEqual(rep["totals"]["usage"], {"n": 3, "d": 1})
        self.assertEqual(rep["totals"]["complete_cycles"], 0)
        self.assertEqual(rep["totals"]["half_cycles"], 2)

    def test_traceable_endpoints_are_real_times(self):
        c, d = worked_traces()
        st = store_with(c, d)
        soc = build_soc_report(st, "C", "D", unit_params(6))
        rep = build_warranty_report(st, soc["report_id"], contract(WORKED_TIERS))
        full = rep["runs"][0]["complete_cycles"][0]
        self.assertEqual(full["start"], {"time": {"n": 2, "d": 1},
                                         "charge": {"n": 2, "d": 1}})
        self.assertEqual(full["end"], {"time": {"n": 3, "d": 1},
                                       "charge": {"n": 4, "d": 1}})
        for half in rep["runs"][0]["half_cycles"]:
            self.assertIn(half["start"]["time"], [{"n": 0, "d": 1},
                                                  {"n": 1, "d": 1}])

    def test_decompose_leftmost_four_only(self):
        # Sequence where only the leftmost window B,C is a valley.
        pts = [(F(i), s) for i, s in enumerate([0, 5, 1, 6, 2, 7])]
        complete, half = decompose(pts)
        # 0,5,1,6 -> |5-1|=4 <= |0-5|=5, |1-6|=5 -> remove 5,1 => 0,6,2,7
        # then 0,6,2,7 -> |6-2|=4 <= 6 and |2-7|=5 -> remove 6,2 => 0,7 half.
        self.assertEqual([c["depth"] for c in complete], [F(4), F(4)])
        self.assertEqual([h["depth"] for h in half], [F(7)])

    def test_skip_when_not_a_valley(self):
        # Increasing gaps 0,1,3,6: |B-C|=2 > |A-B|=1, so the leftmost window is
        # skipped and nothing remains as a complete cycle -> halves only.
        pts = [(F(i), s) for i, s in enumerate([0, 1, 3, 6])]
        complete, half = decompose(pts)
        self.assertEqual(complete, [])
        self.assertEqual([h["depth"] for h in half], [F(1), F(2), F(3)])

    def test_compress_removes_plateaus_and_same_direction(self):
        # plateau 3 at t=1,2 then continue up to 6: the plateau level is a
        # same-direction intermediate and is removed entirely; final dip to 2
        # is a real turning point.
        pts = [(F(0), F(0)), (F(1), F(3)), (F(2), F(3)), (F(3), F(6)), (F(4), F(2))]
        comp = compress_points(pts)
        self.assertEqual([s for _, s in comp], [F(0), F(6), F(2)])
        self.assertEqual([t for t, _ in comp], [F(0), F(3), F(4)])

    def test_tier_validation(self):
        with self.assertRaises(ContractError):
            contract([{"max_depth": 2, "full_cycle_cost": 1},
                      {"max_depth": 2, "full_cycle_cost": 3}])  # not increasing
        with self.assertRaises(ContractError):
            contract([{"max_depth": 2, "full_cycle_cost": -1}])
        # Coverage is checked against capacity at evaluation time.
        c, d = worked_traces()
        st = store_with(c, d)
        soc = build_soc_report(st, "C", "D", unit_params(6))
        with self.assertRaises(ContractError):
            build_warranty_report(st, soc["report_id"],
                                  contract([{"max_depth": 3,
                                             "full_cycle_cost": 1}]))

    def test_half_cycle_cost_is_half_full(self):
        w = contract([{"max_depth": 6, "full_cycle_cost": 3}])
        self.assertEqual(w.full_cycle_cost(F(4)), F(2))
        self.assertEqual(w.half_cycle_cost(F(4)), F(1))
        self.assertEqual(w.half_cycle_cost(F(6)), F(3, 2))


# --------------------------------------------------------------------------- #
# Store / revisions / idempotency
# --------------------------------------------------------------------------- #
class StoreRevisionTests(unittest.TestCase):
    def test_content_addressed_report_id_is_stable(self):
        c, d = worked_traces()
        st = store_with(c, d)
        a = build_soc_report(st, "C", "D", unit_params(6))
        b = build_soc_report(st, "C", "D", unit_params(6))
        self.assertEqual(a["report_id"], b["report_id"])

    def test_revision_yields_new_report_old_readable(self):
        c, d = worked_traces()
        st = store_with(c, d)
        soc1 = build_soc_report(st, "C", "D", unit_params(6))
        rep1 = build_warranty_report(st, soc1["report_id"], contract(WORKED_TIERS))

        # Revise c3 from 2 to 0 -> a new revision, new report id.
        st.register_trace("C", "r2", [
            seg("c1", 0, 1, 6), seg("c2", 1, 2, 0),
            seg("c3", 2, 3, 0), seg("c4", 3, 4, 0),
        ])
        soc2 = rebuild_soc_report(st, "C", "D", unit_params(6))
        self.assertNotEqual(soc1["report_id"], soc2["report_id"])
        rep2 = build_warranty_report(st, soc2["report_id"], contract(WORKED_TIERS))
        self.assertEqual(rep2["totals"]["usage"], {"n": 3, "d": 1})

        # Old reports are still retrievable and unchanged.
        old = st.get_soc_report(soc1["report_id"])
        self.assertEqual(old["totals"]["unserved"], {"n": 0, "d": 1})
        self.assertEqual(st.get_warranty_report(rep1["report_id"])["totals"]["usage"],
                         {"n": 4, "d": 1})

    def test_explicit_revisions_select_history(self):
        c, d = worked_traces()
        st = store_with(c, d)
        st.register_trace("C", "r2", [
            seg("c1", 0, 1, 6), seg("c2", 1, 2, 0),
            seg("c3", 2, 3, 0), seg("c4", 3, 4, 0),
        ])
        r1 = build_soc_report(st, "C", "D", unit_params(6), charge_revision="r1")
        r2 = build_soc_report(st, "C", "D", unit_params(6), charge_revision="r2")
        self.assertNotEqual(r1["report_id"], r2["report_id"])
        self.assertEqual(r1["traces"]["charge"]["revision"], "r1")

    def test_same_revision_different_content_conflicts(self):
        st = ReportStore()
        st.register_trace("C", "r1", [seg("a", 0, 1, 1)])
        with self.assertRaises(ConflictError):
            st.register_trace("C", "r1", [seg("a", 0, 1, 2)])

    def test_reopening_file_store_preserves_reports(self):
        c, d = worked_traces()

        def trace_obj(tid, segments):
            return {"trace_id": tid, "revision": "r1",
                    "segments": [{"segment_id": s.segment_id,
                                  "start": str(s.start), "end": str(s.end),
                                  "power": str(s.power)} for s in segments]}

        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "rep.db")
            st = ReportStore(path)
            register_trace_json(st, trace_obj("C", c))
            register_trace_json(st, trace_obj("D", d))
            soc = build_soc_report(st, "C", "D", unit_params(6))
            rep = build_warranty_report(st, soc["report_id"], contract(WORKED_TIERS))
            st.close()

            st2 = ReportStore(path)
            self.assertEqual(st2.get_soc_report(soc["report_id"])["report_id"],
                             soc["report_id"])
            self.assertEqual(
                st2.get_warranty_report(rep["report_id"])["totals"]["usage"],
                {"n": 4, "d": 1})
            st2.close()

    def test_contract_switch_does_not_mutate_first(self):
        c, d = worked_traces()
        st = store_with(c, d)
        soc = build_soc_report(st, "C", "D", unit_params(6))
        w1 = build_warranty_report(st, soc["report_id"], contract(WORKED_TIERS))
        switch_contract(st, soc["report_id"], contract(FREE_SHALLOW_TIERS,
                                                        revision="w2"))
        self.assertEqual(st.get_warranty_report(w1["report_id"])["totals"]["usage"],
                         {"n": 4, "d": 1})
        # Both contract reports are discoverable for the same SOC history.
        self.assertEqual(len(st.warranty_reports_for_soc(soc["report_id"])), 2)


# --------------------------------------------------------------------------- #
# JSON shape / CLI
# --------------------------------------------------------------------------- #
class JsonShapeTests(unittest.TestCase):
    def test_products_are_json_serializable_fraction_objects(self):
        c, d = worked_traces()
        st = store_with(c, d)
        soc = build_soc_report(st, "C", "D", unit_params(6))
        rep = build_warranty_report(st, soc["report_id"], contract(WORKED_TIERS))
        json.dumps(soc)
        json.dumps(rep)
        self.assertEqual(soc["schema"], "soc-report/v1")
        self.assertEqual(rep["schema"], "warranty-report/v1")
        self.assertEqual(soc["totals"]["overflow"], {"n": 0, "d": 1})
        self.assertEqual(rep["totals"]["usage"], {"n": 4, "d": 1})
        # Denominators are positive.
        for node in (soc["totals"]["min_charge"], rep["totals"]["usage"]):
            self.assertGreater(node["d"], 0)

    def test_scale_five_thousand_segments(self):
        # A long alternating series still simulates/compresses quickly.  Each
        # half-slot fills the *other* route with an explicit zero so both
        # traces are known everywhere (5000 source segments total).
        import time
        n = 625
        c, d = [], []
        for i in range(n):
            c.append(seg(f"c{i}", 4 * i, 4 * i + 1, 6))
            c.append(seg(f"cz1{i}", 4 * i + 1, 4 * i + 2, 0))
            c.append(seg(f"cz2{i}", 4 * i + 2, 4 * i + 3, 0))
            c.append(seg(f"cz3{i}", 4 * i + 3, 4 * i + 4, 0))
            d.append(seg(f"dz1{i}", 4 * i, 4 * i + 1, 0))
            d.append(seg(f"d{i}", 4 * i + 1, 4 * i + 2, 4))
            d.append(seg(f"dz2{i}", 4 * i + 2, 4 * i + 3, 0))
            d.append(seg(f"dz3{i}", 4 * i + 3, 4 * i + 4, 0))
        self.assertEqual(len(c) + len(d), 5000)
        start = time.perf_counter()
        prod = simulate_soc(c, d, unit_params(6))
        self.assertLess(time.perf_counter() - start, 10.0)
        self.assertEqual(prod["proven_runs"], 1)
        st = ReportStore()
        rid = st.save_soc_report({**prod, "report_id": None}, power_key="k")
        start = time.perf_counter()
        rep = build_warranty_report(st, rid, contract(
            [{"max_depth": 6, "full_cycle_cost": 3}], cid="S"))
        self.assertLess(time.perf_counter() - start, 10.0)
        self.assertGreater(rep["totals"]["complete_cycles"], 0)


class CliTests(unittest.TestCase):
    def test_demo_runs_and_prints_expected_totals(self):
        import io
        from contextlib import redirect_stdout
        from power_billing.cli import main

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(["demo"])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        # Worked example: w1 usage 4 then w2 usage 3.
        self.assertIn("totals: full=1 half=2 usage=4", out)
        self.assertIn("totals: full=1 half=2 usage=3", out)
        # Shortfall case.
        self.assertIn("unserved=2", out)
        # Saturation boundary at 11/9.
        self.assertIn("11/9", out)
        # Gap restart produced two runs.
        self.assertIn("gap [2,4) -> truncated", out)
        # Rebuild is visible.
        self.assertIn("old report still retrievable: True", out)

    def test_materials_rebuild_via_revise_block(self):
        import io
        from contextlib import redirect_stdout
        from pathlib import Path
        from power_billing.cli import main

        scen = Path(__file__).resolve().parent.parent / "materials" / "soc" / "worked.json"
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(["soc", "--scenario", str(scen)])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("REBUILT after revision", out)
        self.assertIn("previous report still retrievable: True", out)
        self.assertIn("unserved=2", out)

    def test_soc_subcommand_on_scenario_file(self):
        import io
        from contextlib import redirect_stdout
        from power_billing.cli import main

        c, d = worked_traces()
        scen = {
            "charge": {"trace_id": "C", "revision": "r1",
                       "segments": [{"segment_id": s.segment_id,
                                     "start": str(s.start), "end": str(s.end),
                                     "power": str(s.power)} for s in c]},
            "discharge": {"trace_id": "D", "revision": "r1",
                          "segments": [{"segment_id": s.segment_id,
                                        "start": str(s.start), "end": str(s.end),
                                        "power": str(s.power)} for s in d]},
            "params": {"initial_charge": 0, "capacity": 6,
                       "charge_efficiency": 1, "discharge_efficiency": 1},
        }
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "scen.json")
            with open(path, "w") as fh:
                json.dump(scen, fh)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = main(["soc", "--scenario", path])
            self.assertEqual(rc, 0)
            self.assertIn("SOC report", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
