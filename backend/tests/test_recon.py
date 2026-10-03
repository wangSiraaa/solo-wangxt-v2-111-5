"""对账批次回填账本验收测试（虚构演示数据，离线工艺研发边界）。

覆盖验收口径：
① 完整回填与计划一致 → 批次转“已对账”；
② 分两次乱序回填 + 重复第二次请求只计一次，数量闭合前保持“待对账”；
③ 改选另一份湿基化验单 → 实际 KH/有害组分越界标“异常”，计划方案不变；
④ 冲销生成可追溯反向事件，重启后累计一致；缺测/零分母不允许关闭。
"""
import random
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app import recon

c = TestClient(app)
T = {"SM": {"min": 2.4, "max": 2.8}, "IM": {"min": 1.4, "max": 1.8},
     "KH": {"min": 0.88, "max": 0.94}}
CL_LIMIT = 0.012


def _assay_ids():
    """按 原料code -> {version: assay_version_id} 查化验版。"""
    mats = c.get("/api/materials").json()
    return {m["code"]: {v["version"]: v["id"] for v in m["assay_versions"]}
            for m in mats}


ASSAY = _assay_ids()


def _saved_solution(hazards=None):
    """保存一次 min_cost 试算，返回 (run_id, solution_id)。"""
    r = c.post("/api/blend", json={
        "scenario_name": "recon-test", "batch_t_dry": 1000.0,
        "candidates": [{"material_id": i} for i in [1, 2, 3, 4, 5]],
        "targets": T, "hazard_limits_pct": hazards or {},
        "modes": ["min_cost"], "save": True,
    })
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    detail = c.get(f"/api/runs/{run_id}").json()
    sol = next(s for s in detail["solutions"] if s["success"])
    return run_id, sol["id"], detail


def _create_batch(run_id, solution_id):
    r = c.post("/api/recon-batches",
               json={"run_id": run_id, "solution_id": solution_id})
    assert r.status_code == 201, r.text
    return r.json()


def _append(batch_id, item, assay_version_id, event_id, mass=None):
    r = c.post(f"/api/recon-batches/{batch_id}/events", json={
        "event_id": event_id, "kind": "receipt",
        "blend_item_id": item["blend_item_id"],
        "assay_version_id": assay_version_id,
        "mass_t_wet": mass if mass is not None else item["mass_t_wet"],
    })
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------- ① 完全一致 → 已对账

def test_full_backfill_matching_plan_reconciles():
    run_id, sol_id, _ = _saved_solution()
    batch = _create_batch(run_id, sol_id)
    assert batch["status"] == "pending"
    plan_items = batch["plan"]["items"]
    assert plan_items

    last = None
    for i, item in enumerate(plan_items):
        last = _append(batch["id"], item, item["assay_version_id"], f"t1-evt-{i}")
    assert last["status"] == "reconciled"

    detail = c.get(f"/api/recon-batches/{batch['id']}").json()
    assert detail["status"] == "reconciled"
    totals = detail["state"]["totals"]
    assert totals["diff"]["mass_t_dry"] == pytest.approx(0, abs=1e-2)
    assert totals["diff"]["mass_t_wet"] == pytest.approx(0, abs=1e-2)
    assert totals["diff"]["water_t"] == pytest.approx(0, abs=1e-2)
    assert totals["diff"]["cost"] == pytest.approx(0, abs=1e-2)
    for it in detail["state"]["items"]:
        assert it["closed"]
        assert it["diff"]["mass_t_dry"] == pytest.approx(0, abs=1e-2)
    # 率值差异为零
    d = detail["state"]["chemistry"]["indicator_diff"]
    assert d["SM"] == pytest.approx(0, abs=1e-3)
    assert d["IM"] == pytest.approx(0, abs=1e-3)
    assert d["KH"] == pytest.approx(0, abs=1e-3)
    assert detail["state"]["violations"] == []


# ------------------------------------------------- ② 乱序 + 重复回传幂等 + 闭合前待对账

