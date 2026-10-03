"""离线试验回填账本核心：只追加事件 + 累计重算 + 计划/实际差异。

设计要点：
1. 计划侧在创建批次时整包快照冻结（来自已保存 blend_solution/blend_item），
   之后任何回填都不修改历史计划；
2. 实际到料、冲销、更正都是 recon_event 只追加事件，sign ∈ {+1,-1}，
   累计 = Σ sign×数量（加法满足交换律），因此乱序到达得到同一累计结果；
3. 每条事件必须显式引用 plan_item_id 与 assay_version_id，
   化验版在登记时快照，绝不偷偷改用“当前最新化验单”；
4. 冲销不删原事件，而是追加一条数量相反的 reversal 事件，保留审计痕迹；
5. 状态（pending/reconciled/abnormal）是事件流的纯投影，每次追加后整体重算：
   数量闭合且率值/有害组分在计划窗口内 → reconciled；
   数量闭合但越界或缺测/零分母 → abnormal；未闭合 → pending。
"""
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import chemistry, models
from .chemistry import BlendError
from .config import ALKALI_EQ_FACTOR_K2O

REQUIRED_OXIDES = ["CaO", "SiO2", "Al2O3", "Fe2O3"]
STATUS_PENDING = "pending"
STATUS_RECONCILED = "reconciled"
STATUS_ABNORMAL = "abnormal"


# ---------------------------------------------------------------- 计划快照
def build_plan_snapshot(db: Session, run: models.BlendRun,
                        solution: models.BlendSolution) -> dict:
    """从已保存方案冻结计划侧数据（逐原料质量/水量/成本/化验版/换算依据）。"""
    items = []
    tot = {"mass_t_dry": 0.0, "mass_t_wet": 0.0, "water_t": 0.0, "cost": 0.0}
    for it in sorted(solution.items, key=lambda i: i.id):
        mat = db.get(models.Material, it.material_id)
        ass = db.get(models.AssayVersion, it.assay_version_id)
        comp_dry = chemistry.convert_composition(
            ass.composition, ass.basis, mat.moisture_pct
        )
        row = {
            "plan_item_id": it.id,
            "material_id": mat.id,
            "material_code": mat.code,
            "material_name": mat.name,
            "moisture_pct": mat.moisture_pct,
            "cost_per_t_wet": mat.cost_per_t_wet,
            "assay_version_id": ass.id,
            "assay_version": ass.version,
            "lab_report_no": ass.lab_report_no,
            "assay_basis": ass.basis,
            "measured_oxides": list(ass.measured_oxides),
            "mass_t_dry": float(it.mass_t_dry),
            "mass_t_wet": float(it.mass_t_wet),
            "water_t": float(it.water_t),
            "cost": float(it.cost),
            "share_pct_dry": float(it.share_pct_dry),
            "composition_dry": {k: round(v, 6) for k, v in comp_dry.items()},
            "conversion_trace": it.conversion_trace,
            "raw_assay_composition": dict(ass.composition),
        }
        items.append(row)
        for k in tot:
            tot[k] += row[k]

    payload = solution.indicators or {}
    plan_ind = payload.get("indicators") or {}
    comp = payload.get("composition_dry_pct") or {}
    hazards = _hazard_values(comp, list((run.constraint_set or {})
                                        .get("hazard_limits_pct", {}).keys()))
    return {
        "run_id": run.id,
        "run_code": run.run_code,
        "solution_id": solution.id,
        "mode": solution.mode,
        "batch_t_dry": float(run.batch_t_dry),
        "targets": run.target,
        "hazard_limits_pct": dict((run.constraint_set or {})
                                  .get("hazard_limits_pct", {})),
        "items": items,
        "totals": {k: round(v, 6) for k, v in tot.items()},
        "indicators": {k: plan_ind.get(k) for k in ("SM", "IM", "KH")},
        "composition_dry_pct": comp,
        "hazard_values": hazards,
    }


