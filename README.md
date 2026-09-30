# Kairos Execution

> Kairos 量化系列的**交易执行**模块 —— 一个**自研、轻量、纯内存模拟**的执行框架（OMS + 执行算法 + TCA）。

`kairos_execution` 提供订单管理系统（状态机 + 事件日志）、模拟券商撮合（成交延迟、参与率部分成交、滑点、限价/止损）、
四种经典执行算法（TWAP / VWAP / Iceberg / Implementation Shortfall），以及交易成本分析（TCA）。
核心代码全部原创，仅依赖 `numpy` 与 `pandas`。

> ⚠️ **本项目是模拟执行 / 算法研究框架，不接任何真实券商通道，不做实盘下单。**
> 所有撮合、成交、账户变动均为离线、确定性的内存模拟，仅用于回测执行算法与教学。

## 特性
- **内存 OMS（`OrderManagementSystem`）**：自研订单状态机 `PENDING → ACCEPTED →(PARTIAL)→ FILLED`（及 `CANCELLED/REJECTED`），
  非法迁移（已成再成交 / 已成再撤单等）抛异常；完整 `event_log`、按 id / 父单 / 活动状态查询。
- **模拟券商（`SimBroker`）**：
  - **成交延迟**：提交后第 `latency` 根 bar 才可成交；
  - **参与率部分成交**：单 bar 成交量上限 = `participation_rate × bar.volume`，超出部分顺延累积；
  - **滑点**：市价/止损成交价按 `slippage_bps` 向不利方向偏移；
  - **限价撮合**：买单 `low ≤ limit`、卖单 `high ≥ limit` 成交；
  - **止损撮合**：价格穿越触发价后转市价（跳空则以开盘价成交）。
- **执行算法（父单 → 子单调度）**：`TWAP`（时间均匀切片）、`VWAP`（按成交量分布切片）、
  `Iceberg`（只显露小量、分批补单）、`ImplementationShortfall`（前重后轻的衰减切片，权衡冲击与风险）。
  数量拆分保证「子单量之和 == 父单量」严格成立。
- **交易成本分析（`tca`）**：到达价 / 决策价、滑点(bp)、成交率、已实现价差、市场冲击（平方根参与率简单估计）、
  实现差额（IS），汇总成 `TCAReport`。
- **可测试、可复现**：自带合成 OHLCV 行情 `sim`，示例与测试全部离线、固定 seed、纯内存。
- **真实数据接入（`realdata`）**：把 kairos-data 仓库的真实 A 股日线 CSV（后复权 hfq）
  转成本包 `Bar` 序列（`load_bars` / `load_ohlcv_frame`，只读、离线），
  配套示例在真实 bar 上对比 TWAP / VWAP / IS 的 TCA。

## 安装
```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .            # 或 pip install numpy pandas
pip install -e ".[dev]"     # 需要跑测试时
```

## 快速开始
### ① OMS：手动驱动订单状态机
```python
from kairos_execution import OrderManagementSystem, Order, OrderType, Side

oms = OrderManagementSystem()
o = Order(order_id="O1", symbol="SIM", side=Side.BUY, qty=10, order_type=OrderType.MARKET)
oms.submit(o)                                  # PENDING
oms.accept("O1")                               # ACCEPTED
oms.partial_fill("O1", 4, 100.0, bar=1)        # PARTIAL
oms.fill("O1", 101.0, bar=2)                   # FILLED（吃掉剩余 6）
print(o.status, o.filled_qty, o.avg_fill_price)
print([e.action for e in oms.event_log])       # submit/accept/partial_fill/fill
```

### ② 执行算法 + 模拟撮合 + TCA
```python
from kairos_execution import (Account, Order, OrderType, Side, SimBroker,
                              VWAP, execute_children, make_ohlcv, to_bars, analyze)

bars = to_bars(make_ohlcv(n_bars=60, seed=9, base_volume=200_000), "SIM")
parent = Order("P", "SIM", Side.BUY, 10000, OrderType.MARKET)
broker = SimBroker(account=Account(cash=1e7), latency=0,
                   participation_rate=0.10, slippage_bps=3.0)
children = VWAP().schedule(parent, bars)        # 按成交量切片
fills = execute_children(broker, children, bars, "SIM")
report = analyze(parent, fills, bars=bars, arrival_price=bars[0].open)
print(report.fill_ratio, report.slippage_bps, report.realized_spread_bps)
```

完整可运行示例见 [`examples/demo.py`](examples/demo.py)（对比 TWAP 与 VWAP 的滑点/成交率）。

## 真实数据执行 TCA
[`examples/real_execution_tca.py`](examples/real_execution_tca.py) 在**真实 A 股日线**上
对比三种执行算法：加载真实 bar（`realdata.load_bars`）→ 取最近 `window` 根为执行窗口 →
构造父单 → 分别用 TWAP / VWAP / ImplementationShortfall 切子单 → 经 `SimBroker`
（latency=1、单子单单 bar 参与率上限 10%、滑点 2bp、佣金万三）在真实 bar 上撮合 →
`analyze` 产出 TCA（到达价/决策价/基准 VWAP/滑点 bp/成交率/实现差额/冲击）→ 对比并落盘。

