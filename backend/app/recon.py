"""离线试验批次回填账本：只追加事件 + 由事件流重算的累计/差异/状态。

设计口径（与试算一致）：
1. 计划侧在创建批次时从 blend_solution/blend_item 冻结快照，之后绝不修改；
2. 实际侧只追加事件（到料/冲销/更正），每条事件显式绑定计划原料项与
   化验版本——禁止回退到原料“当前最新”化验单；
3. 累计量 = Σ 事件净贡献（带符号），与到达顺序无关；重复 event_id 幂等；
4. 实际率值/有害组分由累计干基组分质量守恒合成后计算，分母为零或缺测
   一律显式报错并阻止批次进入“已对账”；
5. 每次入账后把状态与差异快照持久化到 recon_batch.state_snapshot。
"""
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import chemistry, models

# 状态
PENDING = "pending"        # 待对账
RECONCILED = "reconciled"  # 已对账
EXCEPTION = "exception"    # 异常

# 事件类型
RECEIPT = "receipt"
REVERSAL = "reversal"
CORRECTION = "correction"

# 数量闭合容差：max(绝对 0.01t, 计划干基量的 0.1%)
TOL_ABS_T = 0.01
TOL_REL = 1e-3

BASE_REQUIRED = ["CaO", "SiO2", "Al2O3", "Fe2O3"]
HAZARD_KEYS = ["MgO", "SO3", "K2O", "Na2O", "Cl", "alkali_eq"]


# ---------------------------------------------------------------- 计划快照

def _plan_hazards(comp_dry_pct: dict) -> dict:
    out = {}
    for k in ["MgO", "SO3", "K2O", "Na2O", "Cl"]:
        if k in comp_dry_pct:
            out[k] = round(float(comp_dry_pct[k]), 6)
    alk = chemistry.alkali_equivalent(comp_dry_pct)
    if alk is not None:
        out["alkali_eq"] = round(alk, 6)
    return out


def build_plan_snapshot(db: Session, run: models.BlendRun,
                        sol: models.BlendSolution) -> dict:
    """从已保存方案冻结计划快照（化验版/换算留痕逐原料固定）。"""
    items = []
    for it in db.scalars(
        select(models.BlendItem)
        .where(models.BlendItem.solution_id == sol.id)
        .order_by(models.BlendItem.id)
    ):
        mat = db.get(models.Material, it.material_id)
        ass = db.get(models.AssayVersion, it.assay_version_id)
        comp_dry = chemistry.convert_composition(
            ass.composition, ass.basis, mat.moisture_pct
        )
        items.append({
            "blend_item_id": it.id,
            "material_id": mat.id,
            "material_code": mat.code,
            "material_name": mat.name,
            "moisture_pct": mat.moisture_pct,
            "cost_per_t_wet": mat.cost_per_t_wet,
            # 计划化验版：冻结，永不随“最新化验单”变化
            "assay_version_id": ass.id,
            "assay_version": ass.version,
            "lab_report_no": ass.lab_report_no,
            "basis": ass.basis,
            "share_pct_dry": it.share_pct_dry,
            "mass_t_dry": it.mass_t_dry,
            "mass_t_wet": it.mass_t_wet,
            "water_t": it.water_t,
            "cost": it.cost,
            "composition_dry": {k: round(v, 6) for k, v in comp_dry.items()},
            "measured_oxides": list(ass.measured_oxides),
            "conversion_trace": it.conversion_trace,
        })
    payload = sol.indicators or {}
    comp_plan = payload.get("composition_dry_pct") or {}
    totals = {
        "mass_t_dry": round(sum(i["mass_t_dry"] for i in items), 6),
        "mass_t_wet": round(sum(i["mass_t_wet"] for i in items), 6),
        "water_t": round(sum(i["water_t"] for i in items), 6),
        "cost": round(sum(i["cost"] for i in items), 6),
    }
    return {
        "run_id": run.id,
        "run_code": run.run_code,
        "solution_id": sol.id,
        "mode": sol.mode,
        "scenario_name": run.scenario_name,
        "batch_t_dry": run.batch_t_dry,
        "targets": run.target,
        "hazard_limits_pct": (run.constraint_set or {}).get("hazard_limits_pct", {}),
        "items": items,
        "totals": totals,
        "indicators": payload.get("indicators"),
        "composition_dry_pct": comp_plan,
        "hazards": _plan_hazards(comp_plan),
    }