def _hazard_values(comp_dry_pct: dict, keys: list[str]) -> dict:
    out = {}
    for key in set(keys) | {"Cl", "alkali_eq"}:
        if key == "alkali_eq":
            if "Na2O" in comp_dry_pct and "K2O" in comp_dry_pct:
                out[key] = round(
                    float(comp_dry_pct["Na2O"])
                    + ALKALI_EQ_FACTOR_K2O * float(comp_dry_pct["K2O"]), 4)
        elif key in comp_dry_pct:
            out[key] = round(float(comp_dry_pct[key]), 4)
    return out


# ---------------------------------------------------------------- 事件校验
def _load_batch(db: Session, batch_id: int) -> models.ReconBatch:
    batch = db.get(models.ReconBatch, batch_id)
    if batch is None:
        raise BlendError("BATCH_NOT_FOUND", f"回填批次 id={batch_id} 不存在。",
                         {"batch_id": batch_id})
    return batch


def _find_plan_item(plan: dict, plan_item_id: int) -> dict:
    for pi in plan["items"]:
        if pi["plan_item_id"] == plan_item_id:
            return pi
    raise BlendError(
        "PLAN_ITEM_NOT_FOUND",
        f"计划原料项 id={plan_item_id} 不属于该批次的已保存方案，"
        "实际到料只能登记到计划内原料。",
        {"plan_item_id": plan_item_id},
    )


def _resolve_assay(db: Session, material_id: int, assay_version_id: int) -> models.AssayVersion:
    """化验版必须显式选择且确属该原料——禁止默认取最新版。"""
    ass = db.get(models.AssayVersion, assay_version_id)
    if ass is None or ass.material_id != material_id:
        raise BlendError(
            "ASSAY_NOT_FOUND",
            f"化验版 id={assay_version_id} 不属于原料 id={material_id}，"
            "每条回填必须明确选择该原料的化验单版本。",
            {"material_id": material_id, "assay_version_id": assay_version_id},
        )
    return ass


def _next_seq(db: Session, batch_id: int) -> int:
    last = db.scalar(
        select(models.ReconEvent.seq)
        .where(models.ReconEvent.batch_id == batch_id)
        .order_by(models.ReconEvent.seq.desc())
        .limit(1)
    )
    return (last or 0) + 1


def make_event_payload(plan_item: dict, ass: models.AssayVersion, material_moisture: float,
                       moisture_pct: float, basis_qty: str, qty: float,
                       cost_per_t_wet: float) -> dict:
    """按实测含水率做干湿基换算并计价，返回事件持久化字段。"""
    if basis_qty == "wet":
        mass_wet = float(qty)
        mass_dry = mass_wet * (1.0 - moisture_pct / 100.0)
    else:
        mass_dry = float(qty)
        mass_wet = mass_dry / (1.0 - moisture_pct / 100.0)
    water = mass_wet - mass_dry
    cost = mass_wet * cost_per_t_wet

    # 化验成分换算到干基：湿基化验单按档案含水率（与计划试算同一口径）
    comp_dry = chemistry.convert_composition(
        ass.composition, ass.basis, material_moisture
    )
    trace = chemistry.build_conversion_trace(
        plan_item["material_code"], plan_item["material_name"],
        ass.composition, ass.basis, material_moisture,
    )
    trace["delivery_mass_balance"] = {
        "delivered_moisture_pct": moisture_pct,
        "input_basis": basis_qty,
        "input_qty_t": round(float(qty), 6),
        "formula": (f"{qty:g} × (1 - {moisture_pct}/100)" if basis_qty == "wet"
                    else f"{qty:g} / (1 - {moisture_pct}/100)"),
        "mass_t_dry": round(mass_dry, 6),
        "mass_t_wet": round(mass_wet, 6),
        "water_t": round(water, 6),
        "cost": f"{mass_wet:.4f} t × {cost_per_t_wet:g} 元/t = {cost:.2f} 元",
    }
    return {
        "moisture_pct": float(moisture_pct),
        "mass_t_dry": mass_dry,
        "mass_t_wet": mass_wet,
        "water_t": water,
        "cost": cost,
        "assay_snapshot": {
            "assay_version_id": ass.id,
            "assay_version": ass.version,
            "lab_report_no": ass.lab_report_no,
            "basis": ass.basis,
            "composition": dict(ass.composition),
            "measured_oxides": list(ass.measured_oxides),
            "composition_dry_used": {k: round(v, 6) for k, v in comp_dry.items()},
            "conversion_trace": trace,
            "cost_per_t_wet": cost_per_t_wet,
        },
    }


