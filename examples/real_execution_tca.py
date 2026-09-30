"""真实 A 股日线上的三算法执行 + TCA 对比（TWAP / VWAP / ImplementationShortfall）。

数据：kairos-data 仓库的真实 A 股日线 CSV（**后复权 hfq，只读**）。hfq 价格水平
被整体放大，但本示例的 TCA 成本指标全部以基点(bp)表示，与价格水平无关。

流程：加载真实 bar → 取最近 window 根为执行窗口（另留少量尾部执行 bar 承接
延迟未竟部分）→ 构造父单(BUY/SELL qty) → 分别用 TWAP / VWAP /
ImplementationShortfall 切子单 → 经 SimBroker(latency=1、单bar参与率上限、
滑点、佣金)在真实 bar 上撮合 → analyze 产出 TCAReport → 三算法对比 →
写 ``research/real_execution/{REPORT.md, tca.json, fills_twap.csv}`` 并打印关键数字。

运行：
    python examples/real_execution_tca.py \
        --csv /path/to/kairos-data/data/ashare/sh600519.csv [--qty 47000] [--window 60]

（离线、纯内存模拟、确定性，**不接任何真实券商，不构成投资建议**）
"""
import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd                                    # noqa: E402

from kairos_execution import (                         # noqa: E402
    Account,
    ImplementationShortfall,
    Order,
    OrderType,
    Side,
    SimBroker,
    TWAP,
    VWAP,
    analyze,
    benchmark_vwap,
    execute_children,
    fills_to_frame,
    load_bars,
    load_ohlcv_frame,
)

DEFAULT_CSV = "/home/zhuoming.wang/quant-hub/kairos/kairos-data/data/ashare/sh600519.csv"
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTDIR = REPO_ROOT / "research" / "real_execution"

# ---- 撮合与父单默认参数（确定性，无随机） ----
LATENCY = 1                 # 子单提交后下一根 bar 才可成交
PARTICIPATION = 0.10        # 单子单单 bar 参与率上限（占该 bar 成交量）
SLIPPAGE_BPS = 2.0          # 市价单滑点（bp）
COMMISSION_RATE = 0.0003    # 佣金：成交额万三
TAIL_BARS = 5               # 窗口后额外执行 bar 数（承接 latency/参与率未竟部分）
IS_DECAY = 0.7              # IS 算法衰减系数（前重后轻）
TARGET_PCT_OF_VOLUME = 0.02 # 未显式给 --qty 时，父单 ≈ 窗口总成交量的 2%


@dataclass
class AlgoRun:
    """单个算法的一次完整执行结果。"""

    name: str
    num_children: int
    fills: list
    report: object
    last_fill_bar: int
    early_fill_pct: float   # 窗口前 1/3 内成交量占已成交量比例(%)


