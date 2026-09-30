"""realdata 离线测试：tmp_path 造小 OHLCV CSV，验证 load_bars / load_ohlcv_frame。

不联网、不依赖任何外部真实数据文件；行故意乱序写入以验证「按日期升序」。
"""
import pandas as pd
import pytest

from kairos_execution import Bar, load_bars, load_ohlcv_frame

HEADER = "date,open,high,low,close,volume\n"
# 乱序写入；OHLC 满足 high ≥ max(o,c) ≥ min(o,c) ≥ low
ROWS = [
    "2024-01-05,10.50,11.00,10.20,10.80,1200\n",
    "2024-01-03,10.00,10.40,9.80,10.10,1000\n",
    "2024-01-04,10.10,10.60,10.00,10.50,1100\n",
    "2024-01-08,11.00,11.50,10.90,11.20,1500\n",
    "2024-01-09,11.20,11.30,10.70,10.90,1300\n",
]
DATES_SORTED = ["2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08", "2024-01-09"]
CLOSES_SORTED = [10.10, 10.50, 10.80, 11.20, 10.90]


@pytest.fixture()
def csv_path(tmp_path):
    """写一个 5 行乱序日期的小 CSV，文件名主干用作默认 symbol。"""
    p = tmp_path / "sh600519.csv"
    p.write_text(HEADER + "".join(ROWS), encoding="utf-8")
    return p


def test_load_ohlcv_frame_sorted_and_typed(csv_path):
    frame = load_ohlcv_frame(csv_path)
    assert list(frame.columns) == ["open", "high", "low", "close", "volume"]
    assert isinstance(frame.index, pd.DatetimeIndex)
    assert frame.index.is_monotonic_increasing
    assert [d.strftime("%Y-%m-%d") for d in frame.index] == DATES_SORTED
    assert frame["close"].tolist() == pytest.approx(CLOSES_SORTED)
    assert frame.dtypes["volume"] == float


def test_load_bars_fields_index_and_symbol(csv_path):
    bars = load_bars(csv_path)
    assert all(isinstance(b, Bar) for b in bars)
    assert len(bars) == 5
    assert [b.index for b in bars] == list(range(5))       # 重排为 0..n-1
    assert all(b.symbol == "sh600519" for b in bars)       # 默认取文件名主干
    first = bars[0]
    assert (first.open, first.high, first.low, first.close, first.volume) == \
        pytest.approx((10.00, 10.40, 9.80, 10.10, 1000.0))
    assert [b.close for b in bars] == pytest.approx(CLOSES_SORTED)


def test_load_bars_symbol_override(csv_path):
    bars = load_bars(csv_path, symbol="AAA")
    assert all(b.symbol == "AAA" for b in bars)


def test_load_bars_ohlcv_envelope(csv_path):
    """数据满足 high ≥ max(o,c) ≥ min(o,c) ≥ low 时，转出的 Bar 应保持该关系。"""
    for b in load_bars(csv_path):
        assert b.high >= max(b.open, b.close)
        assert min(b.open, b.close) >= b.low
        assert b.high >= b.low
        assert b.volume >= 0


def test_load_bars_last_n_takes_most_recent(csv_path):
    bars = load_bars(csv_path, last_n=2)
    assert len(bars) == 2
    assert [b.close for b in bars] == pytest.approx(CLOSES_SORTED[-2:])
    assert [b.index for b in bars] == [0, 1]               # 截取后仍从 0 重排
    assert [d.strftime("%Y-%m-%d") for d in
            load_ohlcv_frame(csv_path).index[-2:]] == DATES_SORTED[-2:]


def test_load_bars_last_n_exceeds_length(csv_path):
    assert len(load_bars(csv_path, last_n=100)) == 5        # 超过总根数→取全部


def test_load_bars_invalid_last_n(csv_path):
    with pytest.raises(ValueError):
        load_bars(csv_path, last_n=0)


def test_load_ohlcv_frame_missing_column(tmp_path):
    p = tmp_path / "bad.csv"
    p.write_text("date,open,high,low,close\n2024-01-03,1,2,0.5,1.5\n",
                 encoding="utf-8")
    with pytest.raises(ValueError, match="volume"):
        load_ohlcv_frame(p)


def test_load_ohlcv_frame_drops_dirty_rows(tmp_path):
    p = tmp_path / "dirty.csv"
    p.write_text(
        HEADER
        + "2024-01-03,10.0,10.4,9.8,10.1,1000\n"
        + "2024-01-04,-1.0,10.6,10.0,10.5,1100\n"     # 价格非正 → 剔除
        + "2024-01-05,10.5,11.0,10.2,10.8,1200\n",
        encoding="utf-8",
    )
    frame = load_ohlcv_frame(p)
    assert len(frame) == 2
    assert [d.strftime("%Y-%m-%d") for d in frame.index] == \
        ["2024-01-03", "2024-01-05"]


def test_load_bars_feeds_algo_schedule(csv_path):
    """转出的真实格式 bar 应可直接喂给执行算法（与合成行情路径一致）。"""
    from kairos_execution import TWAP, Order, OrderType, Side

    bars = load_bars(csv_path)
    parent = Order("P", bars[0].symbol, Side.BUY, 300, OrderType.MARKET)
    children = TWAP().schedule(parent, bars)
    assert sum(c.qty for c in children) == pytest.approx(300)
    assert [c.created_bar for c in children] == list(range(len(bars)))
