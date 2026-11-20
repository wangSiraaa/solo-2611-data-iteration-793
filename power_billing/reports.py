"""High-level, exact report builders (the Python-facing interface).

These functions are thin orchestration over :mod:`store`, :mod:`soc` and
:mod:`warranty`; the CLI in :mod:`cli` calls the same functions.
"""

from __future__ import annotations

from typing import Iterable, Mapping

from .errors import ValidationError
from .soc import Anchor, SocParams, simulate
from .store import PowerContext, ReportStore
from .warranty import WarrantyContract, evaluate as evaluate_warranty


# --------------------------------------------------------------------------- #
# Loading helpers
# --------------------------------------------------------------------------- #
def register_trace_json(store: ReportStore, obj: Mapping) -> None:
    """Register a trace document ``{trace_id, revision, segments:[...]}``."""
    from .store import segments_from_json

    store.register_trace(str(obj["trace_id"]), str(obj["revision"]),
                         segments_from_json(obj))


def _resolve(store: ReportStore, charge_trace_id: str, discharge_trace_id: str,
             charge_revision: str | None, discharge_revision: str | None) -> PowerContext:
    return store.resolve_context(
        charge_trace_id, discharge_trace_id,
        charge_revision=charge_revision, discharge_revision=discharge_revision,
    )


# --------------------------------------------------------------------------- #
# SOC report
# --------------------------------------------------------------------------- #
def build_soc_report(
    store: ReportStore,
    charge_trace_id: str,
    discharge_trace_id: str,
    params: SocParams | Mapping,
    anchors: Iterable[Anchor | Mapping] = (),
    *,
    charge_revision: str | None = None,
    discharge_revision: str | None = None,
    report_id: str | None = None,
) -> dict:
    """Simulate, persist and return an immutable ``soc-report/v1`` product.

    ``report_id`` defaults to a content digest, so the same physics always maps
    to the same id and a later revision can never overwrite an earlier report.
    """
    if isinstance(params, Mapping):
        params = SocParams.from_json(params)
    anchor_list = [
        a if isinstance(a, Anchor) else Anchor.from_json(a) for a in anchors
    ]
    ctx = _resolve(store, charge_trace_id, discharge_trace_id,
                   charge_revision, discharge_revision)
    product = simulate(
        ctx.charge_segments, ctx.discharge_segments, params, anchor_list,
        charge_ref=(ctx.charge_trace_id, ctx.charge_revision),
        discharge_ref=(ctx.discharge_trace_id, ctx.discharge_revision),
    )
    if report_id is not None:
        product["report_id"] = report_id
    rid = store.save_soc_report(product, power_key=ctx.key)
    return store.get_soc_report(rid)


def rebuild_soc_report(
    store: ReportStore,
    charge_trace_id: str,
    discharge_trace_id: str,
    params: SocParams | Mapping,
    anchors: Iterable[Anchor | Mapping] = (),
    *,
    charge_revision: str | None = None,
    discharge_revision: str | None = None,
) -> dict:
    """Rebuild from the *current* (possibly revised) traces.

    Equivalent to :func:`build_soc_report` with default revisions; kept as an
    explicit name so callers can express intent.  Earlier reports remain
    retrievable by their original id.
    """
    return build_soc_report(
        store, charge_trace_id, discharge_trace_id, params, anchors,
        charge_revision=charge_revision, discharge_revision=discharge_revision,
    )


# --------------------------------------------------------------------------- #
# Warranty report
# --------------------------------------------------------------------------- #
def build_warranty_report(
    store: ReportStore,
    soc_report_id: str,
    contract: WarrantyContract | Mapping,
    *,
    report_id: str | None = None,
) -> dict:
    """Evaluate a warranty contract against a stored SOC history, persist, return."""
    if isinstance(contract, Mapping):
        contract = WarrantyContract.from_json(contract)
    soc_product = store.get_soc_report(soc_report_id)
    if soc_product is None:
        raise ValidationError(f"unknown soc report {soc_report_id!r}")
    product = evaluate_warranty(contract, soc_product)
    if report_id is not None:
        product["report_id"] = report_id
    rid = store.save_warranty_report(
        product,
        soc_report_id=soc_report_id,
        contract_key=f"{contract.contract_id}@{contract.revision}",
    )
    return store.get_warranty_report(rid)


def switch_contract(
    store: ReportStore,
    soc_report_id: str,
    contract: WarrantyContract | Mapping,
) -> dict:
    """Recompute the same charge history under a different contract.

    Same decomposition, new costs; the previous warranty report is untouched.
    """
    return build_warranty_report(store, soc_report_id, contract)