def test_out_of_order_and_duplicate_replay_counted_once():
    run_id, sol_id, _ = _saved_solution()
    batch = _create_batch(run_id, sol_id)
    bid = batch["id"]
    items = batch["plan"]["items"]
    assert len(items) >= 2

    # 乱序：从最后一项倒序回填
    ordered = list(reversed(items))
    for i, item in enumerate(ordered):
        resp = _append(bid, item, item["assay_version_id"], f"t2-evt-{i}")
        if i < len(ordered) - 1:
            assert resp["status"] == "pending"  # 数量未闭合保持待对账
    # 重复第二次请求（同一 event_id、同一内容）→ 幂等，只计一次
    dup = _append(bid, ordered[1], ordered[1]["assay_version_id"], "t2-evt-1")
    assert dup["replayed"] is True

    detail = c.get(f"/api/recon-batches/{bid}").json()
    assert detail["state"]["event_count"] == len(items)  # 未重复入账
    assert detail["status"] == "reconciled"
    assert detail["state"]["totals"]["diff"]["mass_t_dry"] == pytest.approx(0, abs=1e-2)

    # 同一 event_id 但内容不同 → 冲突拒绝
    r = c.post(f"/api/recon-batches/{bid}/events", json={
        "event_id": "t2-evt-1", "kind": "receipt",
        "blend_item_id": ordered[1]["blend_item_id"],
        "assay_version_id": ordered[1]["assay_version_id"],
        "mass_t_wet": 1.0,
    })
    assert r.status_code == 422
    assert r.json()["error_code"] == "EVENT_ID_CONFLICT"


def test_cumulative_is_order_invariant():
    """同一事件集合任意乱序，累计结果一致（纯函数重算）。"""
    run_id, sol_id, _ = _saved_solution()
    batch = _create_batch(run_id, sol_id)
    bid = batch["id"]
    for i, item in enumerate(batch["plan"]["items"]):
        _append(bid, item, item["assay_version_id"], f"t2b-evt-{i}",
                mass=item["mass_t_wet"] / 2.0)  # 半量回填 → 保持待对账
    detail = c.get(f"/api/recon-batches/{bid}").json()
    assert detail["status"] == "pending"

    db = SessionLocal()
    try:
        events = [SimpleNamespace(blend_item_id=e["blend_item_id"], delta=e["delta"])
                  for e in detail["events"]]
        base = recon.compute_state(detail["plan"], events)
        for seed in (1, 2, 3):
            shuffled = list(events)
            random.Random(seed).shuffle(shuffled)
            again = recon.compute_state(detail["plan"], shuffled)
            assert again["totals"] == base["totals"]
            assert again["status"] == base["status"]
            assert again["chemistry"]["actual_indicators"] == \
                base["chemistry"]["actual_indicators"]
    finally:
        db.close()


# ------------------------------------------- ③ 改选湿基化验单 → 异常，计划不变

def test_alternative_wet_assay_out_of_bounds_marks_exception():
    run_id, sol_id, run_before = _saved_solution(hazards={"Cl": CL_LIMIT})
    batch = _create_batch(run_id, sol_id)
    bid = batch["id"]
    items = batch["plan"]["items"]

    wet_sh01 = ASSAY["SH01"]["V2026-08W"]  # 另一份湿基化验单（Cl 显著偏高）
    for i, item in enumerate(items):
        assay = wet_sh01 if item["material_code"] == "SH01" \
            else item["assay_version_id"]
        _append(bid, item, assay, f"t3-evt-{i}")

    detail = c.get(f"/api/recon-batches/{bid}").json()
    assert detail["status"] == "exception"
    kinds = {v["kind"] for v in detail["state"]["violations"]}
    assert "hazard_Cl" in kinds            # 有害组分越界
    assert "KH" in kinds                   # 实际 KH 越出窗口
    # 数量虽已闭合，但越界优先 → 不允许转为已对账
    assert detail["state"]["all_closed"]

    # 计划方案不变：历史试算记录与批次计划快照均未改
    run_after = c.get(f"/api/runs/{run_id}").json()
    assert run_after["solutions"][0]["payload"] == \
        run_before["solutions"][0]["payload"]
    sh_plan = next(i for i in detail["plan"]["items"]
                   if i["material_code"] == "SH01")
    assert sh_plan["assay_version"] == "V2026-09"  # 计划仍冻结在原化验版
    # 实际事件留痕：明确选择了湿基化验单
    sh_events = [e for e in detail["events"]
                 if e["assay_snapshot"]["assay_version"] == "V2026-08W"]
    assert len(sh_events) == 1
    assert sh_events[0]["assay_snapshot"]["basis"] == "wet"
    assert sh_events[0]["assay_snapshot"]["dry_factor"] > 1.0


