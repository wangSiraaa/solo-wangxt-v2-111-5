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


# ---------- 离线试验回填账本 ----------
class ReconCreateRequest(BaseModel):
    """从一个已保存方案（run + solution）创建待对账批次。"""

    run_id: int
    solution_id: int
    batch_code: str | None = None
    tolerance_pct: float = Field(default=0.5, ge=0.0, le=100.0)
    abs_tol_t: float = Field(default=0.05, ge=0.0)
    remark: str | None = None


class ReconEventRequest(BaseModel):
    """登记一条实际到料/更正事件（只追加）。

    - plan_item_id 必须引用该批次计划快照中的 blend_item；
    - assay_version_id 必须显式给出（属于该原料的某一化验版），
      不允许缺省后偷偷取“当前最新化验单”；
    - 数量以湿基吨（mass_t_wet，默认）或干基吨（mass_t_dry）报送，
      二者必须且只能给一个，按本批实测含水率 moisture_pct 换算。
    """

    client_event_id: str = Field(min_length=1, max_length=64)
    plan_item_id: int
    assay_version_id: int
    moisture_pct: float = Field(ge=0.0, lt=100.0)
    mass_t_wet: float | None = Field(default=None, ge=0.0)
    mass_t_dry: float | None = Field(default=None, ge=0.0)
    cost_per_t_wet: float | None = Field(default=None, ge=0.0)  # 缺省取原料档案现价
    occurred_at: datetime | None = None
    note: str | None = None

    def quantity(self) -> tuple[str, float]:
        if (self.mass_t_wet is None) == (self.mass_t_dry is None):
            from .chemistry import BlendError
            raise BlendError(
                "BAD_QUANTITY",
                "mass_t_wet 与 mass_t_dry 必须且只能提供一个。",
                {"mass_t_wet": self.mass_t_wet, "mass_t_dry": self.mass_t_dry},
            )
        return ("wet", self.mass_t_wet) if self.mass_t_wet is not None else ("dry", self.mass_t_dry)


class ReconReverseRequest(BaseModel):
    """冲销/更正已登记事件。

    reverse：生成一条数量相反的 reversal 事件，原事件保留留痕；
    correct：先冲销原事件，再追加一条使用新数量/新化验版的 correction 事件。
    """

    client_event_id: str = Field(min_length=1, max_length=64)
    target_event_id: int
    mode: Literal["reverse", "correct"] = "reverse"
    # correct 模式的新值（reverse 模式忽略）
    assay_version_id: int | None = None
    moisture_pct: float | None = Field(default=None, ge=0.0, lt=100.0)
    mass_t_wet: float | None = Field(default=None, ge=0.0)
    mass_t_dry: float | None = Field(default=None, ge=0.0)
    cost_per_t_wet: float | None = Field(default=None, ge=0.0)
    occurred_at: datetime | None = None
    note: str | None = None


class ReconCloseRequest(BaseModel):
    """显式请求关闭批次（数量闭合且无异常才允许转已对账）。"""

    force: bool = False