# ---------------------------------------------------------------- 事件贡献

def _required_components(hazard_limits: dict) -> list[str]:
    req = list(BASE_REQUIRED)
    for key in (hazard_limits or {}):
        if key == "alkali_eq":
            req += ["Na2O", "K2O"]
        else:
            req.append(key)
    return req


def check_assay_measured(mat: models.Material, ass: models.AssayVersion,
                         hazard_limits: dict) -> None:
    """所选化验版必须实测全部必测组分；缺测直接报错，不以零兜底。"""
    chemistry.require_measured(
        [{
            "code": mat.code, "name": mat.name, "version": ass.version,
            "lab_report_no": ass.lab_report_no,
            "composition": ass.composition,
            "measured_oxides": list(ass.measured_oxides or []),
        }],
        _required_components(hazard_limits),
    )


def receipt_contribution(mat: models.Material, ass: models.AssayVersion,
                         mass_t_wet: float) -> tuple[dict, dict]:
    """到料贡献：湿基吨 -> 干基吨/水量/成本/逐组分干基吨。

    返回 (delta, assay_snapshot)。换算口径与试算完全一致：
    干基% = 湿基化验值 /(1-w)；干料吨 = 湿料吨 ×(1-w)；成本按湿料吨价。
    """
    w = mat.moisture_pct / 100.0
    factor = chemistry.dry_factor(mat.moisture_pct)
    comp_dry = chemistry.convert_composition(
        ass.composition, ass.basis, mat.moisture_pct
    )
    mass_dry = mass_t_wet * (1.0 - w)
    delta = {
        "mass_t_wet": mass_t_wet,
        "mass_t_dry": mass_dry,
        "water_t": mass_t_wet - mass_dry,
        "cost": mass_t_wet * mat.cost_per_t_wet,
        "components_t": {k: mass_dry * v / 100.0 for k, v in comp_dry.items()},
    }
    snapshot = {
        "assay_version_id": ass.id,
        "assay_version": ass.version,
        "lab_report_no": ass.lab_report_no,
        "basis": ass.basis,
        "moisture_pct": mat.moisture_pct,
        "dry_factor": round(factor, 6),
        "composition_raw": dict(ass.composition),
        "composition_dry": {k: round(v, 6) for k, v in comp_dry.items()},
        "measured_oxides": list(ass.measured_oxides or []),
        "formula": (f"干基% = 湿基% / (1 - {mat.moisture_pct}/100)；"
                    f"干料t = {mass_t_wet} × (1 - {mat.moisture_pct}/100)"),
    }
    return delta, snapshot


def _negate(delta: dict) -> dict:
    return {
        "mass_t_wet": -delta["mass_t_wet"],
        "mass_t_dry": -delta["mass_t_dry"],
        "water_t": -delta["water_t"],
        "cost": -delta["cost"],
        "components_t": {k: -v for k, v in delta["components_t"].items()},
    }


def _round_delta(delta: dict) -> dict:
    return {
        "mass_t_wet": round(delta["mass_t_wet"], 6),
        "mass_t_dry": round(delta["mass_t_dry"], 6),
        "water_t": round(delta["water_t"], 6),
        "cost": round(delta["cost"], 6),
        "components_t": {k: round(v, 8) for k, v in delta["components_t"].items()},
    }


# ---------------------------------------------------------------- 累计与状态

def _empty_acc() -> dict:
    return {"mass_t_wet": 0.0, "mass_t_dry": 0.0, "water_t": 0.0, "cost": 0.0,
            "components_t": {}}


def _accumulate(acc: dict, delta: dict) -> None:
    acc["mass_t_wet"] += delta["mass_t_wet"]
    acc["mass_t_dry"] += delta["mass_t_dry"]
    acc["water_t"] += delta["water_t"]
    acc["cost"] += delta["cost"]
    for k, v in delta["components_t"].items():
        acc["components_t"][k] = acc["components_t"].get(k, 0.0) + v