# ------------------------- ④ 冲销留痕 + 重启一致 + 缺测/零分母不得关闭

def test_reversal_audit_trail_and_restart_consistency():
    run_id, sol_id, _ = _saved_solution()
    batch = _create_batch(run_id, sol_id)
    bid = batch["id"]
    items = batch["plan"]["items"]

    # 全部回填 → 已对账
    for i, item in enumerate(items):
        _append(bid, item, item["assay_version_id"], f"t4-evt-{i}")
    assert c.get(f"/api/recon-batches/{bid}").json()["status"] == "reconciled"

    # 冲销第一笔到料 → 反向事件留痕，累计回落，状态退回待对账
    detail = c.get(f"/api/recon-batches/{bid}").json()
    target = next(e for e in detail["events"] if e["event_id"] == "t4-evt-0")
    r = c.post(f"/api/recon-batches/{bid}/events/{target['id']}/reverse",
               json={"event_id": "t4-rev-0", "note": "到料单录入错误"})
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "pending"

    detail = c.get(f"/api/recon-batches/{bid}").json()
    rev = next(e for e in detail["events"] if e["event_id"] == "t4-rev-0")
    assert rev["kind"] == "reversal"
    assert rev["reverses_event_id"] == target["id"]      # 可追溯
    assert rev["delta"]["mass_t_dry"] == pytest.approx(-target["delta"]["mass_t_dry"])
    orig = next(e for e in detail["events"] if e["id"] == target["id"])
    assert orig["reversed_by_event_id"] == "t4-rev-0"    # 原事件保留且标注
    it0 = next(i for i in detail["state"]["items"]
               if i["blend_item_id"] == target["blend_item_id"])
    assert it0["actual"]["mass_t_dry"] == pytest.approx(0, abs=1e-6)
    assert not it0["closed"]

    # 重复冲销同一事件 → 拒绝；同一冲销请求重发 → 幂等
    r = c.post(f"/api/recon-batches/{bid}/events/{target['id']}/reverse",
               json={"event_id": "t4-rev-0b"})
    assert r.status_code == 422
    assert r.json()["error_code"] == "EVENT_ALREADY_REVERSED"
    r = c.post(f"/api/recon-batches/{bid}/events/{target['id']}/reverse",
               json={"event_id": "t4-rev-0"})
    assert r.status_code == 201 and r.json()["replayed"] is True

    # 重启（全新客户端/会话）后累计结果一致
    snapshot_before = detail["state"]
    fresh = TestClient(app)
    detail2 = fresh.get(f"/api/recon-batches/{bid}").json()
    assert detail2["state"]["totals"] == snapshot_before["totals"]
    assert detail2["state"]["status"] == snapshot_before["status"]
    assert detail2["state"]["chemistry"]["actual_indicators"] == \
        snapshot_before["chemistry"]["actual_indicators"]

    # 更正：以反向+新值重新入账，原事件仍在
    other = next(e for e in detail2["events"] if e["event_id"] == "t4-evt-1")
    item1 = next(i for i in detail2["plan"]["items"]
                 if i["blend_item_id"] == other["blend_item_id"])
    r = fresh.post(f"/api/recon-batches/{bid}/events", json={
        "event_id": "t4-corr-1", "kind": "correction",
        "blend_item_id": other["blend_item_id"],
        "assay_version_id": other["assay_version_id"],
        "mass_t_wet": item1["mass_t_wet"] / 2.0,
        "reverses_event_id": other["id"],
        "note": "到料量更正为一半",
    })
    assert r.status_code == 201, r.text
    detail3 = fresh.get(f"/api/recon-batches/{bid}").json()
    kinds = [e["kind"] for e in detail3["events"]]
    assert kinds.count("receipt") == len(items)          # 原始到料全部保留
    assert "correction" in kinds
    it1 = next(i for i in detail3["state"]["items"]
               if i["blend_item_id"] == other["blend_item_id"])
    assert it1["actual"]["mass_t_wet"] == pytest.approx(
        item1["mass_t_wet"] / 2.0, abs=1e-3)


