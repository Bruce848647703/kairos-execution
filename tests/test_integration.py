"""端到端集成测试：合成行情 → 算法切片 → SimBroker 撮合 → TCA 汇总。"""
import pytest

from kairos_execution import (
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


def _run(algo, bars, symbol="SIM", qty=10000):
    parent = Order("P", symbol, Side.BUY, qty, OrderType.MARKET)
    account = Account(cash=1e7)
    broker = SimBroker(account=account, latency=0, participation_rate=0.10,
                       slippage_bps=3.0, commission_rate=0.0003)
    children = algo.schedule(parent, bars)
    fills = execute_children(broker, children, bars, symbol)
    report = analyze(parent, fills, bars=bars,
                     arrival_price=bars[0].open, decision_price=bars[0].open)
    return children, fills, report, account


def test_execute_children_fully_fills_twap_and_vwap():
    bars = to_bars(make_ohlcv(n_bars=60, seed=7, base_volume=200_000), "SIM")
    for algo in (TWAP(), VWAP()):
        children, fills, report, account = _run(algo, bars)
        assert sum(c.qty for c in children) == pytest.approx(10000)
        assert report.filled_qty == pytest.approx(10000)
        assert report.fill_ratio == pytest.approx(1.0)
        assert account.position("SIM").qty == pytest.approx(10000)
        assert len(fills) >= 1


def test_vwap_realized_spread_near_slippage_only():
    # VWAP 按成交量切片、且在各自 bar 的收盘价成交，
    # 其成交均价 ≈ (1+滑点)×市场基准 VWAP，故相对基准的价差 ≈ 滑点本身。
    bars = to_bars(make_ohlcv(n_bars=80, seed=11, base_volume=200_000), "SIM")
    _, _, rep, _ = _run(VWAP(), bars)
    assert rep.realized_spread_bps == pytest.approx(3.0, abs=0.5)


def test_vwap_tracks_benchmark_at_least_as_well_as_twap():
    bars = to_bars(make_ohlcv(n_bars=80, seed=11, base_volume=200_000), "SIM")
    _, _, rep_twap, _ = _run(TWAP(), bars)
    _, _, rep_vwap, _ = _run(VWAP(), bars)
    assert abs(rep_vwap.realized_spread_bps) <= abs(rep_twap.realized_spread_bps) + 1e-6


def test_make_ohlcv_is_deterministic():
    a = make_ohlcv(n_bars=30, seed=5)
    b = make_ohlcv(n_bars=30, seed=5)
    assert a.equals(b)


def test_ohlcv_shape_and_envelope():
    df = make_ohlcv(n_bars=50, seed=3)
    assert list(df.columns) == ["symbol", "open", "high", "low", "close", "volume"]
    assert (df["high"] >= df["close"]).all()
    assert (df["low"] <= df["close"]).all()
    assert (df["high"] >= df["low"]).all()
    assert (df["volume"] > 0).all()


def test_to_bars_length_and_index():
    df = make_ohlcv(n_bars=25, seed=1)
    bars = to_bars(df, "SIM")
    assert len(bars) == 25
    assert [b.index for b in bars] == list(range(25))
    assert all(b.symbol == "SIM" for b in bars)
