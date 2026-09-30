"""真实 A 股日线行情加载：把 OHLCV CSV 转成本包的 :class:`Bar` 序列。

数据源为 kairos-data 仓库的 ``data/ashare/<symbol>.csv``（真实 A 股日线，
**后复权 hfq**），要求列 ``date,open,high,low,close,volume``：

- hfq 价格水平被整体放大，绝对价格不具可读性，但**收益率正确**；
  本包 TCA 的成本指标全部以基点(bp)表示，与价格水平无关，故可直接使用。
- ``volume`` 单位以数据源口径为准（手），父单数量应与其保持同一口径，
  参与率 / VWAP 切片等相对量计算不受影响。

本模块**只读、离线**：不联网、不修改源文件，仅依赖 numpy/pandas。
转换复用 :func:`~kairos_execution.sim.to_bars`，保证与合成行情路径完全一致。
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Union

import pandas as pd

from .sim import to_bars
from .types import Bar

# 必需列（date 之外）；顺序即返回 DataFrame 的列顺序
_REQUIRED_COLUMNS = ("open", "high", "low", "close", "volume")


def load_ohlcv_frame(csv_path: Union[str, Path]) -> pd.DataFrame:
    """读取真实 OHLCV CSV，返回按日期升序的 DataFrame。

    处理规则：

    - 必须包含 ``date,open,high,low,close,volume`` 列，缺列抛 ``ValueError``；
    - ``date`` 解析为 ``DatetimeIndex``（名为 ``date``）并升序排序；
    - OHLCV 强制转数值并统一为 ``float64``，含 NaN 或**价格非正**的脏数据行被剔除；
    - 返回列顺序 ``[open, high, low, close, volume]``（源文件若带
      ``symbol`` 列则保留在末列）。

    参数
    ----
    csv_path : CSV 文件路径（只读）。
    """
    path = Path(csv_path)
    df = pd.read_csv(path)
    cols = {c.lower(): c for c in df.columns}
    missing = [c for c in ("date",) + _REQUIRED_COLUMNS if c not in cols]
    if missing:
        raise ValueError(f"CSV 缺少必需列 {missing}: {path}")

    out = pd.DataFrame({
        c: pd.to_numeric(df[cols[c]], errors="coerce").astype("float64")
        for c in _REQUIRED_COLUMNS
    })
    if "symbol" in cols:
        out["symbol"] = df[cols["symbol"]]
    out["date"] = pd.to_datetime(df[cols["date"]], errors="coerce")

    out = out.dropna(subset=["date", *_REQUIRED_COLUMNS])
    prices = out[list(_REQUIRED_COLUMNS[:4])]
    out = out[(prices > 0).all(axis=1) & (out["volume"] >= 0)]
    if out.empty:
        raise ValueError(f"CSV 中没有有效的行情行: {path}")

    out = (
        out.set_index("date")
        .sort_index(kind="stable")
        [list(_REQUIRED_COLUMNS) + (["symbol"] if "symbol" in cols else [])]
    )
    out.index.name = "date"
    return out


def load_bars(
    csv_path: Union[str, Path],
    symbol: Optional[str] = None,
    last_n: Optional[int] = None,
) -> List[Bar]:
    """把真实 OHLCV CSV 转成 :class:`Bar` 列表（日期升序，index 从 0 重排）。

    参数
    ----
    csv_path : CSV 文件路径（只读）。
    symbol   : 标的代码；省略时取文件名主干（如 ``sh600519.csv`` → ``sh600519``）。
    last_n   : 可选，只取**最近 N 根** bar（升序排序后取尾部）；``None`` 取全部。
               N 超过总根数时返回全部；N <= 0 抛 ``ValueError``。

    返回的 bar 序列 ``index`` 恒为 ``0..len-1``，可直接喂给执行算法的
    ``schedule`` 与 :func:`~kairos_execution.broker.execute_children`。
    """
    frame = load_ohlcv_frame(csv_path)
    if last_n is not None:
        if last_n <= 0:
            raise ValueError(f"last_n 必须为正，收到 {last_n}")
        frame = frame.iloc[-int(last_n):]
    if symbol is None:
        symbol = Path(csv_path).stem
    return to_bars(frame, str(symbol))