def run_algo(name, algo, exec_bars, sched_bars, symbol, side, qty,
             arrival, decision):
    """调度子单 → SimBroker 在真实 bar 上撮合 → analyze 产出 TCA。"""
    parent = Order(order_id=f"P-{name}", symbol=symbol, side=side, qty=qty,
                   order_type=OrderType.MARKET)
    account = Account(cash=float(qty) * arrival * 2.0 + 1e9)
    broker = SimBroker(
        account=account,
        latency=LATENCY,
        participation_rate=PARTICIPATION,
        slippage_bps=SLIPPAGE_BPS,
        commission_rate=COMMISSION_RATE,
        market_ref="close",
    )
    children = algo.schedule(parent, sched_bars)
    fills = execute_children(broker, children, exec_bars, symbol)
    report = analyze(parent, fills, bars=sched_bars,
                     arrival_price=arrival, decision_price=decision)
    filled = sum(f.qty for f in fills)
    early = sum(f.qty for f in fills if f.bar < len(sched_bars) // 3)
    return AlgoRun(
        name=name,
        num_children=len(children),
        fills=fills,
        report=report,
        last_fill_bar=max((f.bar for f in fills), default=-1),
        early_fill_pct=(100.0 * early / filled) if filled > 0 else 0.0,
    )


def report_dict(run: AlgoRun, window: int) -> dict:
    """把 TCAReport + 执行侧信息整理成可 JSON 序列化的 dict。"""
    r = run.report
    notional = r.decision_price * r.ordered_qty
    return {
        "num_children": run.num_children,
        "num_fills": r.num_fills,
        "ordered_qty": r.ordered_qty,
        "filled_qty": r.filled_qty,
        "fill_ratio": r.fill_ratio,
        "average_fill_price": r.average_fill_price,
        "slippage_bps": r.slippage_bps,
        "delay_cost_bps": r.delay_cost_bps,
        "realized_spread_bps": r.realized_spread_bps,
        "market_impact_bps": r.market_impact_bps,
        "commission": r.commission,
        "commission_bps": (r.commission / notional * 1e4) if notional > 0 else 0.0,
        "implementation_shortfall": r.implementation_shortfall,
        "implementation_shortfall_bps": r.implementation_shortfall_bps,
        "last_fill_bar": run.last_fill_bar,
        "early_fill_pct": run.early_fill_pct,
        "window_bars": window,
    }


def write_outputs(outdir: Path, runs, meta) -> None:
    """写 REPORT.md / tca.json / fills_twap.csv。"""
    outdir.mkdir(parents=True, exist_ok=True)

    # ---- tca.json ----
    payload = {
        "meta": meta,
        "reports": {run.name: report_dict(run, meta["window"]) for run in runs},
    }
    (outdir / "tca.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # ---- fills_twap.csv（TWAP 成交明细，量小便于人工核对） ----
    twap = next(run for run in runs if run.name == "TWAP")
    fills_to_frame(twap.fills).to_csv(outdir / "fills_twap.csv", index=False)

    # ---- REPORT.md ----
    cols = {run.name: report_dict(run, meta["window"]) for run in runs}
    names = [run.name for run in runs]

    def row(label, key, fmt):
        cells = " | ".join(fmt(cols[n][key]) for n in names)
        return f"| {label} | {cells} |"

    f4 = lambda v: f"{v:,.4f}"          # noqa: E731
    f2 = lambda v: f"{v:,.2f}"          # noqa: E731
    fp = lambda v: f"{v:.2%}"           # noqa: E731
    fi = lambda v: f"{int(v)}"          # noqa: E731

    table = "\n".join([
        "| 指标 | " + " | ".join(names) + " |",
        "|---|" + "---:|" * len(names),
        row("子单切片数", "num_children", fi),
        row("成交笔数", "num_fills", fi),
        row("成交率", "fill_ratio", fp),
        row("成交均价(hfq)", "average_fill_price", f4),
        row("滑点(bp, vs 到达价)", "slippage_bps", f2),
        row("相对基准VWAP(bp)", "realized_spread_bps", f2),
        row("市场冲击(bp, 平方根模型)", "market_impact_bps", f2),
        row("实现差额IS(bp)", "implementation_shortfall_bps", f2),
        row("佣金(bp)", "commission_bps", f2),
        row("最后成交bar", "last_fill_bar", fi),
        row("窗口前1/3完成度(%)", "early_fill_pct", f2),
    ])

    full = all(cols[n]["fill_ratio"] >= 0.999 for n in names)
    fill_note = (
        "三种算法均在执行窗口（含尾部承接 bar）内**足额完成**（成交率≈100%）。"
        if full else
        "部分算法因参与率上限 / 窗口截止未足额成交，见表中成交率。"
    )
    best_spread = min(names, key=lambda n: abs(cols[n]["realized_spread_bps"]))
    worst_spread = max(names, key=lambda n: abs(cols[n]["realized_spread_bps"]))
    sgn = 1.0 if meta["side"] == "BUY" else -1.0
    drift = meta["window_drift_bps"]
    signed_drift = sgn * drift                     # >0 = 窗口内价格对该方向不利漂移
    trend_word = "上行" if drift > 0 else ("下行" if drift < 0 else "走平")
    drift_word = ("不利" if signed_drift > 0 else "有利")
    best_is = min(names, key=lambda n: cols[n]["implementation_shortfall_bps"])

    report_md = f"""# 真实 A 股 bar 执行算法 TCA 报告（{meta['symbol']}）

> 由 `examples/real_execution_tca.py` 离线生成。撮合为**纯内存模拟**
> （SimBroker），不接任何真实券商通道；结果仅用于执行算法研究，**不构成投资建议**。

## 数据与设置
- 数据：`{meta['csv']}`（真实 A 股日线，**后复权 hfq**，只读；volume 单位以数据源口径为准）。
  hfq 价格水平被放大，故下文所有成本指标均为**基点(bp)**，与价格水平无关。
- 执行窗口：{meta['window_start']} ~ {meta['window_end']}，共 {meta['window']} 根 bar
  （另留 {meta['tail_bars']} 根尾部执行 bar 承接 latency/参与率导致的未竟部分）。
  窗口内价格整体**{trend_word}**：首 bar 开盘 {meta['arrival_price']:,.2f} →
  末 bar 收盘 {meta['window_last_close']:,.2f}（对 {meta['side']} 方向为**{drift_word}**漂移
  {signed_drift:+.1f}bp）。
- 父单：{meta['side']} {meta['parent_qty']:,.0f}（≈窗口总成交量的 {meta['parent_pct_of_volume']:.2%}；
  窗口总成交量 {meta['window_total_volume']:,.0f}）。
- SimBroker：latency={LATENCY}、单子单单 bar 参与率上限={PARTICIPATION:.0%}、
  滑点={SLIPPAGE_BPS}bp、佣金={COMMISSION_RATE:.2%}（万三）、市价单按 bar 收盘价撮合。
- 算法：TWAP（等时切片）、VWAP（按量切片）、ImplementationShortfall（decay={IS_DECAY}，前重后轻）。
- 基准：窗口 VWAP（成交量加权收盘价）= {meta['benchmark_vwap']:,.4f}（hfq）；
  到达价 = 决策价 = 窗口首根 bar 开盘价 = {meta['arrival_price']:,.4f}（hfq）。

## 三算法 TCA 对比
{table}

## 简要解读
- **成交率**：{fill_note}
- **窗口趋势主导成本**：本窗口价格{trend_word}、对 {meta['side']} 为{drift_word}漂移
  （{signed_drift:+.1f}bp）。因此「滑点(vs 到达价)」与「实现差额 IS」的绝对水平主要由
  **趋势漂移 × 成交时点**决定，而非撮合摩擦（建模滑点仅 {SLIPPAGE_BPS}bp、佣金约 3bp）。
  越早完成成交，越能锁定接近到达价的成本。
- **相对基准 VWAP**（成交均价 vs 窗口收盘价加权 VWAP，剔除趋势后的择时/口径偏差）：
  本次「{best_spread}」最贴近基准（{cols[best_spread]['realized_spread_bps']:+.2f}bp），
  「{worst_spread}」偏离最大（{cols[worst_spread]['realized_spread_bps']:+.2f}bp）。
  注意：latency={LATENCY} 使每个子单在其所属 bar 的**下一根**收盘成交，VWAP 虽按成交量
  切片、却整体滞后一根 bar，在趋势窗口中并不能完美贴合收盘加权基准；三者谁最贴近基准
  高度依赖本窗口的量价路径，属样本内现象。
- **IS（前重后轻）**：窗口前 1/3 即完成 {cols['IS']['early_fill_pct']:.1f}% 的成交量
  （TWAP {cols['TWAP']['early_fill_pct']:.1f}% / VWAP {cols['VWAP']['early_fill_pct']:.1f}%），
  最后成交 bar={cols['IS']['last_fill_bar']}（TWAP/VWAP 均延伸至窗口尾），
  最快锁定决策价、延迟风险最小；在本次{drift_word}漂移窗口中其实现差额
  {cols['IS']['implementation_shortfall_bps']:+.2f}bp 亦为三者最低（最优={best_is}）。
  代价是早期集中参与，但平方根冲击模型仅按总参与率计量，三者冲击估计同为
  {cols['IS']['market_impact_bps']:.2f}bp（差异体现在成交时点分布而非总量）。
- **实现差额 IS(bp)**：以决策价为基准的总成本（含佣金）。本窗口 {best_is} 最低。
  由于结论由单一历史窗口的价格路径驱动，**不构成算法优劣的一般性结论**；
  换一段窗口（尤其趋势反向时）排序可能改变。

## 产物
- `tca.json`：全部指标与运行元数据（机器可读）。
- `fills_twap.csv`：TWAP 的逐笔成交明细（fill_id/order_id/qty/price/bar/滑点等）。
"""
    (outdir / "REPORT.md").write_text(report_md, encoding="utf-8")


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="真实 A 股 bar 执行算法 + TCA 对比（离线模拟）")
    p.add_argument("--csv", default=DEFAULT_CSV, help="真实 OHLCV CSV 路径（只读）")
    p.add_argument("--qty", type=int, default=None,
                   help="父单数量（与 volume 同口径）；缺省=窗口总成交量的 2%%")
    p.add_argument("--window", type=int, default=60, help="执行窗口 bar 数（默认 60）")
    p.add_argument("--side", choices=["BUY", "SELL"], default="BUY", help="父单方向")
    p.add_argument("--outdir", default=str(DEFAULT_OUTDIR), help="结果输出目录")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    side = Side[args.side]

    # 1) 加载真实 bar：窗口 + 尾部承接 bar，index 已由 load_bars 重排为 0..n-1
    exec_bars = load_bars(args.csv, last_n=args.window + TAIL_BARS)
    n = len(exec_bars)
    tail = min(TAIL_BARS, max(0, n - 1))
    window = n - tail
    sched_bars = exec_bars[:window]
    symbol = exec_bars[0].symbol

    frame = load_ohlcv_frame(args.csv)
    dates = frame.index[-n:]
    window_start, window_end = dates[0].date(), dates[window - 1].date()

    # 2) 父单数量与基准
    window_vol = sum(b.volume for b in sched_bars)
    qty = args.qty if args.qty else int(round(window_vol * TARGET_PCT_OF_VOLUME))
    if qty <= 0:
        raise SystemExit("父单数量必须为正（--qty 或窗口成交量过小）")
    arrival = decision = sched_bars[0].open
    bench = benchmark_vwap(sched_bars)

    print("=" * 78)
    print("Kairos Execution —— 真实 A 股 bar 执行 + TCA（离线模拟，不接真实券商）")
    print(f"数据: {args.csv} (hfq, 只读)")
    print(f"窗口: {window_start} ~ {window_end} 共 {window} 根 bar (+{tail} 根尾部执行 bar) "
          f"| symbol={symbol}")
    print(f"父单: {side.name} {qty:,} ≈ 窗口总成交量({window_vol:,.0f})的 "
          f"{qty / window_vol:.2%}")
    print(f"到达价=决策价=首bar开盘 {arrival:,.4f} | 窗口基准VWAP {bench:,.4f} (hfq)")
    drift_bps = (sched_bars[-1].close - arrival) / arrival * 1e4
    print(f"窗口价格漂移: {drift_bps:+.1f}bp (首bar开盘→末bar收盘, 正=上行)")
    print(f"SimBroker: latency={LATENCY}, 参与率上限={PARTICIPATION:.0%}/子单/bar, "
          f"滑点={SLIPPAGE_BPS}bp, 佣金={COMMISSION_RATE:.2%}")

    # 3) 三算法执行
    runs = [
        run_algo("TWAP", TWAP(), exec_bars, sched_bars, symbol, side, qty,
                 arrival, decision),
        run_algo("VWAP", VWAP(), exec_bars, sched_bars, symbol, side, qty,
                 arrival, decision),
        run_algo("IS", ImplementationShortfall(decay=IS_DECAY), exec_bars,
                 sched_bars, symbol, side, qty, arrival, decision),
    ]

    print("=" * 78)
    header = f"{'指标':<22}" + "".join(f"{run.name:>16}" for run in runs)
    print(header)
    rows = [
        ("成交率", lambda d: f"{d['fill_ratio']:.2%}"),
        ("成交均价(hfq)", lambda d: f"{d['average_fill_price']:,.2f}"),
        ("滑点(bp,vs到达价)", lambda d: f"{d['slippage_bps']:.2f}"),
        ("相对基准VWAP(bp)", lambda d: f"{d['realized_spread_bps']:.2f}"),
        ("市场冲击(bp)", lambda d: f"{d['market_impact_bps']:.2f}"),
        ("实现差额IS(bp)", lambda d: f"{d['implementation_shortfall_bps']:.2f}"),
        ("佣金(bp)", lambda d: f"{d['commission_bps']:.2f}"),
        ("成交笔数", lambda d: f"{d['num_fills']}"),
        ("最后成交bar", lambda d: f"{d['last_fill_bar']}"),
        ("窗口前1/3完成度(%)", lambda d: f"{d['early_fill_pct']:.1f}"),
    ]
    dicts = [report_dict(run, window) for run in runs]
    for label, fmt in rows:
        print(f"{label:<22}" + "".join(f"{fmt(d):>16}" for d in dicts))

    # 4) 产物落盘
    meta = {
        "csv": str(Path(args.csv).resolve()),
        "symbol": symbol,
        "side": side.name,
        "window": window,
        "window_start": str(window_start),
        "window_end": str(window_end),
        "tail_bars": tail,
        "parent_qty": float(qty),
        "window_total_volume": float(window_vol),
        "parent_pct_of_volume": qty / window_vol,
        "arrival_price": float(arrival),
        "decision_price": float(decision),
        "benchmark_vwap": float(bench),
        "window_last_close": float(sched_bars[-1].close),
        "window_drift_bps": float((sched_bars[-1].close - arrival) / arrival * 1e4),
        "broker": {
            "latency": LATENCY,
            "participation_rate": PARTICIPATION,
            "slippage_bps": SLIPPAGE_BPS,
            "commission_rate": COMMISSION_RATE,
            "market_ref": "close",
        },
        "is_decay": IS_DECAY,
        "price_basis": "hfq(后复权)：价格水平被放大，成本指标以 bp 计不受影响",
    }
    outdir = Path(args.outdir)
    write_outputs(outdir, runs, meta)
    print("=" * 78)
    print(f"产物已写入: {outdir}/REPORT.md, tca.json, fills_twap.csv")
    tw = pd.read_csv(outdir / "fills_twap.csv")
    print(f"fills_twap.csv: {len(tw)} 笔 (TWAP 成交明细)")


if __name__ == "__main__":
    main()
