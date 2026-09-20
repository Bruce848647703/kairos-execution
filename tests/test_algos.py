"""执行算法测试：TWAP / VWAP / Iceberg / ImplementationShortfall 的切片正确性。"""
import pytest

from kairos_execution import (
    Bar,
    Iceberg,
    ImplementationShortfall,
    Order,
    OrderType,
    Side,
    TWAP,
    VWAP,
)
from kairos_execution.algos import _allocate


def _parent(qty=10000, side=Side.BUY):
    return Order(order_id="P", symbol="SIM", side=side, qty=qty, order_type=OrderType.MARKET)


# ------------------------------- TWAP -------------------------------
def test_twap_uniform_split_sum_equals_parent():
    kids = TWAP().schedule(_parent(10000), 5)
    assert len(kids) == 5
    assert sum(k.qty for k in kids) == pytest.approx(10000)
    qtys = [k.qty for k in kids]
    assert all(q == pytest.approx(2000) for q in qtys)          # 均匀
    assert all(k.parent_id == "P" and k.side is Side.BUY for k in kids)


def test_twap_handles_indivisible_quantity_exactly():
    kids = TWAP().schedule(_parent(10000), 3)                    # 10000/3 非整除
    assert sum(k.qty for k in kids) == pytest.approx(10000)      # 和仍严格相等
    assert all(k.qty > 0 for k in kids)


def test_twap_with_bars_uses_bar_index():
    bars = [Bar("SIM", 100, 101, 99, 100, 1000, index=i) for i in range(4)]
    kids = TWAP().schedule(_parent(4000), bars)
    assert [k.created_bar for k in kids] == [0, 1, 2, 3]
    assert sum(k.qty for k in kids) == pytest.approx(4000)


# ------------------------------- VWAP -------------------------------
def test_vwap_proportional_to_volume_and_sum():
    vols = [1000, 2000, 3000, 4000]                              # 合计 10000
    kids = VWAP().schedule(_parent(10000), vols)
    qtys = [k.qty for k in kids]
    assert sum(qtys) == pytest.approx(10000)
    total_vol = sum(vols)
    for q, v in zip(qtys, vols):
        assert q == pytest.approx(10000 * v / total_vol, abs=1.0)  # 正比于成交量
    assert qtys == sorted(qtys)                                  # 量随成交量递增


def test_vwap_with_bars_monotonic_in_volume():
    bars = [
        Bar("SIM", 100, 101, 99, 100, v, index=i)
        for i, v in enumerate([500, 1500, 3000])
    ]
    kids = VWAP().schedule(_parent(5000), bars)
    assert sum(k.qty for k in kids) == pytest.approx(5000)
    assert kids[0].qty < kids[1].qty < kids[2].qty


def test_vwap_requires_positive_volume():
    with pytest.raises(ValueError):
        VWAP().schedule(_parent(1000), [0.0, 0.0, 0.0])


# ------------------------------ Iceberg ------------------------------
def test_iceberg_display_limit_and_cumulative():
    kids = Iceberg(display_size=3000).schedule(_parent(10000))
    assert all(k.qty <= 3000 + 1e-9 for k in kids)               # 单次显露 <= 显示量
    assert sum(k.qty for k in kids) == pytest.approx(10000)      # 多批累计完成父单
    assert len(kids) == 4                                        # 3000*3 + 1000
    assert [k.qty for k in kids] == [3000, 3000, 3000, 1000]


def test_iceberg_peek_is_reactive_and_bounded():
    ice = Iceberg(display_size=2500)
    assert ice.peek(10000) == pytest.approx(2500)
    assert ice.peek(1000) == pytest.approx(1000)
    assert ice.peek(0) == pytest.approx(0.0)


def test_iceberg_rejects_nonpositive_display():
    with pytest.raises(ValueError):
        Iceberg(display_size=0)


# --------------------- ImplementationShortfall ---------------------
def test_implementation_shortfall_front_loaded():
    kids = ImplementationShortfall(decay=0.6).schedule(_parent(10000), 5)
    qtys = [k.qty for k in kids]
    assert sum(qtys) == pytest.approx(10000)
    assert qtys[0] > qtys[-1]                                    # 前重后轻
    assert qtys == sorted(qtys, reverse=True)                    # 单调递减


def test_implementation_shortfall_decay_one_is_twap_like():
    kids = ImplementationShortfall(decay=1.0).schedule(_parent(9000), 3)
    qtys = [k.qty for k in kids]
    assert all(q == pytest.approx(3000) for q in qtys)           # decay=1 → 等权


def test_implementation_shortfall_validates_decay():
    with pytest.raises(ValueError):
        ImplementationShortfall(decay=1.5)


# ---------------------------- _allocate ----------------------------
def test_allocate_integer_largest_remainder_exact_sum():
    parts = _allocate(100, [1, 1, 1], lot_size=1)
    assert sum(parts) == pytest.approx(100)
    assert all(p >= 0 for p in parts)
    assert all(float(p).is_integer() for p in parts)


def test_allocate_float_last_fix_exact_sum():
    parts = _allocate(10.0, [1, 2], lot_size=0)
    assert sum(parts) == pytest.approx(10.0)


def test_allocate_zero_weights_falls_back_to_equal():
    parts = _allocate(90, [0, 0, 0], lot_size=1)
    assert parts == [30, 30, 30]
