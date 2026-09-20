"""合成行情工具（离线、固定 seed 可复现），供示例与测试使用。

生成一条带 OHLCV 的价格路径：收盘价用几何布朗运动（GBM）模拟，开/高/低围绕
收盘价构造，成交量叠加「日内 U 型」形态与对数正态噪声，使 VWAP 切片有真实的
分布差异。所有随机性都来自显式 seed，保证结果可复现。
"""
from __future__ import annotations

from typing import List, Optional

import numpy as np
import pandas as pd

from .types import Bar


def make_ohlcv(
    n_bars: int = 120,
    s0: float = 100.0,
    mu: float = 0.05,
    sigma: float = 0.30,
    seed: int = 7,
    base_volume: float = 200_000.0,
    u_shape: float = 0.6,
    start: str = "2022-01-03",
    freq: str = "B",
    symbol: str = "SIM",
) -> pd.DataFrame:
    """生成单标的 OHLCV 面板。

    参数
    ----
    n_bars       : bar 数量。
    s0           : 初始价格。
    mu/sigma     : 年化漂移与波动，按 252 交易日折算到每 bar。
    seed         : 随机种子（固定则结果可复现）。
    base_volume  : 成交量基准。
    u_shape      : 日内 U 型强度（0~1），越大则首尾 bar 成交量越高。
    symbol       : 标的代码（写入返回 DataFrame 的 ``symbol`` 列）。

    返回
    ----
    DataFrame，index 为交易日，columns 为
    ``[symbol, open, high, low, close, volume]``。
    """
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start=start, periods=n_bars, freq=freq)
    dt = 1.0 / 252.0

    # 1) 收盘价：GBM
    shock = rng.standard_normal(n_bars)
    log_ret = (mu - 0.5 * sigma ** 2) * dt + sigma * np.sqrt(dt) * shock
    close = s0 * np.exp(np.cumsum(log_ret))

    # 2) 开盘价：上一收盘 + 轻微跳空
    prev_close = np.concatenate([[s0], close[:-1]])
    gap = rng.standard_normal(n_bars) * sigma * np.sqrt(dt) * 0.3
    open_ = prev_close * np.exp(gap)

    # 3) 高/低价：包住开收，再叠加正向噪声
    hi_base = np.maximum(open_, close)
    lo_base = np.minimum(open_, close)
    wick = np.abs(rng.standard_normal(n_bars)) * sigma * np.sqrt(dt) * 0.5 * hi_base
    high = hi_base + wick
    low = np.maximum(lo_base - wick, 1e-6)

    # 4) 成交量：日内 U 型 × 对数正态噪声
    t = np.linspace(0.0, 1.0, n_bars)
    u = 1.0 + u_shape * (4.0 * (t - 0.5) ** 2)     # 首尾高、中间低
    vol_noise = np.exp(rng.standard_normal(n_bars) * 0.25)
    volume = base_volume * u * vol_noise

    return pd.DataFrame(
        {
            "symbol": symbol,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        },
        index=idx,
    )


def to_bars(df: pd.DataFrame, symbol: Optional[str] = None) -> List[Bar]:
    """把 OHLCV DataFrame 转成 :class:`Bar` 列表。

    ``symbol`` 省略时读取 ``df['symbol']`` 列（逐行），否则用统一代码。
    bar 的 ``index`` 按行序从 0 递增。
    """
    bars: List[Bar] = []
    has_sym_col = "symbol" in df.columns
    for i, (_, row) in enumerate(df.iterrows()):
        sym = symbol if symbol is not None else (
            str(row["symbol"]) if has_sym_col else "SIM"
        )
        bars.append(
            Bar(
                symbol=sym,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["volume"]),
                index=i,
            )
        )
    return bars
