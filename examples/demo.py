"""Kairos Execution 演示：一张 10000 股父买单，分别用 TWAP 与 VWAP 经 SimBroker 执行。

流程：构造合成价格/成交量路径 → 生成父单 → 算法切片 → 模拟撮合 → TCA 报告，
最后对比两种算法的滑点与成交率。

运行： python examples/demo.py
（离线、固定 seed、纯内存模拟，**不接任何真实券商**）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kairos_execution import (          # noqa: E402
    Account,
    Order,
    OrderType,
    Side,
    SimBroker,
    TWAP,
    VWAP,
    analyze,
    execute_children,
    make_ohlcv,
    to_bars,
)

SYMBOL = "SIM"
PARENT_QTY = 10000


def run_algo(algo, bars):
    """用给定算法调度子单并经 SimBroker 执行，返回 (父单, 子单, broker, 成交, 报告)。"""
    parent = Order(order_id=f"P-{type(algo).__name__}", symbol=SYMBOL, side=Side.BUY,
                   qty=PARENT_QTY, order_type=OrderType.MARKET)
    account = Account(cash=10_000_000.0)
    broker = SimBroker(
        account=account,
        latency=0,                 # 切片在其所属 bar 内成交
        participation_rate=0.10,   # 每根 bar 最多参与 10% 成交量
        slippage_bps=3.0,          # 3bp 滑点
        commission_rate=0.0003,    # 万三佣金
    )
    children = algo.schedule(parent, bars)
    fills = execute_children(broker, children, bars, SYMBOL)
    report = analyze(parent, fills, bars=bars,
                     arrival_price=bars[0].open, decision_price=bars[0].open)
    return parent, children, broker, fills, report


def preview_slices(children, k=6):
    """打印前 k 个切片，直观对比 TWAP（等量）与 VWAP（随量变化）。"""
    rows = [(c.created_bar, int(c.qty)) for c in children[:k]]
    return "  ".join(f"bar{b}:{q}" for b, q in rows)


def main():
    df = make_ohlcv(n_bars=60, s0=100.0, mu=0.0, sigma=0.30, seed=9,
                    base_volume=200_000, symbol=SYMBOL)
    bars = to_bars(df, SYMBOL)

    print("=" * 68)
    print("Kairos Execution 演示 —— 纯模拟执行框架（不接真实券商）")
    print(f"合成行情: {len(bars)} 根 bar | 到达价(首 bar 开盘) = {bars[0].open:.4f} "
          f"| 父单 = 买入 {PARENT_QTY} 股 {SYMBOL}")

    reports = {}
    for name, algo in [("TWAP", TWAP()), ("VWAP", VWAP())]:
        parent, children, broker, fills, report = run_algo(algo, bars)
        reports[name] = report
        print("=" * 68)
        print(f"[{name}] 子单切片数 = {len(children)} | 成交笔数 = {len(fills)} | "
              f"已成交 = {report.filled_qty:.0f}/{PARENT_QTY} "
              f"(成交率 {report.fill_ratio:.2%})")
        print(f"  切片预览: {preview_slices(children)} ...")
        frame = broker.fills_frame()
        if not frame.empty:
            show = frame[["order_id", "side", "qty", "price", "bar"]].head(5)
            print("  成交明细(前 5 笔):")
            for line in show.to_string(index=False).splitlines():
                print("    " + line)
        print("  TCA 报告:")
        for line in report.to_frame().to_string(index=False).splitlines():
            print("    " + line)

    tw, vw = reports["TWAP"], reports["VWAP"]
    print("=" * 68)
    print("TWAP vs VWAP 对比")
    print(f"  {'指标':<16}{'TWAP':>16}{'VWAP':>16}")
    rows = [
        ("成交率", f"{tw.fill_ratio:.4f}", f"{vw.fill_ratio:.4f}"),
        ("成交均价", f"{tw.average_fill_price:.4f}", f"{vw.average_fill_price:.4f}"),
        ("滑点(bp)", f"{tw.slippage_bps:.2f}", f"{vw.slippage_bps:.2f}"),
        ("已实现价差(bp)", f"{tw.realized_spread_bps:.2f}", f"{vw.realized_spread_bps:.2f}"),
        ("市场冲击(bp)", f"{tw.market_impact_bps:.2f}", f"{vw.market_impact_bps:.2f}"),
        ("实现差额(bp)", f"{tw.implementation_shortfall_bps:.2f}",
         f"{vw.implementation_shortfall_bps:.2f}"),
    ]
    for label, a, b in rows:
        print(f"  {label:<16}{a:>16}{b:>16}")
    print("=" * 68)
    print("结论: VWAP 按成交量分布下单，成交均价更贴近市场基准 VWAP（已实现价差≈滑点）；")
    print("      TWAP 时间上均匀下单，在量价不匹配时相对基准的偏离更大。二者成交率均达 100%。")


if __name__ == "__main__":
    main()