# ---------------------------------------------------------------- 追加事件
def append_receive(db: Session, batch: models.ReconBatch, req, *,
                   ev_type="receive", sign=1, reverses_event_id=None,
                   corrects_event_id=None) -> models.ReconEvent:
    """登记到料/更正事件。同 client_event_id 重放返回原事件（幂等）。"""
    plan = batch.plan_snapshot
    existing = db.scalar(select(models.ReconEvent).where(
        models.ReconEvent.batch_id == batch.id,
        models.ReconEvent.client_event_id == req.client_event_id,
    ))
    if existing is not None:
        if not _same_request(existing, req):
            raise BlendError(
                "IDEMPOTENCY_CONFLICT",
                f"client_event_id={req.client_event_id} 已存在但请求体不同，"
                "拒绝覆盖；改错请使用冲销/更正接口。",
                {"client_event_id": req.client_event_id,
                 "existing_event_id": existing.id},
            )
        return existing  # 重复回传：不重复计数

    pi = _find_plan_item(plan, req.plan_item_id)
    mat = db.get(models.Material, pi["material_id"])
    ass = _resolve_assay(db, pi["material_id"], req.assay_version_id)
    basis_qty, qty = req.quantity()
    unit_price = req.cost_per_t_wet if req.cost_per_t_wet is not None else mat.cost_per_t_wet
    payload = make_event_payload(
        pi, ass, mat.moisture_pct, req.moisture_pct, basis_qty, qty, unit_price
    )

    ev = models.ReconEvent(
        batch_id=batch.id, seq=_next_seq(db, batch.id),
        client_event_id=req.client_event_id, type=ev_type, sign=sign,
        plan_item_id=pi["plan_item_id"], material_id=pi["material_id"],
        assay_version_id=ass.id,
        moisture_pct=payload["moisture_pct"],
        mass_t_dry=payload["mass_t_dry"], mass_t_wet=payload["mass_t_wet"],
        water_t=payload["water_t"], cost=payload["cost"],
        assay_snapshot=payload["assay_snapshot"],
        reverses_event_id=reverses_event_id,
        corrects_event_id=corrects_event_id,
        note=req.note,
        occurred_at=req.occurred_at or datetime.utcnow(),
    )
    db.add(ev)
    db.flush()
    recompute_batch(db, batch)
    return ev


def _same_request(ev: models.ReconEvent, req) -> bool:
    return (ev.plan_item_id == req.plan_item_id
            and ev.assay_version_id == req.assay_version_id
            and abs(ev.moisture_pct - req.moisture_pct) < 1e-9
            and (req.mass_t_wet is None
                 or abs(ev.mass_t_wet - req.mass_t_wet) < 1e-9)
            and (req.mass_t_dry is None
                 or abs(ev.mass_t_dry - req.mass_t_dry) < 1e-9))