def compute_state(plan: dict, events: list[models.ReconEvent]) -> dict:
    """由计划快照 + 全部事件重算累计、差异与批次状态（纯函数，顺序无关）。"""
    per_item: dict[int, dict] = {}
    total = _empty_acc()
    for ev in events:  # 加法可交换：乱序到达结果一致
        acc = per_item.setdefault(ev.blend_item_id, _empty_acc())
        _accumulate(acc, ev.delta)
        _accumulate(total, ev.delta)

    items, all_closed = [], True
    for p in plan["items"]:
        acc = per_item.get(p["blend_item_id"], _empty_acc())
        tol = max(TOL_ABS_T, abs(p["mass_t_dry"]) * TOL_REL)
        diff_dry = acc["mass_t_dry"] - p["mass_t_dry"]
        closed = abs(diff_dry) <= tol
        all_closed = all_closed and closed
        items.append({
            "blend_item_id": p["blend_item_id"],
            "material_code": p["material_code"],
            "material_name": p["material_name"],
            "plan": {k: round(p[k], 6)
                     for k in ("mass_t_dry", "mass_t_wet", "water_t", "cost")},
            "actual": {k: round(acc[k], 6)
                       for k in ("mass_t_dry", "mass_t_wet", "water_t", "cost")},
            "diff": {
                "mass_t_dry": round(diff_dry, 6),
                "mass_t_wet": round(acc["mass_t_wet"] - p["mass_t_wet"], 6),
                "water_t": round(acc["water_t"] - p["water_t"], 6),
                "cost": round(acc["cost"] - p["cost"], 6),
            },
            "closed": closed,
            "tol_t": round(tol, 6),
        })

    # --- 实际化学：累计干基组分质量守恒合成 ---
    calc_errors, actual_ind, actual_hazards, actual_comp = [], None, None, None
    if total["mass_t_dry"] > 1e-12:
        actual_comp = {
            k: round(v / total["mass_t_dry"] * 100.0, 6)
            for k, v in total["components_t"].items()
        }
        try:
            actual_ind = chemistry.calc_indicators(actual_comp).as_dict()
        except chemistry.ZeroDenominatorError as e:
            calc_errors.append({"code": e.code, "message": e.message,
                                "details": e.details})
        except chemistry.MissingAssayError as e:
            calc_errors.append({"code": e.code, "message": e.message,
                                "details": e.details})
        hazards = {}
        for k in ["MgO", "SO3", "K2O", "Na2O", "Cl"]:
            if k in actual_comp:
                hazards[k] = actual_comp[k]
        alk = chemistry.alkali_equivalent(actual_comp)
        if alk is not None:
            hazards["alkali_eq"] = round(alk, 6)
        actual_hazards = hazards

    # --- 越界判定（率值窗口 + 有害组分上限） ---
    violations = []
    targets = plan.get("targets") or {}
    if actual_ind is not None:
        for key in ("SM", "IM", "KH"):
            win = targets.get(key) or {}
            lo, hi = win.get("min"), win.get("max")
            val = actual_ind.get(key)
            if val is None:
                continue
            if (lo is not None and val < lo - 1e-9) or \
               (hi is not None and val > hi + 1e-9):
                violations.append({
                    "kind": key, "actual": round(val, 4),
                    "window": [lo, hi],
                    "message": f"实际 {key}={val:.4f} 越出目标窗口 "
                               f"[{lo}, {hi}]",
                })
    limits = plan.get("hazard_limits_pct") or {}
    if actual_hazards is not None:
        for key, limit in limits.items():
            val = actual_hazards.get(key)
            if val is None:
                calc_errors.append({
                    "code": "MISSING_ASSAY",
                    "message": f"实际合成缺少受限有害组分 {key}，无法判定上限，"
                               "不允许对账关闭。",
                    "details": {"component": key},
                })
                continue
            if val > float(limit) + 1e-9:
                violations.append({
                    "kind": f"hazard_{key}", "actual": round(val, 6),
                    "limit": float(limit),
                    "message": f"实际有害组分 {key}={val:.4f}% 超过干基上限 "
                               f"{limit}%",
                })

    # 越界/计算错误在数量闭合前仅作预警记录；闭合后才决定状态，
    # 保证“数量闭合前保持待对账”，闭合瞬间越界即异常、零分母不得关闭。
    if not all_closed:
        status = PENDING
    elif calc_errors or violations:
        status = EXCEPTION
    else:
        status = RECONCILED

    plan_ind = plan.get("indicators") or {}
    ind_diff = None
    if actual_ind is not None and plan_ind:
        ind_diff = {k: round(actual_ind[k] - plan_ind[k], 4)
                    for k in ("SM", "IM", "KH") if k in plan_ind}

    return {
        "status": status,
        "event_count": len(events),
        "all_closed": all_closed,
        "violations_evaluated": all_closed,  # 闭合前越界仅预警，不决定状态
        "items": items,
        "totals": {
            "plan": plan["totals"],
            "actual": {k: round(total[k], 6)
                       for k in ("mass_t_dry", "mass_t_wet", "water_t", "cost")},
            "diff": {
                "mass_t_dry": round(total["mass_t_dry"] - plan["totals"]["mass_t_dry"], 6),
                "mass_t_wet": round(total["mass_t_wet"] - plan["totals"]["mass_t_wet"], 6),
                "water_t": round(total["water_t"] - plan["totals"]["water_t"], 6),
                "cost": round(total["cost"] - plan["totals"]["cost"], 6),
            },
        },
        "chemistry": {
            "plan_indicators": plan_ind,
            "plan_hazards": plan.get("hazards"),
            "actual_indicators": actual_ind,
            "actual_hazards": actual_hazards,
            "actual_composition_dry_pct": actual_comp,
            "indicator_diff": ind_diff,
            "calc_errors": calc_errors,
        },
        "violations": violations,
        "computed_at": datetime.utcnow().isoformat(timespec="seconds"),
    }


