"""OMS 状态机与事件日志测试。"""
import pytest

from kairos_execution import (
    IllegalTransitionError,
    Order,
    OrderManagementSystem,
    OrderStatus,
    OrderType,
    Side,
)


def _order(qty=10, oid="O1"):
    return Order(order_id=oid, symbol="SIM", side=Side.BUY, qty=qty,
                 order_type=OrderType.MARKET)


# --------------------------- 正常生命周期 ---------------------------
def test_full_lifecycle_pending_accepted_partial_filled():
    oms = OrderManagementSystem()
    o = _order(10)
    oms.submit(o)
    assert o.status is OrderStatus.PENDING
    oms.accept("O1")
    assert o.status is OrderStatus.ACCEPTED
    oms.partial_fill("O1", 4, 100.0, bar=1)
    assert o.status is OrderStatus.PARTIAL
    assert o.filled_qty == pytest.approx(4)
    oms.fill("O1", 101.0, bar=2)                 # 吃掉剩余 6
    assert o.status is OrderStatus.FILLED
    assert o.filled_qty == pytest.approx(10)
    assert o.remaining == pytest.approx(0)
    expected_avg = (4 * 100.0 + 6 * 101.0) / 10  # 摊薄成交价
    assert o.avg_fill_price == pytest.approx(expected_avg)


def test_event_log_records_all_transitions_in_order():
    oms = OrderManagementSystem()
    oms.submit(_order(10))
    oms.accept("O1")
    oms.partial_fill("O1", 4, 100.0, bar=1)
    oms.fill("O1", 100.0, bar=2)
    actions = [e.action for e in oms.event_log]
    assert actions == ["submit", "accept", "partial_fill", "fill"]
    assert [e.seq for e in oms.event_log] == [1, 2, 3, 4]
    # 成交事件带上了数量与价格
    fill_events = [e for e in oms.event_log if e.action in ("fill", "partial_fill")]
    assert sum(e.qty for e in fill_events) == pytest.approx(10)


def test_multiple_partial_fills_accumulate():
    oms = OrderManagementSystem()
    oms.submit(_order(9))
    oms.accept("O1")
    oms.partial_fill("O1", 3, 100.0)
    oms.partial_fill("O1", 3, 102.0)
    assert oms.get("O1").status is OrderStatus.PARTIAL
    assert oms.get("O1").filled_qty == pytest.approx(6)
    oms.partial_fill("O1", 3, 104.0)             # 最后一笔 → FILLED
    assert oms.get("O1").status is OrderStatus.FILLED
    assert len(oms.fills("O1")) == 3


# --------------------------- 非法状态转换 ---------------------------
def test_illegal_fill_after_filled_raises():
    oms = OrderManagementSystem()
    oms.submit(_order(10))
    oms.accept("O1")
    oms.fill("O1", 100.0, bar=1)
    assert oms.get("O1").status is OrderStatus.FILLED
    with pytest.raises(IllegalTransitionError):
        oms.fill("O1", 100.0, bar=2)             # 已成不能再次成交


def test_illegal_partial_fill_after_filled_raises():
    oms = OrderManagementSystem()
    oms.submit(_order(10))
    oms.accept("O1")
    oms.fill("O1", 100.0)
    with pytest.raises(IllegalTransitionError):
        oms.partial_fill("O1", 1, 100.0)


def test_illegal_cancel_after_filled_raises():
    oms = OrderManagementSystem()
    oms.submit(_order(10))
    oms.accept("O1")
    oms.fill("O1", 100.0)
    with pytest.raises(IllegalTransitionError):
        oms.cancel("O1")                         # 已成不能撤单


def test_illegal_double_cancel_raises():
    oms = OrderManagementSystem()
    oms.submit(_order(10))
    oms.accept("O1")
    oms.cancel("O1")
    with pytest.raises(IllegalTransitionError):
        oms.cancel("O1")


def test_illegal_fill_after_cancel_raises():
    oms = OrderManagementSystem()
    oms.submit(_order(10))
    oms.accept("O1")
    oms.cancel("O1")
    with pytest.raises(IllegalTransitionError):
        oms.partial_fill("O1", 1, 100.0)


def test_illegal_accept_twice_raises():
    oms = OrderManagementSystem()
    oms.submit(_order(10))
    oms.accept("O1")
    with pytest.raises(IllegalTransitionError):
        oms.accept("O1")


def test_fill_before_accept_raises():
    oms = OrderManagementSystem()
    oms.submit(_order(10))
    with pytest.raises(IllegalTransitionError):
        oms.fill("O1", 100.0)                    # PENDING 未受理不能成交


def test_reject_then_accept_raises():
    oms = OrderManagementSystem()
    oms.submit(_order(10))
    oms.reject("O1", reason="风控拒绝")
    assert oms.get("O1").status is OrderStatus.REJECTED
    with pytest.raises(IllegalTransitionError):
        oms.accept("O1")


# --------------------------- 参数与查询 ---------------------------
def test_overfill_raises_value_error():
    oms = OrderManagementSystem()
    oms.submit(_order(10))
    oms.accept("O1")
    with pytest.raises(ValueError):
        oms.partial_fill("O1", 11, 100.0)        # 超过下单量


def test_nonpositive_fill_qty_raises():
    oms = OrderManagementSystem()
    oms.submit(_order(10))
    oms.accept("O1")
    with pytest.raises(ValueError):
        oms.partial_fill("O1", 0, 100.0)


def test_duplicate_submit_raises():
    oms = OrderManagementSystem()
    oms.submit(_order(10, "D"))
    with pytest.raises(ValueError):
        oms.submit(_order(5, "D"))


def test_get_missing_raises_key_error():
    oms = OrderManagementSystem()
    with pytest.raises(KeyError):
        oms.get("NOPE")


def test_queries_active_historical_and_children():
    oms = OrderManagementSystem()
    oms.submit(_order(10, "A"))
    oms.submit(_order(10, "B"))
    child = Order(order_id="C", symbol="SIM", side=Side.BUY, qty=5,
                  order_type=OrderType.MARKET, parent_id="A")
    oms.submit(child)
    oms.accept("A")
    oms.fill("A", 100.0)                         # A → 历史
    oms.accept("B")                              # B → 活动
    assert {o.order_id for o in oms.active_orders()} == {"B", "C"}
    assert {o.order_id for o in oms.historical_orders()} == {"A"}
    assert [c.order_id for c in oms.children_of("A")] == ["C"]
    assert oms.get("A").status is OrderStatus.FILLED
