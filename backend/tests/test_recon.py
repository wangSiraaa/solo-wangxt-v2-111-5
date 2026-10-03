"""离线试验回填账本验收：事件溯源、幂等、乱序、冲销留痕、异常与持久化。

运行：RAWMIX_DATABASE_URL=sqlite:////tmp/rawmix_test.db（或 PostgreSQL）
"""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import recon
from app.database import SessionLocal
from app.main import app
from app.models import ReconBatch

c = TestClient(app)
T = {"SM": {"min": 2.4, "max": 2.8}, "IM": {"min": 1.4, "max": 1.8},
     "KH": {"min": 0.88, "max": 0.94}}
HAZARDS = {"Cl": 0.05}

# AS01 的“正常湿基化验单”显式版本（该原料另有更新的异常湿基版，
# 计划必须显式锁定正常版，而非默认取最新）。
_AS01_NORMAL_ASSAY = next(
    v["id"] for m in c.get("/api/materials").json() if m["code"] == "AS01"
    for v in m["assay_versions"] if v["version"] == "V2026-09W")
_AS01_ALT_ASSAY = next(
    v["id"] for m in c.get("/api/materials").json() if m["code"] == "AS01"
    for v in m["assay_versions"] if v["version"] == "V2026-10W-ALT")


def _saved_run(cands=(1, 2, 3, 4, 5), hazards=None, modes=("min_cost",),
               cheap=None, targets=None, pin_assays=None):
    # pin_assays: {material_id: assay_version_id}，显式锁定计划化验单；
    # 默认把 AS01 锁到正常湿基版（其最新版是“异常湿基版”）。
    pin = {4: _AS01_NORMAL_ASSAY}
    pin.update(pin_assays or {})
    candidates = []
    for i in cands:
        av = pin.get(i)
        candidates.append({"material_id": i, "assay_version_id": av} if av
                          else {"material_id": i})
    body = {
        "scenario_name": "recon-test", "batch_t_dry": 1000,
        "candidates": candidates,
        "targets": targets or T,
        "hazard_limits_pct": hazards or dict(HAZARDS),
        "modes": list(modes), "save": True,
    }
    if cheap:
        body["cheap_material_id"] = cheap
    r = c.post("/api/blend", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["run_id"]
    return data


def _make_batch(cands=(1, 2, 3, 4, 5), modes=("min_cost",), cheap=None,
                targets=None, hazards=None, pin_assays=None):
    run = _saved_run(cands=cands, modes=modes, cheap=cheap, targets=targets,
                     hazards=hazards, pin_assays=pin_assays)
    return c.get(f"/api/runs/{run['run_id']}").json()


def _new_batch(detail, **over):
    payload = {"run_id": detail["id"],
               "solution_id": _solution_pk(detail),
               "tolerance_pct": 0.5, "abs_tol_t": 0.05}
    payload.update(over)
    r = c.post("/api/recon/batches", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


def _solution_pk(detail):
    # solution 的主键未在 run 详情中直接给出，经列表接口取
    from app import models
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        return db.query(models.BlendSolution).filter_by(run_id=detail["id"]).first().id
    finally:
        db.close()


def _plan_items(batch):
    return batch["plan"]["items"]


def _full_plan_events(batch, tag="rcv"):
    """按计划数量逐条回填，化验版与计划一致（使用各原料当前默认版本）。"""
    out = []
    for i, it in enumerate(_plan_items(batch)):
        out.append({
            "client_event_id": f"{tag}-{i}",
            "plan_item_id": it["plan_item_id"],
            "assay_version_id": it["assay_version_id"],
            "moisture_pct": it["moisture_pct"],
            "mass_t_wet": round(it["mass_t_wet"], 4),
        })
    return out


def _post_events(batch, events):
    for ev in events:
        r = c.post(f"/api/recon/batches/{batch['id']}/events", json=ev)
        assert r.status_code == 200, r.text
    return c.get(f"/api/recon/batches/{batch['id']}").json()


# ---------- 验收①：完整回填与计划一致 → 已对账 ----------
def test_full_match_becomes_reconciled():
    batch = _new_batch(_make_batch())
    assert batch["status"] == "pending"
    batch = _post_events(batch, _full_plan_events(batch))
    assert batch["status"] == "reconciled"
    diff = batch["diff"]
    for it in diff["items"]:
        assert it["closed"]
        assert abs(it["diff"]["mass_t_dry"]) <= it["dry_tolerance_t"] + 1e-9
    assert diff["quantity_closed"] and diff["in_range"]
    assert abs(diff["totals"]["diff"]["mass_t_dry"]) < 0.1
    # 显式关闭成功
    r = c.post(f"/api/recon/batches/{batch['id']}/close", json={})
    assert r.status_code == 200 and r.json()["status"] == "reconciled"


def test_cannot_create_from_failed_solution():
    # LS + SH 两料在该窗口无解 → 失败方案不允许建批次
    run = _saved_run(cands=(1, 3))
    detail = c.get(f"/api/runs/{run['run_id']}").json()
    r = c.post("/api/recon/batches", json={
        "run_id": detail["id"], "solution_id": _solution_pk(detail)})
    assert r.status_code == 422
    assert r.json()["error_code"] == "PLAN_NOT_RECONCILABLE"


# ---------- 验收②：两次乱序回填 + 重复第二次只计一次，闭合前保持待对账 ----------
def test_out_of_order_and_idempotent_replay():
    batch = _new_batch(_make_batch())
    events = _full_plan_events(batch, tag="split")
    assert len(events) >= 4
    first_half = events[::2]   # 第 1、3、5… 条
    second_half = list(reversed(events[1::2]))  # 第 …4、2 条（乱序）

    batch = _post_events(batch, first_half)
    assert batch["status"] == "pending"  # 数量未闭合
    snap_before = batch["diff"]["totals"]["actual"]

    # 重复回传第一批（含与首批完全相同的请求体）——幂等，不重复计数
    batch2 = _post_events(batch, first_half)
    assert batch2["diff"]["totals"]["actual"] == snap_before
    assert batch2["diff"]["event_count"] == len(first_half)
    assert batch2["status"] == "pending"

    # 乱序补齐第二批
    batch = _post_events(batch2, second_half)
    assert batch["status"] == "reconciled"
    assert batch["diff"]["event_count"] == len(events)

    # 乱序到达结果与按序到达一致：另建批次按序回填比较累计
    ordered_detail = _make_batch()
    ordered = _new_batch(ordered_detail)
    ordered = _post_events(ordered, _full_plan_events(ordered, tag="ord"))
    for key in ("mass_t_dry", "mass_t_wet", "water_t", "cost"):
        assert ordered["diff"]["totals"]["actual"][key] == \
            pytest.approx(batch["diff"]["totals"]["actual"][key], abs=1e-6)


def test_idempotency_conflicting_body_rejected():
    batch = _new_batch(_make_batch())
    it = _plan_items(batch)[0]
    ev = {"client_event_id": "dup-x", "plan_item_id": it["plan_item_id"],
          "assay_version_id": it["assay_version_id"],
          "moisture_pct": it["moisture_pct"], "mass_t_wet": 10.0}
    assert c.post(f"/api/recon/batches/{batch['id']}/events", json=ev).status_code == 200
    ev["mass_t_wet"] = 12.0
    r = c.post(f"/api/recon/batches/{batch['id']}/events", json=ev)
    assert r.status_code == 422
    assert r.json()["error_code"] == "IDEMPOTENCY_CONFLICT"


# ---------- 验收③：改用另一份湿基化验单 → KH/有害越界 → 异常，计划不变 ----------
def test_other_wet_assay_marks_abnormal_and_plan_unchanged():
    # max_cheap 锁定粉煤灰 AS01 用量（约 5.5%），计划 Cl≈0.008% < 上限 0.02%。
    # 回填 AS01 时改选另一份湿基化验单（Cl 0.30% 湿基）→ 实际 Cl 越限 → 异常。
    detail = _make_batch(modes=("max_cheap",), cheap=4, hazards={"Cl": 0.02})
    batch = _new_batch(detail)
    as01_items = [i for i in _plan_items(batch) if i["material_code"] == "AS01"]
    assert as01_items and as01_items[0]["share_pct_dry"] > 4.0
    as01_item = as01_items[0]["plan_item_id"]
    alt = _AS01_ALT_ASSAY
    plan_before = batch["plan"]

    events = _full_plan_events(batch, tag="alt")
    for ev in events:
        if ev["plan_item_id"] == as01_item:
            ev["assay_version_id"] = alt
    batch = _post_events(batch, events)

    assert batch["status"] == "abnormal"
    assert any(i["code"] == "INDICATOR_OUT_OF_RANGE"
               for i in batch["diff"]["issues"])
    cl = next(x for x in batch["diff"]["hazards"] if x["hazard"] == "Cl")
    assert cl["out_of_range"]
    assert cl["actual"] is not None and cl["actual"] > 0.02
    assert cl["plan"] < 0.02  # 计划本身合规
    # 事件必须留痕所选化验版，且计划快照原样未动
    as01_events = [e for e in batch["events"]
                   if next(i for i in plan_before["items"]
                           if i["plan_item_id"] == e["plan_item_id"])
                   ["material_code"] == "AS01"]
    assert all(e["assay_snapshot"]["assay_version"] == "V2026-10W-ALT"
               for e in as01_events)
    assert c.get(f"/api/recon/batches/{batch['id']}").json()["plan"] == plan_before
    # 越界批次显式关闭被拒绝
    r = c.post(f"/api/recon/batches/{batch['id']}/close", json={})
    assert r.status_code == 422
    assert r.json()["error_code"] == "INDICATOR_OUT_OF_RANGE"


def test_event_must_reference_plan_item_and_explicit_assay():
    batch = _new_batch(_make_batch())
    it = _plan_items(batch)[0]
    # 化验版不属于该原料 → 拒绝（不能隐式取最新）
    other_assay = next(v for m in c.get("/api/materials").json()
                       for v in m["assay_versions"]
                       if m["id"] != it["material_id"])["id"]
    r = c.post(f"/api/recon/batches/{batch['id']}/events", json={
        "client_event_id": "bad-assay", "plan_item_id": it["plan_item_id"],
        "assay_version_id": other_assay,
        "moisture_pct": 2.0, "mass_t_wet": 5.0})
    assert r.status_code == 422 and r.json()["error_code"] == "ASSAY_NOT_FOUND"
    # 计划项不属于该批次
    r = c.post(f"/api/recon/batches/{batch['id']}/events", json={
        "client_event_id": "bad-item", "plan_item_id": 999999,
        "assay_version_id": it["assay_version_id"],
        "moisture_pct": 2.0, "mass_t_wet": 5.0})
    assert r.status_code == 422 and r.json()["error_code"] == "PLAN_ITEM_NOT_FOUND"
    # 干湿基数量必须且只能给一个
    r = c.post(f"/api/recon/batches/{batch['id']}/events", json={
        "client_event_id": "bad-qty", "plan_item_id": it["plan_item_id"],
        "assay_version_id": it["assay_version_id"], "moisture_pct": 2.0})
    assert r.status_code == 422 and r.json()["error_code"] == "BAD_QUANTITY"


# ---------- 验收④：冲销生成反向事件，重启后累计一致；缺测/零分母不得关闭 ----------
def test_reversal_audit_trail_and_persistence():
    batch = _new_batch(_make_batch())
    it = _plan_items(batch)[0]
    ev = {"client_event_id": "will-reverse",
          "plan_item_id": it["plan_item_id"],
          "assay_version_id": it["assay_version_id"],
          "moisture_pct": it["moisture_pct"],
          "mass_t_wet": round(it["mass_t_wet"], 4)}
    r = c.post(f"/api/recon/batches/{batch['id']}/events", json=ev).json()
    original_id = r["event"]["id"]
    after_receive = r["diff"]["totals"]["actual"]["mass_t_wet"]
    assert after_receive == pytest.approx(it["mass_t_wet"], abs=1e-6)

    r = c.post(f"/api/recon/batches/{batch['id']}/reverse", json={
        "client_event_id": "rev-1", "target_event_id": original_id,
        "note": "磅单录入有误，冲销"})
    assert r.status_code == 200, r.text
    rev = r.json()["events"][0]
    assert rev["type"] == "reversal" and rev["sign"] == -1
    assert rev["reverses_event_id"] == original_id
    # 原事件仍在（只追加留痕）
    detail = c.get(f"/api/recon/batches/{batch['id']}").json()
    assert len(detail["events"]) == 2
    assert detail["events"][0]["id"] == original_id
    assert abs(detail["diff"]["totals"]["actual"]["mass_t_wet"]) < 1e-6
    assert detail["status"] == "pending"  # 冲销后数量不再闭合

    # 重复冲销被拒绝
    r = c.post(f"/api/recon/batches/{batch['id']}/reverse", json={
        "client_event_id": "rev-2", "target_event_id": original_id})
    assert r.status_code == 422 and r.json()["error_code"] == "EVENT_ALREADY_REVERSED"

    # “重启”：丢弃全部进程内状态，直接从持久化事件重放投影
    totals_before = detail["diff"]["totals"]
    db = SessionLocal()
    try:
        rec = db.get(ReconBatch, batch["id"])
        recon.recompute_batch(db, rec)
        db.commit()
    finally:
        db.close()
    detail2 = c.get(f"/api/recon/batches/{batch['id']}").json()
    assert detail2["diff"]["totals"] == totals_before
    assert detail2["status"] == "pending"


def test_correction_applies_reversal_plus_new_receive():
    batch = _new_batch(_make_batch())
    it = _plan_items(batch)[0]
    r = c.post(f"/api/recon/batches/{batch['id']}/events", json={
        "client_event_id": "orig", "plan_item_id": it["plan_item_id"],
        "assay_version_id": it["assay_version_id"],
        "moisture_pct": it["moisture_pct"], "mass_t_wet": 100.0})
    original_id = r.json()["event"]["id"]
    r = c.post(f"/api/recon/batches/{batch['id']}/reverse", json={
        "client_event_id": "corr-1", "target_event_id": original_id,
        "mode": "correct", "assay_version_id": it["assay_version_id"],
        "moisture_pct": it["moisture_pct"], "mass_t_wet": 120.0,
        "note": "实收更正为 120t"})
    assert r.status_code == 200, r.text
    evs = r.json()["events"]
    assert [e["type"] for e in evs] == ["reversal", "correction"]
    assert evs[1]["corrects_event_id"] == original_id
    detail = c.get(f"/api/recon/batches/{batch['id']}").json()
    # +100、冲销 -100、更正 +120 → 净计 120 湿吨（原事件仍保留留痕）
    assert detail["diff"]["totals"]["actual"]["mass_t_wet"] == pytest.approx(
        120.0, abs=1e-6)


def test_missing_assay_blocks_close():
    # SP01(id=7) 化验版缺测 Fe2O3：把它的到料登记到一个 LS 计划项下做演示。
    run = _saved_run(hazards={})
    detail = c.get(f"/api/runs/{run['run_id']}").json()
    batch = _new_batch(detail)
    it = _plan_items(batch)[0]
    sp01 = next(m for m in c.get("/api/materials").json() if m["code"] == "SP01")
    # 注：SP01 不属于该原料，直接构造一条事件验证投影缺测拦截 —— 走 service 层
    from app import models
    from app import chemistry as chem
    db = SessionLocal()
    try:
        rec = db.get(ReconBatch, batch["id"])
        ass = db.get(models.AssayVersion, sp01["assay_versions"][0]["id"])
        mat = db.get(models.Material, it["material_id"])
        payload = recon.make_event_payload(
            it, ass, mat.moisture_pct, 3.0, "wet", it["mass_t_wet"],
            mat.cost_per_t_wet)
        db.add(models.ReconEvent(
            batch_id=rec.id, seq=recon._next_seq(db, rec.id),
            client_event_id="missing-1", type="receive", sign=1,
            plan_item_id=it["plan_item_id"], material_id=it["material_id"],
            assay_version_id=ass.id, **{k: payload[k] for k in
                                        ("moisture_pct", "mass_t_dry",
                                         "mass_t_wet", "water_t", "cost")},
            assay_snapshot=payload["assay_snapshot"],
            occurred_at=datetime.utcnow()))
        db.flush()
        recon.recompute_batch(db, rec)
        db.commit()
        bid = rec.id
    finally:
        db.close()
    # 缺测即硬异常；即便数量恰好闭合也不允许关闭
    detail = c.get(f"/api/recon/batches/{bid}").json()
    assert any(x["code"] == "MISSING_ASSAY" for x in detail["diff"]["issues"])
    r = c.post(f"/api/recon/batches/{bid}/close", json={})
    assert r.status_code == 422 and r.json()["error_code"] == "MISSING_ASSAY"


def test_zero_denominator_and_not_closed_block_close():
    # 未回填任何事件 → 数量未闭合，禁止关闭
    batch = _new_batch(_make_batch())
    r = c.post(f"/api/recon/batches/{batch['id']}/close", json={})
    assert r.status_code == 422
    assert r.json()["error_code"] == "QUANTITY_NOT_CLOSED"


def test_zero_denominator_event_blocks_close():
    # Fe2O3 实测为 0（且四大组分齐全）的化验单 → 合成 IM 分母为零 → 异常，禁止关闭。
    run = _saved_run(hazards={})
    detail = c.get(f"/api/runs/{run['run_id']}").json()
    batch = _new_batch(detail)
    it = _plan_items(batch)[0]
    from app import models
    db = SessionLocal()
    try:
        rec = db.get(ReconBatch, batch["id"])
        # 构造一张“已实测 Fe2O3=0”的完整化验单（挂在不参与常规配料的
        # 演示料 QZ00 下，避免成为 LS01 的“最新化验版”污染其它用例）。
        qz00 = next(m for m in c.get("/api/materials").json() if m["code"] == "QZ00")
        ass = db.scalar(select(models.AssayVersion).where(
            models.AssayVersion.material_id == qz00["id"],
            models.AssayVersion.version == "TEST-ZERO-TMP"))
        if ass is None:
            ass = models.AssayVersion(
                material_id=qz00["id"], version="TEST-ZERO-TMP",
                lab_report_no="LAB-TEST-ZERO", assayed_at=datetime.utcnow(),
                basis="dry",
                composition={"CaO": 0.05, "SiO2": 99.6, "Al2O3": 0.35,
                             "Fe2O3": 0.0, "MgO": 0.0, "SO3": 0.0, "K2O": 0.0,
                             "Na2O": 0.0, "Cl": 0.0, "LOI": 0.0},
                measured_oxides=["CaO", "SiO2", "Al2O3", "Fe2O3",
                                 "MgO", "SO3", "K2O", "Na2O", "Cl", "LOI"])
            db.add(ass); db.flush()
        mat = db.get(models.Material, it["material_id"])
        payload = recon.make_event_payload(
            it, ass, mat.moisture_pct, 0.2, "dry", it["mass_t_dry"],
            mat.cost_per_t_wet)
        db.add(models.ReconEvent(
            batch_id=rec.id, seq=recon._next_seq(db, rec.id),
            client_event_id="zero-1", type="receive", sign=1,
            plan_item_id=it["plan_item_id"], material_id=it["material_id"],
            assay_version_id=ass.id, **{k: payload[k] for k in
                                        ("moisture_pct", "mass_t_dry",
                                         "mass_t_wet", "water_t", "cost")},
            assay_snapshot=payload["assay_snapshot"],
            occurred_at=datetime.utcnow()))
        db.flush()
        snap = recon.recompute_batch(db, rec)
        db.commit()
        bid = rec.id
    finally:
        db.close()
    assert any(x["code"] == "ZERO_DENOMINATOR" for x in snap["issues"])
    r = c.post(f"/api/recon/batches/{bid}/close", json={})
    assert r.status_code == 422 and r.json()["error_code"] == "ZERO_DENOMINATOR"