# ---------------------------------------------------------------- 账本操作

def create_batch(db: Session, run_id: int, solution_id: int,
                 note: str | None = None) -> models.ReconBatch:
    run = db.get(models.BlendRun, run_id)
    if run is None:
        raise chemistry.BlendError("RUN_NOT_FOUND", f"试算 run_id={run_id} 不存在。")
    sol = db.get(models.BlendSolution, solution_id)
    if sol is None or sol.run_id != run.id:
        raise chemistry.BlendError(
            "SOLUTION_NOT_FOUND",
            f"方案 solution_id={solution_id} 不属于试算 {run.run_code}。")
    if not sol.success:
        raise chemistry.BlendError(
            "SOLUTION_NOT_FEASIBLE",
            "失败方案无可对账的计划量，不能创建对账批次。")

    import uuid
    batch = models.ReconBatch(
        batch_code=f"RCN-{uuid.uuid4().hex[:10].upper()}",
        run_id=run.id,
        solution_id=sol.id,
        status=PENDING,
        plan_snapshot=build_plan_snapshot(db, run, sol),
        state_snapshot={},
        note=note,
    )
    db.add(batch)
    db.flush()
    batch.state_snapshot = compute_state(batch.plan_snapshot, [])
    batch.status = batch.state_snapshot["status"]
    db.commit()
    db.refresh(batch)
    return batch


def _get_batch(db: Session, batch_id: int) -> models.ReconBatch:
    batch = db.get(models.ReconBatch, batch_id)
    if batch is None:
        raise chemistry.BlendError("BATCH_NOT_FOUND",
                                   f"对账批次 id={batch_id} 不存在。")
    return batch


def _plan_item(plan: dict, blend_item_id: int) -> dict:
    for it in plan["items"]:
        if it["blend_item_id"] == blend_item_id:
            return it
    raise chemistry.BlendError(
        "ITEM_NOT_IN_BATCH",
        f"计划原料项 blend_item_id={blend_item_id} 不在本批次计划快照中。",
        {"blend_item_id": blend_item_id})


def _find_event_by_client_key(db: Session, batch_id: int, event_id: str):
    return db.scalars(
        select(models.ReconEvent).where(
            models.ReconEvent.batch_id == batch_id,
            models.ReconEvent.event_id == event_id,
        )
    ).first()


def _find_event_by_pk(db: Session, batch_id: int, pk: int):
    ev = db.get(models.ReconEvent, pk)
    if ev is None or ev.batch_id != batch_id:
        raise chemistry.BlendError(
            "EVENT_NOT_FOUND", f"事件 id={pk} 不属于批次 {batch_id}。")
    return ev


def _already_referenced(db: Session, batch_id: int, target_pk: int) -> bool:
    return db.scalars(
        select(func.count()).select_from(models.ReconEvent).where(
            models.ReconEvent.batch_id == batch_id,
            models.ReconEvent.reverses_event_id == target_pk,
        )
    ).one() > 0