def reverse_event(db: Session, batch: models.ReconBatch, req) -> list[models.ReconEvent]:
    """冲销/更正：原事件保留，追加反向事件；correction 再追加一条正向新事件。"""
    plan = batch.plan_snapshot
    target = db.get(models.ReconEvent, req.target_event_id)
    if target is None or target.batch_id != batch.id:
        raise BlendError("EVENT_NOT_FOUND",
                         f"事件 id={req.target_event_id} 不属于该批次。",
                         {"target_event_id": req.target_event_id})
    if target.type in ("reversal",):
        raise BlendError("EVENT_NOT_REVERSIBLE",
                         "冲销事件不能再次冲销；请冲销对应的原始到料事件。",
                         {"target_event_id": target.id})
    dup = db.scalar(select(models.ReconEvent).where(
        models.ReconEvent.batch_id == batch.id,
        models.ReconEvent.client_event_id == req.client_event_id,
    ))
    if dup is not None:
        # 幂等重放：reverse 直接返回原反向事件；correct 返回反向+更正两条
        if req.mode == "reverse":
            return [dup]
        corr = db.scalar(select(models.ReconEvent).where(
            models.ReconEvent.batch_id == batch.id,
            models.ReconEvent.client_event_id == f"{req.client_event_id}:correction",
        ))
        return [dup] + ([corr] if corr is not None else [])
    already = db.scalar(select(models.ReconEvent).where(
        models.ReconEvent.batch_id == batch.id,
        models.ReconEvent.reverses_event_id == target.id,
    ))
    if already is not None:
        raise BlendError("EVENT_ALREADY_REVERSED",
                         f"事件 id={target.id} 已被冲销事件 id={already.id} 冲销，"
                         "不能重复冲销。",
                         {"target_event_id": target.id,
                          "reversal_event_id": already.id})

    pi = _find_plan_item(plan, target.plan_item_id)
    reversal = models.ReconEvent(
        batch_id=batch.id, seq=_next_seq(db, batch.id),
        client_event_id=req.client_event_id,
        type="reversal", sign=-target.sign,
        plan_item_id=target.plan_item_id, material_id=target.material_id,
        assay_version_id=target.assay_version_id,
        moisture_pct=target.moisture_pct,
        mass_t_dry=target.mass_t_dry, mass_t_wet=target.mass_t_wet,
        water_t=target.water_t, cost=target.cost,
        assay_snapshot=target.assay_snapshot,
        reverses_event_id=target.id,
        note=req.note or f"冲销事件 #{target.seq}（{target.client_event_id}）",
        occurred_at=req.occurred_at or datetime.utcnow(),
    )
    db.add(reversal)
    db.flush()
    result = [reversal]

    if req.mode == "correct":
        if (req.assay_version_id is None or req.moisture_pct is None
                or (req.mass_t_wet is None and req.mass_t_dry is None)):
            raise BlendError(
                "BAD_CORRECTION",
                "更正必须给出 assay_version_id、moisture_pct 及"
                " mass_t_wet/mass_t_dry 之一。",
            )
        mat = db.get(models.Material, pi["material_id"])
        ass = _resolve_assay(db, pi["material_id"], req.assay_version_id)
        basis_qty, qty = ("wet", req.mass_t_wet) if req.mass_t_wet is not None \
            else ("dry", req.mass_t_dry)
        unit_price = (req.cost_per_t_wet if req.cost_per_t_wet is not None
                      else mat.cost_per_t_wet)
        payload = make_event_payload(
            pi, ass, mat.moisture_pct, req.moisture_pct, basis_qty, qty, unit_price
        )
        corr = models.ReconEvent(
            batch_id=batch.id, seq=_next_seq(db, batch.id),
            client_event_id=f"{req.client_event_id}:correction",
            type="correction", sign=1,
            plan_item_id=pi["plan_item_id"], material_id=pi["material_id"],
            assay_version_id=ass.id,
            moisture_pct=payload["moisture_pct"],
            mass_t_dry=payload["mass_t_dry"], mass_t_wet=payload["mass_t_wet"],
            water_t=payload["water_t"], cost=payload["cost"],
            assay_snapshot=payload["assay_snapshot"],
            corrects_event_id=target.id,
            note=req.note or f"更正事件 #{target.seq}",
            occurred_at=req.occurred_at or datetime.utcnow(),
        )
        db.add(corr)
        db.flush()
        result.append(corr)

    recompute_batch(db, batch)
    return result