def test_missing_assay_event_rejected_and_batch_cannot_close():
    run_id, sol_id, _ = _saved_solution()
    batch = _create_batch(run_id, sol_id)
    bid = batch["id"]
    items = batch["plan"]["items"]

    # SH01 改选缺测 Fe2O3 的化验版 → 422 拒绝入账，绝不以零含量兜底
    sh = next(i for i in items if i["material_code"] == "SH01")
    r = c.post(f"/api/recon-batches/{bid}/events", json={
        "event_id": "t5-bad-1", "kind": "receipt",
        "blend_item_id": sh["blend_item_id"],
        "assay_version_id": ASSAY["SH01"]["V2026-07M"],
        "mass_t_wet": sh["mass_t_wet"],
    })
    assert r.status_code == 422
    assert r.json()["error_code"] == "MISSING_ASSAY"

    # 其余原料全部回填后，批次仍因 SH01 数量未闭合而无法关闭
    for i, item in enumerate(items):
        if item["material_code"] == "SH01":
            continue
        _append(bid, item, item["assay_version_id"], f"t5-evt-{i}")
    detail = c.get(f"/api/recon-batches/{bid}").json()
    assert detail["status"] == "pending"
    assert detail["state"]["event_count"] == len(items) - 1  # 缺测事件未入账


def test_zero_denominator_blocks_close():
    """累计合成出现零分母（如 Fe2O3 全为实测 0）→ 不允许对账关闭。"""
    plan = {
        "items": [{
            "blend_item_id": 1, "material_code": "QZ00", "material_name": "零铁石英",
            "mass_t_dry": 100.0, "mass_t_wet": 100.2, "water_t": 0.2, "cost": 11000.0,
        }],
        "totals": {"mass_t_dry": 100.0, "mass_t_wet": 100.2,
                   "water_t": 0.2, "cost": 11000.0},
        "targets": T, "hazard_limits_pct": {},
        "indicators": {"SM": 3.0, "IM": 1.5, "KH": 0.9},
        "hazards": {},
    }
    ev = SimpleNamespace(blend_item_id=1, delta={
        "mass_t_wet": 100.2, "mass_t_dry": 100.0, "water_t": 0.2, "cost": 11000.0,
        "components_t": {"CaO": 0.05, "SiO2": 99.6, "Al2O3": 0.35,
                         "Fe2O3": 0.0, "LOI": 0.0},  # Fe2O3 实测 0 → IM 分母为零
    })
    state = recon.compute_state(plan, [ev])
    assert state["status"] == "exception"
    assert state["status"] != "reconciled"
    assert state["chemistry"]["calc_errors"][0]["code"] == "ZERO_DENOMINATOR"
    assert state["all_closed"]  # 数量闭合也不能关闭：零分母优先


# ---------------------------------------------------------------- 杂项校验

def test_create_batch_guards():
    run_id, sol_id, _ = _saved_solution()
    # 不存在的试算/方案
    r = c.post("/api/recon-batches", json={"run_id": 99999, "solution_id": 1})
    assert r.status_code == 422 and r.json()["error_code"] == "RUN_NOT_FOUND"
    # 批次不存在
    assert c.get("/api/recon-batches/99999").status_code == 404
    # 化验版不属于该原料 → 拒绝（不许悄悄换化验单）
    batch = _create_batch(run_id, sol_id)
    item = batch["plan"]["items"][0]
    r = c.post(f"/api/recon-batches/{batch['id']}/events", json={
        "event_id": "t6-x", "kind": "receipt",
        "blend_item_id": item["blend_item_id"],
        "assay_version_id": ASSAY["IR01"]["V2026-09"],
        "mass_t_wet": 1.0,
    })
    assert r.status_code == 422
    assert r.json()["error_code"] == "ASSAY_MATERIAL_MISMATCH"
