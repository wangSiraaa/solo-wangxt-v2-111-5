"""API 端到端：依赖已播种的 PostgreSQL（虚构演示数据）。"""
from fastapi.testclient import TestClient

from app.main import app

c = TestClient(app)
T = {"SM": {"min": 2.4, "max": 2.8}, "IM": {"min": 1.4, "max": 1.8},
     "KH": {"min": 0.88, "max": 0.94}}


def _blend(ids, modes=("min_cost",), hazards=None, cheap=None, save=False, batch=1000):
    body = {
        "scenario_name": "api-test", "batch_t_dry": batch,
        "candidates": [{"material_id": i} for i in ids],
        "targets": T, "hazard_limits_pct": hazards or {},
        "modes": list(modes), "save": save,
    }
    if cheap:
        body["cheap_material_id"] = cheap
    return c.post("/api/blend", json=body)


def test_health_and_materials():
    assert c.get("/api/health").json()["mode"] == "fictional-boundary"
    mats = c.get("/api/materials", params={"active_only": True}).json()
    assert len(mats) == 5


def test_multi_mode_blend_and_trace_fields():
    r = _blend([1, 2, 3, 4, 5], ["min_cost", "max_cheap", "balanced"], cheap=4)
    assert r.status_code == 200
    sols = r.json()["solutions"]
    assert len(sols) == 3
    for s in sols:
        assert s["success"]
        assert s["indicators"]["SM"] and s["items"]
    costs = [s["cost_per_t_dry"] for s in sols]
    assert costs[0] <= costs[2]  # 成本最优不劣于平衡方案


def test_wet_basis_assay_converted():
    # AS01 粉煤灰为湿基化验单（SiO2 湿基 39.36，含水率 18%）；
    # 显式锁定 V2026-09W，避免默认取“最新版”（AS01 现有更新的异常湿基版）。
    as01_assay = next(v for m in c.get("/api/materials").json()
                      if m["code"] == "AS01" for v in m["assay_versions"]
                      if v["version"] == "V2026-09W")["id"]
    body = {
        "scenario_name": "api-test", "batch_t_dry": 1000,
        "candidates": [{"material_id": i} if i != 4
                       else {"material_id": 4, "assay_version_id": as01_assay}
                       for i in [1, 2, 3, 4, 5]],
        "targets": T, "modes": ["max_cheap"], "save": False,
        "cheap_material_id": 4,
    }
    r = c.post("/api/blend", json=body)
    item = next(i for i in r.json()["solutions"][0]["items"]
                if i["material_code"] == "AS01")
    sio2 = next(st for st in item["conversion_trace"]["steps"]
                if st["component"] == "SiO2")
    assert sio2["basis_in"] == "wet" and sio2["basis_out"] == "dry"
    assert abs(sio2["value_out"] - 48.0) < 1e-6


def test_cheap_only_conflict_returns_conflicts():
    r = _blend([1, 3], ["min_cost"])
    sol = r.json()["solutions"][0]
    assert not sol["success"]
    assert sol["diagnostic"]["conflicts"]


def test_missing_assay_http_422():
    r = _blend([1, 2, 7], ["min_cost"])  # SP01 缺测 Fe2O3
    assert r.status_code == 422
    assert r.json()["error_code"] == "MISSING_ASSAY"


def test_zero_denominator_http_422():
    r = c.post("/api/evaluate", json={
        "scenario_name": "零分母",
        "picks": [{"material_id": 8}],
        "shares_pct_dry": [100],
    })
    assert r.status_code == 422
    assert r.json()["error_code"] == "ZERO_DENOMINATOR"


def test_save_run_and_recall():
    r = _blend([1, 2, 3, 4, 5], ["min_cost"], save=True)
    rid = r.json()["run_id"]
    assert rid
    detail = c.get(f"/api/runs/{rid}").json()
    assert detail["run_code"].startswith("RUN-")
    assert detail["solutions"][0]["items"][0]["conversion_trace"]["steps"]
    assert detail["solutions"][0]["items"][0]["raw_assay"]