def _next_seq(db: Session, batch_id: int) -> int:
    return (db.scalars(
        select(func.coalesce(func.max(models.ReconEvent.seq), 0)).where(
            models.ReconEvent.batch_id == batch_id)
    ).one()) + 1


def _persist_event(db: Session, batch: models.ReconBatch, **kw) -> models.ReconEvent:
    ev = models.ReconEvent(
        batch_id=batch.id, seq=_next_seq(db, batch.id), **kw)
    db.add(ev)
    db.flush()
    _refresh_state(db, batch)
    db.commit()
    db.refresh(ev)
    return ev


def _refresh_state(db: Session, batch: models.ReconBatch) -> None:
    events = list(db.scalars(
        select(models.ReconEvent)
        .where(models.ReconEvent.batch_id == batch.id)
        .order_by(models.ReconEvent.seq)
    ))
    state = compute_state(batch.plan_snapshot, events)
    batch.state_snapshot = state
    batch.status = state["status"]
    db.flush()


def _resolve_assay(db: Session, mat: models.Material, assay_version_id: int):
    ass = db.get(models.AssayVersion, assay_version_id)
    if ass is None or ass.material_id != mat.id:
        raise chemistry.BlendError(
            "ASSAY_MATERIAL_MISMATCH",
            f"化验版 id={assay_version_id} 不属于原料 {mat.code}；"
            "回填必须显式选择该原料的化验版本。",
            {"material_id": mat.id, "assay_version_id": assay_version_id})
    return ass


def append_event(db: Session, batch_id: int, req) -> tuple[models.ReconEvent, bool]:
    """追加到料/更正事件。重复 event_id 幂等重放，净贡献只计一次。"""
    batch = _get_batch(db, batch_id)
    plan = batch.plan_snapshot

    existing = _find_event_by_client_key(db, batch.id, req.event_id)
    if existing is not None:
        same = (
            existing.kind == req.kind
            and existing.blend_item_id == req.blend_item_id
            and existing.assay_version_id == req.assay_version_id
            and abs(existing.mass_t_wet - req.mass_t_wet) < 1e-9
            and (existing.reverses_event_id or None) == req.reverses_event_id
        )
        if not same:
            raise chemistry.BlendError(
                "EVENT_ID_CONFLICT",
                f"幂等键 event_id='{req.event_id}' 已用于不同内容的事件，"
                "请更换 event_id。",
                {"event_id": req.event_id})
        return existing, True  # 幂等重放：不再入账

    item = _plan_item(plan, req.blend_item_id)
    mat = db.get(models.Material, item["material_id"])
    ass = _resolve_assay(db, mat, req.assay_version_id)
    # 缺测拦截：不以零含量入账
    check_assay_measured(mat, ass, plan.get("hazard_limits_pct") or {})

    delta, snapshot = receipt_contribution(mat, ass, req.mass_t_wet)

    if req.kind == CORRECTION:
        if req.reverses_event_id is None:
            raise chemistry.BlendError(
                "CORRECTION_NEEDS_TARGET",
                "更正事件必须指定 reverses_event_id（被更正的原始事件）。")
        target = _find_event_by_pk(db, batch.id, req.reverses_event_id)
        _check_reversible(db, batch.id, target)
        delta = _add_deltas(delta, _negate(target.delta))
        snapshot = {**snapshot, "corrects_event_id": target.event_id,
                    "corrects_seq": target.seq}
    elif req.kind != RECEIPT:
        raise chemistry.BlendError("BAD_EVENT_KIND",
                                   f"未知事件类型: {req.kind}")

    ev = _persist_event(
        db, batch,
        event_id=req.event_id, kind=req.kind,
        blend_item_id=item["blend_item_id"], material_id=mat.id,
        assay_version_id=ass.id, mass_t_wet=req.mass_t_wet,
        moisture_pct=mat.moisture_pct, cost_per_t_wet=mat.cost_per_t_wet,
        reverses_event_id=req.reverses_event_id if req.kind == CORRECTION else None,
        assay_snapshot=snapshot, delta=_round_delta(delta),
        note=req.note,
    )
    return ev, False


def _add_deltas(a: dict, b: dict) -> dict:
    out = _empty_acc()
    _accumulate(out, a)
    _accumulate(out, b)
    return out


