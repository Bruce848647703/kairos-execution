"""交易成本分析（TCA, Transaction Cost Analysis）。

以「到达价 / 决策价」为基准，把一次执行的成本拆解为若干可读指标：滑点、
延迟成本、相对市场 VWAP 的价差、市场冲击（简单估计）、佣金，并汇总为
实现差额（Implementation Shortfall）。所有指标既提供**纯函数**（便于单测），
也提供 :func:`analyze` 把它们组装成 :class:`TCAReport`。

约定：成本类指标以「基点(bps)」表示，**正数=不利/有成本**，负数=有利。
方向由 :class:`~kairos_execution.types.Side` 决定（买入 +1、卖出 -1）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence

import pandas as pd

from .types import Bar, Fill, Order, Side


def _side_sign(side) -> int:
    """把 Side 枚举或整数统一成方向符号（+1/-1）。"""
    if isinstance(side, Side):
        return side.sign
    return int(side)


def average_fill_price(fills: Sequence[Fill]) -> float:
    """成交均价（按成交量加权，即成交 VWAP）；无成交返回 0.0。"""
    total_qty = sum(f.qty for f in fills)
    if total_qty <= 0:
        return 0.0
    return sum(f.qty * f.price for f in fills) / total_qty


def total_filled(fills: Sequence[Fill]) -> float:
    """累计成交量。"""
    return float(sum(f.qty for f in fills))


def total_commission(fills: Sequence[Fill]) -> float:
    """累计佣金。"""
    return float(sum(f.commission for f in fills))


def slippage_bps(arrival_price: float, average_fill: float, side) -> float:
    """滑点（基点）：成交均价相对到达价的偏离，带方向。

    买入成交均价高于到达价 → 正（不利）；卖出成交均价低于到达价 → 正（不利）。
    公式::

        bps = side_sign × (average_fill − arrival) / arrival × 1e4
    """
    if arrival_price <= 0:
        return 0.0
    return _side_sign(side) * (average_fill - arrival_price) / arrival_price * 1e4


def fill_ratio(filled_qty: float, ordered_qty: float) -> float:
    """成交率 = 成交量 / 下单量（0~1，可能因超量而 >1 的异常在此不裁剪）。"""
    if ordered_qty <= 0:
        return 0.0
    return float(filled_qty) / float(ordered_qty)


def benchmark_vwap(bars: Sequence[Bar]) -> float:
    """基准 VWAP：给定 bar 序列的成交量加权平均收盘价。"""
    vol = sum(b.volume for b in bars)
    if vol <= 0:
        return 0.0
    return sum(b.close * b.volume for b in bars) / vol


def realized_spread_bps(average_fill: float, benchmark_price: float, side) -> float:
    """已实现价差（基点）：成交均价相对基准（如市场 VWAP）的偏离，带方向。

    度量「相对市场自然成交价的择时/流动性成本」。正值=比基准更贵（买）或更便宜地卖出。
    """
    if benchmark_price <= 0:
        return 0.0
    return _side_sign(side) * (average_fill - benchmark_price) / benchmark_price * 1e4


def market_impact_bps(
    filled_qty: float, total_volume: float, coefficient_bps: float = 10.0
) -> float:
    """市场冲击（基点，简单估计）：平方根参与率模型。

    ``impact ≈ coefficient_bps × sqrt(成交量 / 市场总量)``。参与率越高、冲击越大；
    这是一个量纲清晰的粗估，仅用于横向对比不同执行方案的冲击大小。
    """
    if total_volume <= 0 or filled_qty <= 0:
        return 0.0
    participation = filled_qty / total_volume
    return float(coefficient_bps) * math.sqrt(participation)


def delay_cost_bps(arrival_price: float, decision_price: float, side) -> float:
    """延迟成本（基点）：到达价相对决策价的漂移，带方向。

    度量「从决策到实际到达市场之间」价格移动带来的成本。
    """
    if decision_price <= 0:
        return 0.0
    return _side_sign(side) * (arrival_price - decision_price) / decision_price * 1e4


@dataclass
class TCAReport:
    """一次执行的 TCA 汇总报告。

    字段说明
    --------
    ordered_qty / filled_qty / fill_ratio : 下单量、成交量、成交率。
    arrival_price / decision_price        : 到达价、决策价（基准）。
    average_fill_price                    : 成交均价（成交 VWAP）。
    slippage_bps                          : 成交均价 vs 到达价（bps）。
    delay_cost_bps                        : 到达价 vs 决策价（bps）。
    realized_spread_bps                   : 成交均价 vs 市场基准 VWAP（bps）。
    market_impact_bps                     : 平方根参与率冲击估计（bps）。
    commission                            : 佣金合计（货币）。
    implementation_shortfall              : 实现差额（货币，含佣金）。
    implementation_shortfall_bps          : 实现差额 / (决策价×下单量)（bps）。
    num_fills                             : 成交笔数。
    """

    symbol: str
    side: Side
    ordered_qty: float
    filled_qty: float
    fill_ratio: float
    arrival_price: float
    decision_price: float
    average_fill_price: float
    slippage_bps: float
    delay_cost_bps: float
    realized_spread_bps: float
    market_impact_bps: float
    commission: float
    implementation_shortfall: float
    implementation_shortfall_bps: float
    num_fills: int

    def to_frame(self) -> pd.DataFrame:
        """整理成两列（metric/value）DataFrame，便于打印。"""
        d = {
            "symbol": self.symbol,
            "side": self.side.name,
            "ordered_qty": self.ordered_qty,
            "filled_qty": self.filled_qty,
            "fill_ratio": self.fill_ratio,
            "arrival_price": self.arrival_price,
            "decision_price": self.decision_price,
            "average_fill_price": self.average_fill_price,
            "slippage_bps": self.slippage_bps,
            "delay_cost_bps": self.delay_cost_bps,
            "realized_spread_bps": self.realized_spread_bps,
            "market_impact_bps": self.market_impact_bps,
            "commission": self.commission,
            "implementation_shortfall": self.implementation_shortfall,
            "implementation_shortfall_bps": self.implementation_shortfall_bps,
            "num_fills": self.num_fills,
        }
        return pd.DataFrame({"metric": list(d.keys()), "value": list(d.values())})


def analyze(
    parent: Order,
    fills: Sequence[Fill],
    bars: Optional[Sequence[Bar]] = None,
    arrival_price: Optional[float] = None,
    decision_price: Optional[float] = None,
    impact_coef_bps: float = 10.0,
) -> TCAReport:
    """汇总一次执行的 TCA 报告。

    参数
    ----
    parent         : 父订单（提供下单量、方向、标的）。
    fills          : 该父单（及其子单）的全部成交回报。
    bars           : 执行窗口的行情 bar（用于市场基准 VWAP 与冲击估计）。
    arrival_price  : 到达价；省略则取 ``bars[0].open``，再否则取成交均价。
    decision_price : 决策价；省略则等于到达价。
    impact_coef_bps: 平方根冲击模型的系数（bps）。
    """
    only = [f for f in fills if f.parent_id == parent.order_id or f.order_id == parent.order_id]
    if not only:
        only = list(fills)

    filled = total_filled(only)
    avg_fill = average_fill_price(only)
    commission = total_commission(only)

    if arrival_price is None:
        if bars:
            arrival_price = float(bars[0].open)
        else:
            arrival_price = avg_fill if avg_fill > 0 else 0.0
    if decision_price is None:
        decision_price = arrival_price

    ratio = fill_ratio(filled, parent.qty)
    slip = slippage_bps(arrival_price, avg_fill, parent.side)
    delay = delay_cost_bps(arrival_price, decision_price, parent.side)

    if bars:
        bench = benchmark_vwap(bars)
        spread = realized_spread_bps(avg_fill, bench, parent.side)
        total_vol = sum(b.volume for b in bars)
        impact = market_impact_bps(filled, total_vol, impact_coef_bps)
    else:
        spread = 0.0
        impact = 0.0

    sign = _side_sign(parent.side)
    # 实现差额（货币）：相对决策价的成本 + 佣金
    is_cash = sign * (avg_fill - decision_price) * filled + commission
    denom = decision_price * parent.qty
    is_bps = (is_cash / denom * 1e4) if denom > 0 else 0.0

    return TCAReport(
        symbol=parent.symbol,
        side=parent.side,
        ordered_qty=float(parent.qty),
        filled_qty=filled,
        fill_ratio=ratio,
        arrival_price=float(arrival_price),
        decision_price=float(decision_price),
        average_fill_price=avg_fill,
        slippage_bps=slip,
        delay_cost_bps=delay,
        realized_spread_bps=spread,
        market_impact_bps=impact,
        commission=commission,
        implementation_shortfall=is_cash,
        implementation_shortfall_bps=is_bps,
        num_fills=len(only),
    )
