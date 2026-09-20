"""SimBroker 撮合测试：限价/止损、部分成交累积、成交延迟、滑点、账户更新。"""
import pytest

from kairos_execution import (
    Account,
    Bar,
    Order,
    OrderStatus,
    OrderType,
    Side,
    SimBroker,
)


def _bar(o, h, low, c, v, i=0, sym="SIM"):
    return Bar(sym, o, h, low, c, v, index=i)


def _mkt(order, bar):
    return {order.symbol: bar}


# ------------------------------ 限价单 ------------------------------
def test_limit_buy_fills_only_when_low_below_limit():
    broker = SimBroker(latency=0, participation_rate=None, slippage_bps=0.0)
    o = Order("L1", "SIM", Side.BUY, 100, OrderType.LIMIT, limit_price=100.0)
    broker.submit(o, bar=0)
    # low=101 > limit → 不成交
    broker.on_bar(0, _mkt(o, _bar(102, 103, 101, 102, 1e6, 0)))
    assert o.status is OrderStatus.ACCEPTED
    assert o.filled_qty == 0
    # low=99 <= limit → 以限价成交（限价单无滑点）
    broker.on_bar(1, _mkt(o, _bar(101, 101, 99, 100, 1e6, 1)))
    assert o.status is OrderStatus.FILLED
    assert o.filled_qty == pytest.approx(100)
    assert o.avg_fill_price == pytest.approx(100.0)


def test_limit_sell_fills_when_high_above_limit():
    broker = SimBroker(latency=0, participation_rate=None)
    o = Order("L2", "SIM", Side.SELL, 50, OrderType.LIMIT, limit_price=110.0)
    broker.submit(o, bar=0)
    broker.on_bar(0, _mkt(o, _bar(105, 108, 104, 107, 1e6, 0)))   # high=108 < 110
    assert o.filled_qty == 0
    broker.on_bar(1, _mkt(o, _bar(108, 112, 107, 111, 1e6, 1)))   # high=112 >= 110
    assert o.status is OrderStatus.FILLED
    assert o.avg_fill_price == pytest.approx(110.0)


# ------------------------------ 止损单 ------------------------------
def test_stop_buy_triggers_on_cross_fills_at_stop():
    broker = SimBroker(latency=0, participation_rate=None, slippage_bps=0.0)
    o = Order("S1", "SIM", Side.BUY, 100, OrderType.STOP, stop_price=105.0)
    broker.submit(o, bar=0)
    broker.on_bar(0, _mkt(o, _bar(100, 104, 99, 103, 1e6, 0)))     # high=104 < 105
    assert o.filled_qty == 0
    broker.on_bar(1, _mkt(o, _bar(103, 106, 102, 105.5, 1e6, 1)))  # high=106 >= 105
    assert o.status is OrderStatus.FILLED
    assert o.avg_fill_price == pytest.approx(105.0)               # 触发价成交


def test_stop_buy_gap_open_above_stop_fills_at_open():
    broker = SimBroker(latency=0, participation_rate=None, slippage_bps=0.0)
    o = Order("S2", "SIM", Side.BUY, 100, OrderType.STOP, stop_price=105.0)
    broker.submit(o, bar=0)
    broker.on_bar(0, _mkt(o, _bar(108, 110, 107, 109, 1e6, 0)))   # 开盘跳空 108 > 105
    assert o.status is OrderStatus.FILLED
    assert o.avg_fill_price == pytest.approx(108.0)               # 以开盘价成交


def test_stop_sell_triggers_on_downward_cross():
    broker = SimBroker(latency=0, participation_rate=None, slippage_bps=0.0)
    o = Order("S3", "SIM", Side.SELL, 100, OrderType.STOP, stop_price=95.0)
    broker.submit(o, bar=0)
    broker.on_bar(0, _mkt(o, _bar(100, 101, 96, 98, 1e6, 0)))     # low=96 > 95
    assert o.filled_qty == 0
    broker.on_bar(1, _mkt(o, _bar(98, 99, 94, 95, 1e6, 1)))       # low=94 <= 95
    assert o.status is OrderStatus.FILLED
    assert o.avg_fill_price == pytest.approx(95.0)


# --------------------------- 部分成交累积 ---------------------------
def test_partial_fill_accumulates_to_full_via_participation():
    # 参与率 0.1、bar 量 1000 → 每 bar 最多 100；共 250 需 3 根 bar
    broker = SimBroker(latency=0, participation_rate=0.1, slippage_bps=0.0)
    o = Order("M1", "SIM", Side.BUY, 250, OrderType.MARKET)
    broker.submit(o, bar=0)
    for i in range(4):
        broker.on_bar(i, _mkt(o, _bar(100, 100, 100, 100.0, 1000, i)))
        if i < 2:
            assert o.status is OrderStatus.PARTIAL
    assert o.status is OrderStatus.FILLED
    assert o.filled_qty == pytest.approx(250)
    qtys = [f.qty for f in broker.fills("M1")]
    assert qtys == [pytest.approx(100), pytest.approx(100), pytest.approx(50)]