def _check_reversible(db: Session, batch_id: int,
                      target: models.ReconEvent) -> None:
    if target.kind == REVERSAL:
        raise chemistry.BlendError(
            "CANNOT_REVERSE_REVERSAL",
            "冲销事件本身不可再被冲销/更正（它已是反向留痕）。")
    if _already_referenced(db, batch_id, target.id):
        raise chemistry.BlendError(
            "EVENT_ALREADY_REVERSED",
            f"事件 {target.event_id} 已被冲销或更正过，不可重复抵销。")


def reverse_event(db: Session, batch_id: int, target_pk: int,
                  new_event_id: str, note: str | None = None):
    """冲销已登记事件：追加一条全额反向事件，原始事件保留不改。"""
    batch = _get_batch(db, batch_id)

    existing = _find_event_by_client_key(db, batch.id, new_event_id)
    if existing is not None:
        if existing.kind == REVERSAL and existing.reverses_event_id == target_pk:
            return existing, True
        raise chemistry.BlendError(
            "EVENT_ID_CONFLICT",
            f"幂等键 event_id='{new_event_id}' 已被占用。",
            {"event_id": new_event_id})

    target = _find_event_by_pk(db, batch.id, target_pk)
    _check_reversible(db, batch.id, target)

    ev = _persist_event(
        db, batch,
        event_id=new_event_id, kind=REVERSAL,
        blend_item_id=target.blend_item_id, material_id=target.material_id,
        assay_version_id=target.assay_version_id,
        mass_t_wet=target.mass_t_wet,
        moisture_pct=target.moisture_pct, cost_per_t_wet=target.cost_per_t_wet,
        reverses_event_id=target.id,
        assay_snapshot={**target.assay_snapshot,
                        "reverses_event_id": target.event_id,
                        "reverses_seq": target.seq},
        delta=_round_delta(_negate(target.delta)),
        note=note or f"冲销事件 {target.event_id}",
    )
    return ev, False


def get_batch_detail(db: Session, batch_id: int) -> dict:
    """批次详情：计划快照 + 事件流 + 由事件重算的最新状态（重启一致）。"""
    batch = _get_batch(db, batch_id)
    _refresh_state(db, batch)  # 以事件流为准重算并落库，保证重启后一致
    db.commit()
    return serialize_batch(db, batch)


def serialize_batch(db: Session, batch: models.ReconBatch) -> dict:
    events = list(db.scalars(
        select(models.ReconEvent)
        .where(models.ReconEvent.batch_id == batch.id)
        .order_by(models.ReconEvent.seq)
    ))
    reversed_by = {
        e.reverses_event_id: e.event_id for e in events
        if e.reverses_event_id is not None
    }
    return {
        "id": batch.id,
        "batch_code": batch.batch_code,
        "run_id": batch.run_id,
        "solution_id": batch.solution_id,
        "status": batch.status,
        "note": batch.note,
        "created_at": batch.created_at.isoformat(timespec="seconds"),
        "plan": batch.plan_snapshot,
        "state": batch.state_snapshot,
        "events": [{
            "id": e.id,
            "event_id": e.event_id,
            "seq": e.seq,
            "kind": e.kind,
            "blend_item_id": e.blend_item_id,
            "material_id": e.material_id,
            "assay_version_id": e.assay_version_id,
            "mass_t_wet": e.mass_t_wet,
            "moisture_pct": e.moisture_pct,
            "cost_per_t_wet": e.cost_per_t_wet,
            "reverses_event_id": e.reverses_event_id,
            "reversed_by_event_id": reversed_by.get(e.id),
            "assay_snapshot": e.assay_snapshot,
            "delta": e.delta,
            "note": e.note,
            "created_at": e.created_at.isoformat(timespec="seconds"),
        } for e in events],
    }


def list_batches(db: Session, run_id: int | None = None, limit: int = 100):
    stmt = select(models.ReconBatch).order_by(models.ReconBatch.id.desc()).limit(limit)
    if run_id is not None:
        stmt = stmt.where(models.ReconBatch.run_id == run_id)
    return [{
        "id": b.id,
        "batch_code": b.batch_code,
        "run_id": b.run_id,
        "solution_id": b.solution_id,
        "status": b.status,
        "event_count": (b.state_snapshot or {}).get("event_count", 0),
        "scenario_name": (b.plan_snapshot or {}).get("scenario_name"),
        "created_at": b.created_at.isoformat(timespec="seconds"),
    } for b in db.scalars(stmt)]