# ---------------------------------------------------------------- 累计重算
def _accumulate(events: list[models.ReconEvent]) -> dict:
    """按 plan_item_id 汇总带符号累计（与到达顺序无关）。"""
    acc: dict[int, dict] = {}
    for ev in events:
        key = ev.plan_item_id
        a = acc.setdefault(key, {
            "plan_item_id": key, "material_id": ev.material_id,
            "mass_t_dry": 0.0, "mass_t_wet": 0.0, "water_t": 0.0,
            "cost": 0.0, "comp_mass": {}, "assay_versions": [],
            "event_count": 0,
        })
        for f in ("mass_t_dry", "mass_t_wet", "water_t", "cost"):
            a[f] += ev.sign * getattr(ev, f)
        snap = ev.assay_snapshot or {}
        comp = snap.get("composition_dry_used") or {}
        for ox, v in comp.items():
            a["comp_mass"][ox] = a["comp_mass"].get(ox, 0.0) \
                + ev.sign * ev.mass_t_dry * float(v) / 100.0
        ver = f"{snap.get('assay_version')}（{snap.get('lab_report_no')}）"
        if ver not in a["assay_versions"]:
            a["assay_versions"].append(ver)
        a["event_count"] += 1
    return acc


def _check_required_measured(events, extra_keys: list[str]) -> list[dict]:
    """参与合成的每张化验单：四大组分 + 被设限有害组分必须实测。"""
    needed = set(REQUIRED_OXIDES)
    for k in extra_keys:
        if k == "alkali_eq":
            needed.update(["Na2O", "K2O"])
        else:
            needed.add(k)
    missing = []
    seen = set()
    for ev in events:
        if ev.sign <= 0:
            continue
        snap = ev.assay_snapshot or {}
        measured = set(snap.get("measured_oxides") or [])
        for comp in needed:
            if comp not in measured:
                tag = (ev.id, comp)
                if tag in seen:
                    continue
                seen.add(tag)
                missing.append({
                    "event_id": ev.id, "seq": ev.seq,
                    "material_id": ev.material_id,
                    "assay_version": snap.get("assay_version"),
                    "lab_report_no": snap.get("lab_report_no"),
                    "component": comp,
                })
    return missing


