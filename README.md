# 离线原料配比试算台（虚构工艺边界 · 工艺研发用）

> ⚠️ **边界声明**：本应用使用的原料名称、化验单数值、成本、可用量与率值窗口均为**虚构演示数据**，
> 仅用于工艺研发离线比较“原料成本 ↔ 生料化学指标”的取舍。
> 应用不连接任何生产控制系统，**不向真实生产设备下发指令**。

## 技术栈

| 层 | 技术 | 职责 |
|---|---|---|
| 前端 | Angular 18（standalone 组件，纯 CSS 堆叠条） | 氧化物来源/配比比例展示、试算交互、方案对比、化验追溯、对账批次回填 |
| 后端 | FastAPI + Pydantic | REST API、干湿基换算、错误码、只追加账本、静态托管 |
| 优化 | SciPy `linprog`（HiGHS） | 线性规划：成本最优 / 廉价料最大 / 率值居中 |
| 存储 | PostgreSQL 15 | 原料、**多版化验单**、试算批次、方案、逐原料换算留痕、**对账事件与差异快照** |

## 计算口径

1. **先质量守恒合成，再算率值**（不把率值当输入去反推成分）。
   干基份额 x_i（Σx_i=1）：`合成干基% = Σ x_i × 原料干基%`。
2. 率值（分母为零即报错，见下）：
   - 硅率 `SM = SiO2 / (Al2O3 + Fe2O3)`
   - 铝率 `IM = Al2O3 / Fe2O3`
   - 石灰饱和系数 `KH = (CaO − 1.65·Al2O3 − 0.35·Fe2O3) / (2.8·SiO2)`
3. **干湿基**：化验按 `dry`（干基）或 `wet`（收到基）登记；
   含水率 w 时 `干基% = 湿基% /(1−w)`，自由水不并入 LOI；
   质量换算 `湿料t = 干料t /(1−w)`，成本按湿料吨价结算。
4. 碱当量 `Na2O + 0.658·K2O`。

### 硬性错误规则（不以零含量兜底）

- **缺测**：候选原料的必测组分（CaO/SiO2/Al2O3/Fe2O3，及被设上限的有害组分）
  不在 `measured_oxides` 中 → HTTP 422 `MISSING_ASSAY`，返回原料/化验版/单号/缺测项；
- **分母为零**：Fe2O₃、SiO₂ 等为 0 导致 IM/SM/KH 无定义 → HTTP 422 `ZERO_DENOMINATOR`；
- “已实测为 0”（演示料 QZ00）与“未测/缺测”（演示料 SP01）严格区分。

### 约束

- 原料**最低掺量**（干基 %，档案字段）、**湿基可用量**（换算成干基份额上限）；
- **有害组分干基上限**（Cl、碱当量等，可扩展）；
- 率值区间经线性化进入 LP（如 SM≤hi ⇔ `Σ(SiO2−hi(Al2O3+Fe2O3))x ≤ 0`）。

### 求解模式与无解诊断

- `min_cost`：最小元/吨干生料；
- `max_cheap`：两阶段 LP——先最大化指定廉价料份额，再锁定份额最小化成本打破平局；
- `balanced`：率值对区间中点的绝对偏差最小（线性化），轻微成本偏好做次序裁决；
- **求解失败**：对全部不等式做“最小违约松弛”模型，列出仍被突破的冲突约束、
  限值、最小违约解达到值与缺口；最低掺量之和 >100% 另有算术预检 `MIN_SHARE_OVERFLOW`。

## 对账批次回填账本（只追加事件）

研发试验完成后，实际到料与化验结果同原配比方案对账，**历史计划行永不被回填修改**：

1. **创建批次**：`POST /api/recon-batches` 从一个已保存方案冻结计划快照
   （逐原料计划干/湿/水/成本、计划化验版、换算留痕、率值窗口与有害组分上限），
   批次初始为 **待对账**；
2. **只追加事件**：到料 `receipt` / 冲销 `reversal` / 更正 `correction`
   全部只追加。每条事件必须显式引用 **计划原料项 `blend_item_id`** 与
   **化验版 `assay_version_id`**——禁止回退到原料“当前最新”化验单；
   - 冲销/更正生成**全额反向事件**抵销原事件净贡献，原事件保留可审计；
     同一事件只能被抵销一次，冲销事件不可再被冲销；
   - 更正 = 反向抵销 + 以新数量/新化验版重新入账（一条事件内完成）；
3. **幂等与乱序**：`event_id` 为客户端幂等键，`(batch_id, event_id)` 唯一，
   重复回传原样重放只计一次（内容不同则 422 `EVENT_ID_CONFLICT`）；
   累计量 = Σ 事件净贡献（纯加法），乱序到达/服务重启后重算结果一致；
4. **差异与状态**：按累计干/湿质量、水量、成本、率值（SM/IM/KH）与有害组分
   计算计划 vs 实际差异；逐原料干基数量闭合（容差 max(0.01t, 0.1%)）后：
   - 率值越出目标窗口或有害组分超限 → **异常**（越界在闭合前仅预警）；
   - 全部闭合且无越界/计算错误 → **已对账**；
   - 缺测化验 422 `MISSING_ASSAY` 拒绝入账、零分母计入 `calc_errors`，
     两者都**阻止批次关闭**；
