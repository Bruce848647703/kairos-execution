"""核心数据类型测试：Order 校验、Position 盈亏、Account 权益、Bar 工具。"""
import pytest

from kairos_execution import (
    Account,
    Bar,
    Order,
    OrderStatus,
    OrderType,
    Position,
    Side,
)


def test_order_rejects_nonpositive_qty():
    with pytest.raises(ValueError):
        Order("X", "SIM", Side.BUY, 0)


def test_limit_order_requires_limit_price():
    with pytest.raises(ValueError):
        Order("X", "SIM", Side.BUY, 10, OrderType.LIMIT)


def test_stop_order_requires_stop_price():
    with pytest.raises(ValueError):
        Order("X", "SIM", Side.BUY, 10, OrderType.STOP)


def test_order_accepts_string_side_and_type():
    o = Order("X", "SIM", "buy", 10, "limit", limit_price=100.0)
    assert o.side is Side.BUY
    assert o.order_type is OrderType.LIMIT


def test_order_remaining_and_flags():
    o = Order("X", "SIM", Side.BUY, 10, OrderType.MARKET)
    assert o.remaining == pytest.approx(10)
    assert o.is_active and not o.is_terminal
    o.filled_qty = 10
    o.status = OrderStatus.FILLED
    assert o.remaining == pytest.approx(0)
    assert o.is_terminal and not o.is_active


def test_position_buy_averages_and_sell_realizes_pnl():
    p = Position("SIM")
    p.apply_fill(Side.BUY, 100, 10.0)
    assert p.qty == pytest.approx(100)
    assert p.avg_price == pytest.approx(10.0)
    p.apply_fill(Side.BUY, 100, 12.0)
    assert p.qty == pytest.approx(200)
    assert p.avg_price == pytest.approx(11.0)
    p.apply_fill(Side.SELL, 50, 15.0)
    assert p.qty == pytest.approx(150)
    # 约定与样板一致：realized = (avg - exec) * qty
    assert p.realized_pnl == pytest.approx((11.0 - 15.0) * 50)


def test_account_equity_and_market_value():
    acct = Account(cash=1000.0)
    acct.position("SIM").apply_fill(Side.BUY, 10, 100.0)
    assert acct.market_value({"SIM": 110.0}) == pytest.approx(10 * 110.0)
    assert acct.equity({"SIM": 110.0}) == pytest.approx(1000.0 + 10 * 110.0)


def test_account_position_autocreate():
    acct = Account(cash=0.0)
    p = acct.position("NEW")
    assert isinstance(p, Position) and p.qty == 0.0
    assert acct.position("NEW") is p            # 幂等


def test_bar_contains_and_typical_price():
    b = Bar("SIM", 100, 105, 95, 100, 1000)
    assert b.contains(100) and b.contains(95) and not b.contains(106)
    assert b.typical_price == pytest.approx((105 + 95 + 100) / 3)