def recompute_batch(db: Session, batch: models.ReconBatch) -> dict:
    """重放全部事件 → 累计/差异/状态，并持久化 diff_snapshot。"""
    plan = batch.plan_snapshot
    events = list(db.scalars(select(models.ReconEvent)
                             .where(models.ReconEvent.batch_id == batch.id)
                             .order_by(models.ReconEvent.seq)))
    acc = _accumulate(events)

    issues: list[dict] = []
    tol_rel = batch.tolerance_pct / 100.0

    # ---- 逐原料差异 + 数量闭合判定 ----
    item_rows, total_actual = [], {
        "mass_t_dry": 0.0, "mass_t_wet": 0.0, "water_t": 0.0, "cost": 0.0}
    quantity_closed = len(events) > 0
    for pi in plan["items"]:
        a = acc.get(pi["plan_item_id"], {
            "mass_t_dry": 0.0, "mass_t_wet": 0.0, "water_t": 0.0, "cost": 0.0,
            "comp_mass": {}, "assay_versions": [], "event_count": 0})
        actual = {k: round(a[k], 6) for k in
                  ("mass_t_dry", "mass_t_wet", "water_t", "cost")}
        diff = {k: round(actual[k] - pi[k], 6) for k in actual}
        tol = max(batch.abs_tol_t, abs(pi["mass_t_dry"]) * tol_rel)
        closed = abs(actual["mass_t_dry"] - pi["mass_t_dry"]) <= tol
        if not closed:
            quantity_closed = False
        if actual["mass_t_dry"] < -1e-9:
            issues.append({"code": "NEGATIVE_MASS",
                           "message": f"{pi['material_code']} 冲销后累计干基质量为负"
                                      f"（{actual['mass_t_dry']:.4f} t）。",
                           "plan_item_id": pi["plan_item_id"]})
        for k in total_actual:
            total_actual[k] += actual[k]
        item_rows.append({
            "plan_item_id": pi["plan_item_id"],
            "material_id": pi["material_id"],
            "material_code": pi["material_code"],
            "material_name": pi["material_name"],
            "plan_assay_version": pi["assay_version"],
            "plan_lab_report_no": pi["lab_report_no"],
            "plan_assay_basis": pi["assay_basis"],
            "actual_assay_versions": a["assay_versions"],
            "plan": {k: pi[k] for k in
                     ("mass_t_dry", "mass_t_wet", "water_t", "cost",
                      "share_pct_dry", "moisture_pct")},
            "actual": actual,
            "diff": diff,
            "dry_tolerance_t": round(tol, 6),
            "closed": closed,
            "event_count": a["event_count"],
        })

    plan_tot = plan["totals"]
    totals = {
        "plan": plan_tot,
        "actual": {k: round(v, 6) for k, v in total_actual.items()},
    }
    totals["diff"] = {k: round(totals["actual"][k] - plan_tot[k], 6)
                      for k in plan_tot}

    # ---- 缺测检查 ----
    missing = _check_expected_measured(events, plan) if events else []
    for m in missing:
        issues.append({"code": "MISSING_ASSAY",
                       "message": f"事件 #{m['seq']} 选用的化验单 "
                                  f"{m['assay_version']} 缺测 {m['component']}，"
                                  "未测不允许按零含量关闭批次。",
                       **m})

    # ---- 实际合成成分（干基，质量守恒）与率值 ----
    actual_comp, actual_ind, zero_denom = None, None, None
    if total_actual["mass_t_dry"] > 1e-9:
        comp_mass: dict[str, float] = {}
        for a in acc.values():
            for ox, v in a["comp_mass"].items():
                comp_mass[ox] = comp_mass.get(ox, 0.0) + v
        actual_comp = {ox: round(100.0 * v / total_actual["mass_t_dry"], 6)
                       for ox, v in comp_mass.items()}
        try:
            actual_ind = chemistry.calc_indicators(actual_comp).as_dict()
        except chemistry.ZeroDenominatorError as e:
            zero_denom = {"indicator": e.details["indicator"],
                          "denominator": e.details["denominator"]}
            issues.append({"code": "ZERO_DENOMINATOR",
                           "message": e.message, **e.details})
        except chemistry.MissingAssayError as e:
            # 缺测已在前面的 measured 检查中登记；这里不因 KeyError 中断投影
            for m in e.details.get("missing", []):
                issues.append({"code": "MISSING_ASSAY",
                               "message": "合成成分缺少必测组分，"
                                          "未测不允许按零含量处理。", **m})

    # ---- 率值窗口 + 有害组分上限 ----
    indicator_checks, in_range = [], True
    for key in ("SM", "IM", "KH"):
        iv = plan["targets"].get(key) or {}
        lo, hi = iv.get("min"), iv.get("max")
        val = actual_ind.get(key) if actual_ind else None
        out = bool(val is not None and ((lo is not None and val < lo - 1e-9)
                                        or (hi is not None and val > hi + 1e-9)))
        plan_val = plan["indicators"].get(key)
        indicator_checks.append({
            "indicator": key, "min": lo, "max": hi,
            "plan": plan_val, "actual": val,
            "diff": (round(val - plan_val, 6) if val is not None and plan_val is not None
                     else None),
            "out_of_range": out,
        })
        in_range = in_range and not out

    hazard_checks = []
    for key, limit in (plan.get("hazard_limits_pct") or {}).items():
        if key == "alkali_eq":
            val = (round(actual_comp.get("Na2O", 0.0)
                         + ALKALI_EQ_FACTOR_K2O * actual_comp.get("K2O", 0.0), 4)
                   if actual_comp and "Na2O" in actual_comp and "K2O" in actual_comp
                   else None)
        else:
            val = actual_comp.get(key) if actual_comp else None
        out = val is not None and val > float(limit) + 1e-9
        plan_val = plan.get("hazard_values", {}).get(key)
        hazard_checks.append({
            "hazard": key, "limit": float(limit),
            "plan": plan_val, "actual": val,
            "diff": round(val - plan_val, 6) if val is not None and plan_val is not None
            else None,
            "out_of_range": out,
        })
        in_range = in_range and not out

    hard_issue = any(i["code"] in ("MISSING_ASSAY", "ZERO_DENOMINATOR", "NEGATIVE_MASS")
                     for i in issues)
    if hard_issue:
        # 缺测/零分母/负质量属硬性异常，与数量是否闭合无关
        status = STATUS_ABNORMAL
    elif quantity_closed and not in_range:
        status = STATUS_ABNORMAL
        issues.append({"code": "INDICATOR_OUT_OF_RANGE",
                       "message": "数量已闭合但实际 KH/率值或有害组分越界，"
                                  "计划方案不改动，批次标为异常。"})
    elif quantity_closed:
        status = STATUS_RECONCILED
    else:
        status = STATUS_PENDING

    snapshot = {
        "totals": totals,
        "items": item_rows,
        "actual_composition_dry_pct": actual_comp,
        "plan_composition_dry_pct": plan.get("composition_dry_pct"),
        "indicators": indicator_checks,
        "hazards": hazard_checks,
        "quantity_closed": quantity_closed,
        "in_range": in_range,
        "hard_issue": hard_issue,
        "issues": issues,
        "event_count": len(events),
        "computed_at": datetime.utcnow().isoformat(timespec="seconds"),
    }
    batch.status = status
    batch.diff_snapshot = snapshot
    db.flush()
    return snapshot


