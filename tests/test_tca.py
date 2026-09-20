"""TCA 指标测试：滑点、成交率、成交均价、报告汇总、冲击估计。"""
import pytest

from kairos_execution import (
    Bar,
    Fill,
    Order,
    OrderType,
    Side,
    analyze,
    average_fill_price,
    benchmark_vwap,
    fill_ratio,
    market_impact_bps,
    realized_spread_bps,
    slippage_bps,
)


def _fill(qty, price, oid="c", parent="P", commission=0.0):
    return Fill(fill_id=f"f-{oid}-{qty}-{price}", order_id=oid, symbol="SIM",
                side=Side.BUY, qty=qty, price=price, commission=commission,
                parent_id=parent)


# --------------------------- 纯函数指标 ---------------------------
def test_slippage_bps_buy_and_sell():
    # 买入：成交 100.5 vs 到达 100 → +50bp（不利）
    assert slippage_bps(100.0, 100.5, Side.BUY) == pytest.approx(50.0)
    # 卖出：成交 99.5 vs 到达 100 → +50bp（卖得更便宜=不利）
    assert slippage_bps(100.0, 99.5, Side.SELL) == pytest.approx(50.0)
    # 买入但成交更便宜 → 负值（有利）
    assert slippage_bps(100.0, 99.0, Side.BUY) == pytest.approx(-100.0)


def test_fill_ratio_basic():
    assert fill_ratio(8000, 10000) == pytest.approx(0.8)
    assert fill_ratio(0, 10000) == pytest.approx(0.0)
    assert fill_ratio(5, 0) == pytest.approx(0.0)          # 下单量为 0 → 0


def test_average_fill_price_is_volume_weighted():
    fills = [_fill(100, 10.0), _fill(300, 12.0)]
    # (100*10 + 300*12)/400 = 4600/400 = 11.5
    assert average_fill_price(fills) == pytest.approx(11.5)


def test_average_fill_price_empty_is_zero():
    assert average_fill_price([]) == pytest.approx(0.0)


def test_benchmark_vwap():
    bars = [Bar("S", 0, 0, 0, 10, 100), Bar("S", 0, 0, 0, 20, 300)]
    # (10*100 + 20*300)/400 = 7000/400 = 17.5
    assert benchmark_vwap(bars) == pytest.approx(17.5)


def test_realized_spread_bps_sign():
    # 买入成交价高于基准 → 正
    assert realized_spread_bps(102.0, 100.0, Side.BUY) == pytest.approx(200.0)
    # 卖出成交价高于基准 → 有利 → 负
    assert realized_spread_bps(102.0, 100.0, Side.SELL) == pytest.approx(-200.0)


def test_market_impact_increases_with_participation():
    lo = market_impact_bps(100, 100_000, 10.0)
    hi = market_impact_bps(10_000, 100_000, 10.0)
    assert hi > lo >= 0.0
    assert market_impact_bps(0, 100_000, 10.0) == pytest.approx(0.0)


# ----------------------------- 报告汇总 -----------------------------
def test_analyze_report_fields_full_fill():
    parent = Order("P", "SIM", Side.BUY, 1000, OrderType.MARKET)
    fills = [_fill(600, 101.0, "c1", commission=6.0),
             _fill(400, 102.0, "c2", commission=4.0)]
    bars = [Bar("SIM", 100, 101, 99, 100, 5000, 0),
            Bar("SIM", 100, 103, 99, 102, 5000, 1)]
    rep = analyze(parent, fills, bars=bars, arrival_price=100.0, decision_price=100.0)
    assert rep.ordered_qty == pytest.approx(1000)
    assert rep.filled_qty == pytest.approx(1000)
    assert rep.fill_ratio == pytest.approx(1.0)
    # 成交均价 = (600*101 + 400*102)/1000 = 101.4
    assert rep.average_fill_price == pytest.approx(101.4)
    # 滑点 = (101.4-100)/100*1e4 = 140bp
    assert rep.slippage_bps == pytest.approx(140.0)
    assert rep.commission == pytest.approx(10.0)
    # IS(货币) = 1*(101.4-100)*1000 + 10 = 1410
    assert rep.implementation_shortfall == pytest.approx(1410.0)
    assert rep.num_fills == 2


def test_analyze_partial_fill_ratio():
    parent = Order("P", "SIM", Side.BUY, 1000, OrderType.MARKET)
    fills = [_fill(300, 100.0, "c1")]
    rep = analyze(parent, fills, arrival_price=100.0)
    assert rep.fill_ratio == pytest.approx(0.3)
    assert rep.slippage_bps == pytest.approx(0.0)
    assert rep.num_fills == 1


def test_analyze_sell_side_slippage_positive_when_sold_below_arrival():
    parent = Order("P", "SIM", Side.SELL, 500, OrderType.MARKET)
    fills = [Fill("f", "c1", "SIM", Side.SELL, 500, 99.0, parent_id="P")]
    rep = analyze(parent, fills, arrival_price=100.0, decision_price=100.0)
    # 卖出成交价 99 < 到达价 100 → 不利 → 正滑点 100bp
    assert rep.slippage_bps == pytest.approx(100.0)


def test_analyze_defaults_arrival_to_first_bar_open():
    parent = Order("P", "SIM", Side.BUY, 100, OrderType.MARKET)
    fills = [_fill(100, 105.0, "c1")]
    bars = [Bar("SIM", 100, 106, 99, 104, 5000, 0)]
    rep = analyze(parent, fills, bars=bars)          # 不给 arrival/decision
    assert rep.arrival_price == pytest.approx(100.0)  # = bars[0].open
    assert rep.decision_price == pytest.approx(100.0)  # 默认等于到达价
    assert rep.slippage_bps == pytest.approx(500.0)    # (105-100)/100*1e4


def test_report_to_frame_shape():
    parent = Order("P", "SIM", Side.BUY, 100, OrderType.MARKET)
    rep = analyze(parent, [_fill(100, 100.0, "c1")], arrival_price=100.0)
    frame = rep.to_frame()
    assert list(frame.columns) == ["metric", "value"]
    assert "slippage_bps" in set(frame["metric"])
