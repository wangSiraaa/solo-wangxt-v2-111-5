"""Pydantic 入参/出参模型。"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class AssayVersionOut(BaseModel):
    id: int
    version: str
    lab_report_no: str
    assayed_at: datetime
    basis: str
    composition: dict
    measured_oxides: list


class MaterialOut(BaseModel):
    id: int
    code: str
    name: str
    category: str
    moisture_pct: float
    cost_per_t_wet: float
    availability_t_wet: float | None
    min_share_pct: float
    is_active: bool
    note: str | None = None
    assay_versions: list[AssayVersionOut] = []


class Interval(BaseModel):
    min: float | None = None
    max: float | None = None


class Targets(BaseModel):
    SM: Interval
    IM: Interval
    KH: Interval


class Candidate(BaseModel):
    material_id: int
    assay_version_id: int | None = None  # 默认取最新版


class BlendRequest(BaseModel):
    scenario_name: str = "未命名试算"
    batch_t_dry: float = Field(default=1000.0, gt=0)
    candidates: list[Candidate]
    targets: Targets
    hazard_limits_pct: dict[str, float] = {}  # 干基 %，如 {"Cl": 0.03, "alkali_eq": 1.5}
    modes: list[Literal["min_cost", "max_cheap", "balanced"]] = ["min_cost"]
    cheap_material_id: int | None = None  # max_cheap 模式的“廉价原料”
    save: bool = True


class EvaluateRequest(BaseModel):
    """手工配比试算：直接给干基份额，用于分母为零/缺测报错演示。"""

    scenario_name: str = "手工配比"
    picks: list[Candidate]
    shares_pct_dry: list[float]  # 与 picks 等长；和不强制 100，会归一化


class SolutionItem(BaseModel):
    material_code: str
    material_name: str
    assay_version: str
    lab_report_no: str
    share_pct_dry: float
    mass_t_dry: float
    mass_t_wet: float
    water_t: float
    cost: float
    conversion_trace: dict


class SolutionOut(BaseModel):
    mode: str
    mode_label: str
    success: bool
    total_cost: float | None = None
    cost_per_t_dry: float | None = None
    indicators: dict | None = None
    composition_dry_pct: dict | None = None
    composition_wet_pct: dict | None = None
    water_pct_in_wet_mix: float | None = None
    items: list[SolutionItem] = []
    diagnostic: dict | None = None


class BlendResponse(BaseModel):
    run_id: int | None
    run_code: str
    status: str
    solutions: list[SolutionOut]


# ---------------- 对账批次回填账本 ----------------

class ReconBatchCreate(BaseModel):
    """从已保存方案创建待对账批次（计划快照随之冻结，不再修改）。"""

    run_id: int
    solution_id: int
    note: str | None = None


class ReconEventAppend(BaseModel):
    """追加到料/更正事件。

    event_id 为客户端幂等键：重复回传同一 event_id 只入账一次。
    assay_version_id 必填——必须显式选择化验版本，禁止默认最新化验单。
    """

    event_id: str = Field(min_length=1, max_length=64)
    kind: Literal["receipt", "correction"] = "receipt"
    blend_item_id: int
    assay_version_id: int
    mass_t_wet: float = Field(gt=0)
    reverses_event_id: int | None = None  # kind=correction 时必填
    note: str | None = None


class ReconReverseRequest(BaseModel):
    """冲销已登记事件：生成全额反向事件，原事件保留作审计痕迹。"""

    event_id: str = Field(min_length=1, max_length=64)  # 新冲销事件的幂等键
    note: str | None = None