def _check_expected_measured(events, plan: dict) -> list[dict]:
    return _check_required_measured(events,
                                    list((plan.get("hazard_limits_pct") or {}).keys()))


# ---------------------------------------------------------------- CRUD 外观
def create_batch(db: Session, req) -> models.ReconBatch:
    run = db.get(models.BlendRun, req.run_id)
    if run is None:
        raise BlendError("RUN_NOT_FOUND", f"试算 run_id={req.run_id} 不存在。",
                         {"run_id": req.run_id})
    solution = db.get(models.BlendSolution, req.solution_id)
    if solution is None or solution.run_id != run.id:
        raise BlendError(
            "SOLUTION_NOT_FOUND",
            f"方案 solution_id={req.solution_id} 不属于 run_id={req.run_id}，"
            "回填批次只能从已保存方案创建。",
            {"run_id": req.run_id, "solution_id": req.solution_id},
        )
    if not solution.success or not solution.items:
        raise BlendError(
            "PLAN_NOT_RECONCILABLE",
            "该方案求解失败或没有计划原料项，不能作为对账基准。",
            {"solution_id": solution.id},
        )
    plan = build_plan_snapshot(db, run, solution)
    batch = models.ReconBatch(
        batch_code=req.batch_code or f"RC-{uuid.uuid4().hex[:10].upper()}",
        run_id=run.id, solution_id=solution.id,
        status=STATUS_PENDING,
        tolerance_pct=req.tolerance_pct, abs_tol_t=req.abs_tol_t,
        plan_snapshot=plan, remark=req.remark,
    )
    db.add(batch)
    db.flush()
    batch.diff_snapshot = recompute_batch(db, batch)
    return batch


def list_batches(db: Session, limit: int = 50) -> list[dict]:
    rows = list(db.scalars(
        select(models.ReconBatch).order_by(models.ReconBatch.id.desc()).limit(limit)
    ))
    return [{
        "id": b.id, "batch_code": b.batch_code,
        "run_id": b.run_id, "solution_id": b.solution_id,
        "status": b.status,
        "scenario_name": (b.plan_snapshot or {}).get("run_code"),
        "event_count": (b.diff_snapshot or {}).get("event_count", 0),
        "quantity_closed": (b.diff_snapshot or {}).get("quantity_closed", False),
        "created_at": b.created_at.isoformat(timespec="seconds"),
        "updated_at": b.updated_at.isoformat(timespec="seconds"),
    } for b in rows]