```bash
python examples/real_execution_tca.py \
    --csv /path/to/kairos-data/data/ashare/sh600519.csv   # 可选 --qty --window --side
```

产物写入 `research/real_execution/`：`REPORT.md`（中文报告：窗口、父单占成交量比、
三算法成交率/均价/滑点bp/相对VWAP/IS 对比表 + 解读）、`tca.json`、`fills_twap.csv`。

```python
from kairos_execution import load_bars, load_ohlcv_frame
bars = load_bars("sh600519.csv", last_n=60)   # 日期升序，index 重排 0..n-1
frame = load_ohlcv_frame("sh600519.csv")      # DatetimeIndex + float64 OHLCV
```

> **数据声明**：真实行情来自本系列 [`kairos-data`](../kairos-data) 仓库
> `data/ashare/<symbol>.csv`（腾讯/新浪公开接口的**后复权 hfq** 日线，本仓库只读引用、
> 不再分发）。hfq 价格水平被整体放大，但收益率正确——本项目 TCA 成本指标一律以
> **基点(bp)** 表示，与价格水平无关；`volume` 单位为手，父单数量与其同口径即可。
> 数据不保证准确、完整或及时，仅用于研究与教学，任何结果**不构成投资建议**。
> 示例本身离线运行、纯内存模拟，不联网、不接任何真实券商。

## API 概览
| 模块 | 关键对象 | 说明 |
|---|---|---|
| `types` | `Side` `OrderType` `OrderStatus` `Order` `Fill` `Position` `Account` `Bar` | 核心数据类型 |
| `oms` | `OrderManagementSystem` `OrderEvent` `IllegalTransitionError` | 订单状态机 + 事件日志 |
| `broker` | `Broker` `SimBroker` `execute_children` `fills_to_frame` | 抽象券商 + 模拟撮合 |
| `algos` | `TWAP` `VWAP` `Iceberg` `ImplementationShortfall` `ExecutionAlgo` | 执行算法（父单→子单） |
| `tca` | `analyze` `TCAReport` `slippage_bps` `fill_ratio` `realized_spread_bps` `market_impact_bps` | 交易成本分析 |
| `sim` | `make_ohlcv` `to_bars` | 合成 OHLCV 行情（离线可复现） |
| `realdata` | `load_bars` `load_ohlcv_frame` | 真实 A 股日线 CSV（hfq，只读）→ Bar 序列 |

## 设计要点
- **关注点分离**：`OMS` 只管订单生命周期与事件日志；`SimBroker` 只做「订单 ↔ 行情」撮合并更新账户；
  `algos` 只负责把父单拆成带时序的子单。三者解耦，状态机可被独立、确定性地测试。
- **严格状态机**：迁移表 `_TRANSITIONS` 显式列出合法路径，任何非法迁移立即抛 `IllegalTransitionError`，把 bug 暴露在最早处。
- **数量守恒**：`_allocate` 用最大余额法（整 lot）或末位补齐（浮点）保证「子单量之和 == 父单量」，避免累计误差。
- **参与率与延迟**：撮合以「到期 bar + 参与率上限」驱动部分成交与顺延，贴近真实执行的容量约束。
- **TCA 方向约定**：成本类指标以基点表示，**正数=不利**；方向由 `Side`（买 +1 / 卖 -1）统一处理。
- **防未来函数**：切片计划只用给定的量价分布；撮合在第 `created_bar + latency` 根 bar 才发生。

## 测试
```bash
make test          # 或 python -m pytest -q
```
覆盖：TWAP 均匀切分与求和、VWAP 正比成交量、Iceberg 显露上限与累计、OMS 正常/非法状态迁移与事件日志、
SimBroker 限价/止损撮合、参与率部分成交累积、成交延迟、滑点、账户更新、TCA 各指标数值，
以及 `realdata` 的真实 OHLCV CSV 解析（升序、字段/类型、脏数据剔除、取最近 N 根，全部离线用 tmp 小 CSV）。

## 项目结构
```
kairos_execution/   核心包（types / oms / broker / algos / tca / sim / realdata）
examples/           可运行示例（demo.py 合成行情；real_execution_tca.py 真实 A 股 bar + TCA）
research/           示例产物（real_execution/ 的 REPORT.md + tca.json + fills_twap.csv 入库）
tests/              pytest 测试
```

## 许可
MIT © 2026 Bruce848647703，见 [LICENSE](LICENSE)。

## 参考与致谢
本项目为**独立原创实现**，未复制任何第三方代码。设计思路受业界通用执行范式
（OMS 订单状态机与事件溯源、TWAP / VWAP / Iceberg / Implementation Shortfall 执行算法、
部分成交与成交延迟的模拟撮合、TCA 的到达价 / 滑点 / 成交率 / 实现差额分解）启发，
在此向开源量化社区致谢。算法与接口均为本仓库自研，且仅用于模拟与研究，不接任何真实券商。
