"""SQLAlchemy 模型：原料、化验版本、试算方案、对账批次账本。

化验成分按版本保存（assay_version），方案结果通过 blend_item.assay_version_id
与 assay_composition 回指具体化验单，保证结果可追溯到原始化验版与换算过程。

对账批次（recon_batch）从已保存方案冻结计划快照；实际到料/冲销/更正
全部作为只追加事件（recon_event）入账，批次状态与差异快照由事件流重算，
历史计划行永不被回填修改。
"""
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class Material(Base):
    __tablename__ = "material"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(64))
    category: Mapped[str] = mapped_column(String(32))  # 钙质/硅铝质/铁质/校正料/演示用
    moisture_pct: Mapped[float] = mapped_column(Float, default=0.0)  # 收到基含水率 %
    cost_per_t_wet: Mapped[float] = mapped_column(Float)  # 元/吨（收到基/湿基）
    availability_t_wet: Mapped[float | None] = mapped_column(Float, nullable=True)  # 可用量，湿基吨；NULL 不限
    min_share_pct: Mapped[float] = mapped_column(Float, default=0.0)  # 最低掺量（干基份额，%）
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    assay_versions: Mapped[list["AssayVersion"]] = relationship(
        back_populates="material", cascade="all, delete-orphan"
    )


class AssayVersion(Base):
    """原料化验单（一个原料可有多个化验版）。

    composition 形如 {"CaO": 78.2, "SiO2": 4.1, ..., "LOI": 35.0}，
    basis = dry 表示干基化验值（占干样 %），basis = wet 表示收到基化验值（占湿样 %）。
    缺测氧化物应不出现在 dict 中（由 measured_oxides 或 NULL 区分），
    禁止用 0 代替“未测”。
    """

    __tablename__ = "assay_version"
    __table_args__ = (UniqueConstraint("material_id", "version", name="uq_assay_material_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    material_id: Mapped[int] = mapped_column(ForeignKey("material.id"))
    version: Mapped[str] = mapped_column(String(32))
    lab_report_no: Mapped[str] = mapped_column(String(64))
    assayed_at: Mapped[datetime] = mapped_column(DateTime)
    basis: Mapped[str] = mapped_column(String(8), default="dry")  # dry / wet
    composition: Mapped[dict] = mapped_column(JSON)
    measured_oxides: Mapped[list] = mapped_column(JSON)  # 实际测定项目，如 ["CaO","SiO2"]
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    material: Mapped["Material"] = relationship(back_populates="assay_versions")


class BlendRun(Base):
    """一次试算（可含多个方案：成本最优/廉价料最多/平衡方案）。"""

    __tablename__ = "blend_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_code: Mapped[str] = mapped_column(String(64), unique=True)
    scenario_name: Mapped[str] = mapped_column(String(128))
    batch_t_dry: Mapped[float] = mapped_column(Float)
    target: Mapped[dict] = mapped_column(JSON)
    constraint_set: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16))  # feasible / infeasible / error
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)

    items: Mapped[list["BlendItem"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    solutions: Mapped[list["BlendSolution"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class BlendSolution(Base):
    __tablename__ = "blend_solution"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("blend_run.id"))
    mode: Mapped[str] = mapped_column(String(32))  # min_cost / max_cheap / balanced
    success: Mapped[bool] = mapped_column(Boolean)
    total_cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    indicators: Mapped[dict] = mapped_column(JSON)  # SM/IM/KH + 合成成分 + 有害组分
    diagnostic: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # 冲突项诊断
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    run: Mapped["BlendRun"] = relationship(back_populates="solutions")
    items: Mapped[list["BlendItem"]] = relationship(
        primaryjoin="BlendSolution.id == BlendItem.solution_id",
        viewonly=True,
    )


class BlendItem(Base):
    """某方案下某原料的配比与干湿基换算过程。"""

    __tablename__ = "blend_item"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("blend_run.id"))
    solution_id: Mapped[int] = mapped_column(ForeignKey("blend_solution.id"))
    material_id: Mapped[int] = mapped_column(ForeignKey("material.id"))
    assay_version_id: Mapped[int] = mapped_column(ForeignKey("assay_version.id"))
    share_pct_dry: Mapped[float] = mapped_column(Float)  # 干基份额 %
    mass_t_dry: Mapped[float] = mapped_column(Float)
    mass_t_wet: Mapped[float] = mapped_column(Float)
    water_t: Mapped[float] = mapped_column(Float)
    cost: Mapped[float] = mapped_column(Float)
    # 换算留痕：湿基->干基/干基->湿基每个氧化物的完整过程
    conversion_trace: Mapped[dict] = mapped_column(JSON)
    assay_composition_snapshot: Mapped[dict] = mapped_column(JSON)  # 原始化验单快照

    run: Mapped["BlendRun"] = relationship(back_populates="items")


class ReconBatch(Base):
    """离线试验批次回填账本（从已保存方案冻结计划快照）。

    status 由事件流重算得出：
      pending    待对账（数量未闭合，且无越界/计算错误）
      reconciled 已对账（逐原料干基数量闭合，且率值/有害组分均在窗口内）
      exception  异常（实际率值越出目标窗口、有害组分超限或零分母等）
    plan_snapshot 创建后不再修改；state_snapshot 为最新累计与差异快照。
    """

    __tablename__ = "recon_batch"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_code: Mapped[str] = mapped_column(String(64), unique=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("blend_run.id"))
    solution_id: Mapped[int] = mapped_column(ForeignKey("blend_solution.id"))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    plan_snapshot: Mapped[dict] = mapped_column(JSON)   # 冻结的计划（含计划化验版）
    state_snapshot: Mapped[dict] = mapped_column(JSON)  # 最新累计/差异快照
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    events: Mapped[list["ReconEvent"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan",
        order_by="ReconEvent.seq",
    )


class ReconEvent(Base):
    """只追加账本事件：到料(receipt)/冲销(reversal)/更正(correction)。

    - event_id 为客户端幂等键，(batch_id, event_id) 唯一：重复回传只计一次；
    - 每条事件必须显式引用计划原料项 blend_item_id 与化验版 assay_version_id，
      绝不回退到原料“当前最新”化验单；
    - reversal 全额抵销被引用事件的净贡献；correction 抵销被引用事件并
      以新数量/新化验版重新入账；被抵销的事件行本身永不修改；
    - delta 为该事件对累计量的净贡献（带符号），累计 = Σ delta，
      与事件到达顺序无关，重启后按事件流重算结果一致。
    """

    __tablename__ = "recon_event"
    __table_args__ = (
        UniqueConstraint("batch_id", "event_id", name="uq_recon_event_client_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("recon_batch.id"))
    event_id: Mapped[str] = mapped_column(String(64))  # 客户端幂等键
    seq: Mapped[int] = mapped_column(Integer)          # 批次内到达序号（仅展示排序用）
    kind: Mapped[str] = mapped_column(String(16))      # receipt / reversal / correction
    blend_item_id: Mapped[int] = mapped_column(ForeignKey("blend_item.id"))
    material_id: Mapped[int] = mapped_column(ForeignKey("material.id"))
    assay_version_id: Mapped[int] = mapped_column(ForeignKey("assay_version.id"))
    mass_t_wet: Mapped[float] = mapped_column(Float)   # 本次到料湿基吨（reversal 为被冲销量）
    moisture_pct: Mapped[float] = mapped_column(Float)
    cost_per_t_wet: Mapped[float] = mapped_column(Float)
    reverses_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("recon_event.id"), nullable=True
    )
    assay_snapshot: Mapped[dict] = mapped_column(JSON)  # 化验版/单号/基准/换算系数留痕
    delta: Mapped[dict] = mapped_column(JSON)           # 净贡献：干湿质量/水/成本/组分吨
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    batch: Mapped["ReconBatch"] = relationship(back_populates="events")