def _event_out(ev: models.ReconEvent) -> dict:
    return {
        "id": ev.id, "seq": ev.seq, "client_event_id": ev.client_event_id,
        "type": ev.type, "sign": ev.sign,
        "plan_item_id": ev.plan_item_id, "material_id": ev.material_id,
        "assay_version_id": ev.assay_version_id,
        "moisture_pct": ev.moisture_pct,
        "mass_t_dry": round(ev.mass_t_dry, 6),
        "mass_t_wet": round(ev.mass_t_wet, 6),
        "water_t": round(ev.water_t, 6),
        "cost": round(ev.cost, 2),
        "signed_mass_t_dry": round(ev.sign * ev.mass_t_dry, 6),
        "signed_cost": round(ev.sign * ev.cost, 2),
        "assay_snapshot": ev.assay_snapshot,
        "reverses_event_id": ev.reverses_event_id,
        "corrects_event_id": ev.corrects_event_id,
        "note": ev.note,
        "occurred_at": ev.occurred_at.isoformat(timespec="seconds"),
        "created_at": ev.created_at.isoformat(timespec="seconds"),
    }


def batch_detail(db: Session, batch_id: int) -> dict | None:
    batch = db.get(models.ReconBatch, batch_id)
    if batch is None:
        return None
    return {
        "id": batch.id,
        "batch_code": batch.batch_code,
        "run_id": batch.run_id,
        "solution_id": batch.solution_id,
        "status": batch.status,
        "tolerance_pct": batch.tolerance_pct,
        "abs_tol_t": batch.abs_tol_t,
        "remark": batch.remark,
        "created_at": batch.created_at.isoformat(timespec="seconds"),
        "updated_at": batch.updated_at.isoformat(timespec="seconds"),
        "plan": batch.plan_snapshot,
        "diff": batch.diff_snapshot,
        "events": [_event_out(e) for e in batch.events],
    }


def close_batch(db: Session, batch: models.ReconBatch, force: bool = False) -> dict:
    """显式关闭：重新投影后给出明确拒绝原因（缺测/零分母/未闭合/越界）。"""
    snap = recompute_batch(db, batch)
    if force:
        batch.status = STATUS_ABNORMAL
        db.flush()
        return snap
    if snap["issues"] and any(i["code"] == "MISSING_ASSAY" for i in snap["issues"]):
        raise BlendError("MISSING_ASSAY",
                         "存在缺测组分的化验单，未测不得按零含量处理，批次不能关闭。",
                         {"issues": snap["issues"]})
    if any(i["code"] == "ZERO_DENOMINATOR" for i in snap["issues"]):
        raise BlendError("ZERO_DENOMINATOR",
                         "实际合成率值分母为零，指标无定义，批次不能关闭。",
                         {"issues": snap["issues"]})
    if any(i["code"] == "NEGATIVE_MASS" for i in snap["issues"]):
        raise BlendError("NEGATIVE_MASS",
                         "冲销后存在负的累计到料量，先补登记正向到料。",
                         {"issues": snap["issues"]})
    if not snap["quantity_closed"]:
        pending = [{"material_code": it["material_code"],
                    "diff_mass_t_dry": it["diff"]["mass_t_dry"],
                    "tolerance_t": it["dry_tolerance_t"]}
                   for it in snap["items"] if not it["closed"]]
        raise BlendError("QUANTITY_NOT_CLOSED",
                         "实际到料量未与计划闭合，批次保持待对账。",
                         {"pending": pending})
    if not snap["in_range"]:
        raise BlendError("INDICATOR_OUT_OF_RANGE",
                         "数量闭合但实际 KH/率值或有害组分越界，批次转为异常，"
                         "计划方案不允许修改。",
                         {"indicators": [c for c in snap["indicators"]
                                         if c["out_of_range"]],
                          "hazards": [c for c in snap["hazards"]
                                      if c["out_of_range"]]})
    return snap