def test_participation_rate_none_means_unlimited():
    broker = SimBroker(latency=0, participation_rate=None)
    o = Order("U1", "SIM", Side.BUY, 1e6, OrderType.MARKET)
    broker.submit(o, bar=0)
    broker.on_bar(0, _mkt(o, _bar(100, 100, 100, 100.0, 1.0, 0)))  # 量仅 1 也不限量
    assert o.status is OrderStatus.FILLED
    assert o.filled_qty == pytest.approx(1e6)


# ----------------------------- 成交延迟 -----------------------------
def test_latency_delays_fill_by_n_bars():
    broker = SimBroker(latency=2, participation_rate=None)
    o = Order("D1", "SIM", Side.BUY, 100, OrderType.MARKET)
    broker.submit(o, bar=0)
    broker.on_bar(0, _mkt(o, _bar(100, 100, 100, 100.0, 1e6, 0)))
    assert o.filled_qty == 0                     # 0 < 0+2
    broker.on_bar(1, _mkt(o, _bar(100, 100, 100, 100.0, 1e6, 1)))
    assert o.filled_qty == 0                     # 1 < 0+2
    broker.on_bar(2, _mkt(o, _bar(100, 100, 100, 100.0, 1e6, 2)))
    assert o.status is OrderStatus.FILLED        # 2 >= 0+2 → 第 2 根 bar 成交
    assert broker.fills("D1")[0].bar == 2


def test_latency_zero_fills_same_bar():
    broker = SimBroker(latency=0, participation_rate=None)
    o = Order("D2", "SIM", Side.BUY, 100, OrderType.MARKET)
    broker.submit(o, bar=3)
    broker.on_bar(3, _mkt(o, _bar(100, 100, 100, 100.0, 1e6, 3)))
    assert o.status is OrderStatus.FILLED
    assert broker.fills("D2")[0].bar == 3


# ------------------------------- 滑点 -------------------------------
def test_slippage_worsens_market_buy():
    broker = SimBroker(latency=0, participation_rate=None, slippage_bps=50.0)
    o = Order("SL", "SIM", Side.BUY, 100, OrderType.MARKET)
    broker.submit(o, bar=0)
    broker.on_bar(0, _mkt(o, _bar(100, 100, 100, 100.0, 1e6, 0)))
    assert o.avg_fill_price == pytest.approx(100.5)   # close 100 + 50bp


def test_slippage_recorded_on_fill():
    broker = SimBroker(latency=0, participation_rate=None, slippage_bps=50.0)
    o = Order("SL2", "SIM", Side.BUY, 100, OrderType.MARKET)
    broker.submit(o, bar=0)
    broker.on_bar(0, _mkt(o, _bar(100, 100, 100, 100.0, 1e6, 0)))
    f = broker.fills("SL2")[0]
    assert f.reference_price == pytest.approx(100.0)
    assert f.slippage == pytest.approx(0.5)           # 买入抬价 0.5


# --------------------------- 账户与撤单 ---------------------------
def test_account_updates_on_buy_fill():
    acct = Account(cash=1_000_000.0)
    broker = SimBroker(account=acct, latency=0, participation_rate=None,
                       commission_rate=0.001)
    o = Order("A1", "SIM", Side.BUY, 100, OrderType.MARKET)
    broker.submit(o, bar=0)
    broker.on_bar(0, _mkt(o, _bar(100, 100, 100, 100.0, 1e6, 0)))
    pos = acct.position("SIM")
    assert pos.qty == pytest.approx(100)
    assert pos.avg_price == pytest.approx(100.0)
    # 现金 = 1e6 - 100*100 - 佣金(10000*0.001=10)
    assert acct.cash == pytest.approx(1_000_000 - 10_000 - 10)


def test_cancel_active_order_via_broker():
    broker = SimBroker(latency=0, participation_rate=None)
    o = Order("C1", "SIM", Side.BUY, 100, OrderType.MARKET)
    broker.submit(o, bar=0)
    broker.cancel("C1")
    assert o.status is OrderStatus.CANCELLED
    broker.on_bar(0, _mkt(o, _bar(100, 100, 100, 100.0, 1e6, 0)))
    assert o.filled_qty == 0                     # 撤单后不再成交