5. **持久化**：事件、批次状态与最新差异快照（`recon_batch.state_snapshot`）
   全部落库；详情接口每次以事件流重算并刷新快照。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/recon-batches` | 从已保存方案创建待对账批次（冻结计划快照） |
| GET | `/api/recon-batches?run_id=` | 批次列表 |
| GET | `/api/recon-batches/{id}` | 批次详情：计划/实际/差异 + 全部事件 |
| POST | `/api/recon-batches/{id}/events` | 追加到料/更正事件（`event_id` 幂等） |
| POST | `/api/recon-batches/{id}/events/{event_pk}/reverse` | 冲销已登记事件（反向留痕） |

## 目录

```
backend/
  app/
    main.py        FastAPI 路由 + 错误处理 + SPA 托管
    chemistry.py   干湿基换算 / 质量守恒 / SM/IM/KH / 缺测与零分母异常
    optimizer.py   SciPy HiGHS LP、多模式、冲突诊断
    recon.py       对账账本：计划快照 / 只追加事件 / 累计差异 / 状态机
    models.py      SQLAlchemy：material / assay_version / blend_run / solution / item
                   / recon_batch / recon_event
    crud.py        持久化与历史回看
    schemas.py     Pydantic 模型
    seed.py        虚构演示数据（含湿基化验单、缺测/零分母演示料、
                   对账演示用高 Cl 湿基化验单 SH01/V2026-08W）
  tests/           29 个 pytest（换算/守恒/报错/求解/API/追溯/对账账本验收）
  scripts/         pg_start / pg_stop / seed / serve
frontend/
  src/app/
    components/    materials / blend / solution-card / history(含对账批次) / stack-bar
    services/api.service.ts
    models/models.ts
```

## 启动（本机用户态，无需 root/docker）

PostgreSQL 15 以 deb 解包方式安装在 `~/.local/pgsql`，数据目录 `~/.local/pgdata`，
端口 **55432**，库名 **rawmix**，连接串：
`postgresql+psycopg2://mixapp@127.0.0.1:55432/rawmix`（可用 `RAWMIX_DATABASE_URL` 覆盖）。

```bash
# 1) 启动数据库（首次自动 initdb + 建库）
backend/scripts/pg_start.sh
# 2) 写入虚构演示数据
backend/scripts/seed.sh
# 3) 启动 API + 已构建前端（http://127.0.0.1:8000）
backend/scripts/serve.sh
```

前端开发模式（热更新，代理 /api → :8000）：

```bash
cd frontend && ./dev.sh        # http://127.0.0.1:4200
# 生产构建（输出到 backend/static，由 FastAPI 托管）：
cd frontend && ./node_modules/.bin/ng build frontend
```

Python 依赖：`pip install -r backend/requirements.txt`（本机装于用户 site-packages）。

## 前端四个标签页

1. **原料与化验**：全部原料/多版化验单、干湿基标记、缺测红格、干基换算预览；
2. **配比试算与方案对比**：候选/化验版/率值窗口/有害上限/模式选择，四个快速场景：
   - 基准三方案对比（含水率差异：粉煤灰 18% 湿基化验单 → 采购湿料量与留痕）；
   - 廉价原料（页岩）致 IM/KH 超限 → 失败 + 冲突项；
   - 碱当量上限收紧（0.40%）→ 有害组分冲突与突破量；
   - 5000 t 大批量 → 湿基可用量与 KH 同时冲突；
3. **手工配比**：一键装入“100% 零铁石英（IM 分母为零）”和“缺测矿样（MISSING_ASSAY）”；
4. **历史追溯**：每个方案可追到批次号、原始化验版本/单号、原始 wet/dry 报送值、
   逐组分湿→干公式、干/湿料质量、水量与成本算式；
5. **对账批次**（历史页内）：从可行方案一键创建待对账批次；逐原料
   计划/实际/差异对照与原始换算依据；到料/冲销/更正事件流；
   率值与有害组分计划 vs 实际对照、越界与计算错误提示。

## API 摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/materials?active_only=` | 原料与全部化验版本 |
| POST | `/api/blend` | 试算（多模式、约束、可入库） |
| POST | `/api/evaluate` | 手工份额合成 + 率值（错误演示） |
| GET | `/api/runs` `/api/runs/{id}` | 历史批次与完整追溯 |
| POST | `/api/recon-batches` | 创建对账批次（冻结计划快照） |
| GET | `/api/recon-batches` `/api/recon-batches/{id}` | 批次列表 / 详情（计划/实际/差异 + 事件流） |
| POST | `/api/recon-batches/{id}/events` | 追加到料/更正（幂等） |
| POST | `/api/recon-batches/{id}/events/{pk}/reverse` | 冲销（反向事件留痕） |
| GET | `/api/health` | 健康检查（含 fictional-boundary 标记） |

错误响应体：`{ "error_code": "MISSING_ASSAY|ZERO_DENOMINATOR|EVENT_ID_CONFLICT|...", "message": ..., "details": ... }`。

## 测试

```bash
cd backend && python3 -m pytest tests/ -q
# 29 passed
```

对账账本验收（`tests/test_recon.py`）：
① 完整回填与计划一致 → 批次转“已对账”，质量/水量/成本/率值差异归零；
② 乱序分笔回填 + 重复回传只计一次，数量闭合前保持“待对账”；
③ 改选高 Cl 湿基化验单（SH01/V2026-08W）→ 实际 Cl/KH 越界标“异常”，
   历史方案与计划快照不变；
④ 冲销生成可追溯反向事件，重启后累计一致；缺测化验 422 拒绝入账、
   零分母合成一律阻止批次关闭。
