# 实验分析平台（序贯检验 + 决策日志）

面向「每天看结果、中途停流量/改分配」的产品实验场景，从零实现：
**事件接入 → 序贯分析 → 决策日志 → Web 页面**。纯 Python 标准库 + 原生 JS，零第三方依赖。

## 快速开始

```bash
# 1. 生成合成演示数据（两个实验：提前停 + 观察到终点，含迟到/撤销/切流量/指标换版）
python3 -m exp_platform.seed_demo --db demo.db

# 2. 启动服务
python3 -m exp_platform.server --db demo.db --port 8321
# 打开 http://127.0.0.1:8321

# 3. 跑测试（统计有效性蒙特卡洛 + 数据流水线 + HTTP）
python3 -m unittest discover -s tests -v
```

## 它解决什么

| 场景 | 机制 |
|---|---|
| 每天看结果，不能反复用固定样本检验 | mSPRT always-valid p 值 + 置信序列（任意时刻停，总体 α 仍 ≤ 5%）；备选组序贯 OBF/Pocock α 消耗。实测裸 z 每日偷看误报 22%，mSPRT 0.8% |
| 中途停流量 / 改分配 | `allocation_change` 记为 epoch 并上图；观察窗从实验开始起算，**永不重置** |
| 用户/曝光/转化迟到、订单撤销 | 事件 append-only + 摄入水位线 seq；迟到与 tombstone 撤销随水位推进**自动参与重算**；历史 run 不可变 |
| 多任务并行、指标换版、报告确认交错 | 每个 run 绑定（水位 seq、指标版本、方法版本、参数）；决策确认带水位守卫：旧水位的迟到任务 **409 拒绝并留审计**，新决策只能以更新数据 supersede |

## 合成演示的两条路径

`seed_demo.py` 模拟 15 天逐日摄入与每日分析：

- **实验 A（真实 +1.5pp）**：第 6 天 mSPRT 越界 → 确认 `stop_efficacy` → 停止实验流量；
  随后一个迟到 worker 持第 5 天旧水位线的 run 试图确认 `continue` → 被拒绝并记入审计。
- **实验 B（真实 ~0）**：每日分析始终未越界；第 5 天切流量 50/50→65/35（窗口不重置），
  第 8 天指标换版 v1→v2（always-valid 链按新版本重启）；终点补入迟到转化与欺诈撤销后
  重算，确认 `stop_futility`（措辞：未检出 ≥MDE 效应，不宣称无效应）。

## 架构

```
exp_platform/
  statsutil.py   正态分布、mSPRT 混合似然比、置信序列、组序贯边界（独立增量递推积分）
  storage.py     SQLite(WAL) schema：events(append-only) / metric_defs / analysis_runs / decisions
  analysis.py    水位线快照聚合、always-valid 链、组序贯检查、迟到/撤销统计、run 落库
  decisions.py   决策确认（BEGIN IMMEDIATE + 水位守卫）、supersede、拒绝审计
  server.py      http.server JSON API + 静态页
  seed_demo.py   合成数据生成
web/             index.html / app.js(原生 SVG 图表) / style.css
tests/           test_stats.py / test_pipeline.py / test_http.py
docs/methods.md  方法学说明（敏感性 vs 决策延迟、局限与措辞规范）
```

### 数据模型要点

- `events.seq`：全局摄入水位线。查询统一 `seq <= W AND NOT EXISTS(revoke r WHERE
  r.target=e AND r.seq <= W)` → 任意历史水位可复算，与并发摄入无关。
- `analysis_runs`：不可变。`as_of_seq`（数据快照）+ `metric_version` + `method_version`
  + `params` + `result`。
- `decisions`：只增不改（confirmed → superseded）；`decision_attempts` 记录被拒绝的确认。

### API 一览

```
GET  /api/experiments                      列表（含最新 run 摘要与已确认决策）
POST /api/experiments                      创建实验
GET  /api/experiments/{id}                 详情（含分流 epoch、当前水位）
POST /api/experiments/{id}/events          批量摄入事件
POST /api/experiments/{id}/analyses        跑一次分析（可指定 metric_version / as_of_seq）
GET  /api/experiments/{id}/analyses        run 列表
GET  /api/analyses/{run_id}                run 完整结果（含轨迹）
POST /api/experiments/{id}/decisions       确认决策（旧水位 → 409）
GET  /api/experiments/{id}/decisions       决策日志 + 被拒绝记录
POST /api/experiments/{id}/allocations     记录分流变更（epoch）
GET  /api/experiments/{id}/series?metric=  日累计序列（迟到/撤销已重算）
POST /api/metrics/{id}/versions            指标定义换版
```

## 方法版本

`exp_platform.METHOD_VERSION`（当前 `msprt-1.0+gsd-1.0+agg-1.0`）。统计公式或聚合口径
变更时递增；历史 run 通过该字段绑定当时的算法。
