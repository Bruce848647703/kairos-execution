"""交易执行的核心数据类型：枚举、订单、成交、持仓、账户与行情 bar。

本模块只定义「数据结构 + 纯逻辑」，不涉及撮合或调度，方便被 oms / broker /
algos / tca 复用。所有价格、数量均用 float 表示；方向由 :class:`Side` 决定，
数量恒为正数。设计遵循「dataclass 优先、无全局状态、可复现」的原则。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Mapping, Optional

import numpy as np


class Side(Enum):
    """买卖方向。value 同时用作「带符号乘子」：买入 +1、卖出 -1。"""

    BUY = 1
    SELL = -1

    @property
    def sign(self) -> int:
        """返回方向符号（+1 买 / -1 卖），便于计算带符号的盈亏与滑点。"""
        return int(self.value)


class OrderType(Enum):
    """订单类型。

    - MARKET：市价单，按撮合 bar 的参考价成交。
    - LIMIT ：限价单，买在 low ≤ limit、卖在 high ≥ limit 时成交。
    - STOP  ：止损单，价格穿越触发价后转为市价成交。
    """

    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"


class OrderStatus(Enum):
    """订单生命周期状态（含状态机语义）。

    合法路径::

        PENDING ──accept──▶ ACCEPTED ──partial──▶ PARTIAL ──fill──▶ FILLED
           │                    │                    │
           │reject              │cancel              │cancel/fill
           ▼                    ▼                    ▼
        REJECTED            CANCELLED            CANCELLED/FILLED

    FILLED / CANCELLED / REJECTED 为终态，不可再迁移。
    """

    PENDING = "pending"      # 已提交，等待受理
    ACCEPTED = "accepted"    # 已受理，等待成交
    PARTIAL = "partial"      # 已部分成交
    FILLED = "filled"        # 全部成交（终态）
    CANCELLED = "cancelled"  # 已撤销（终态）
    REJECTED = "rejected"    # 被拒绝（终态）

    @property
    def is_active(self) -> bool:
        """是否处于「活动」状态（仍可继续成交或撤销）。"""
        return self in _ACTIVE_STATUSES

    @property
    def is_terminal(self) -> bool:
        """是否为终态（生命周期已结束）。"""
        return self in _TERMINAL_STATUSES


_ACTIVE_STATUSES = frozenset(
    {OrderStatus.PENDING, OrderStatus.ACCEPTED, OrderStatus.PARTIAL}
)
_TERMINAL_STATUSES = frozenset(
    {OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED}
)


@dataclass
class Bar:
    """单根行情 bar（OHLCV）。撮合与算法切片的最小市场数据单元。

    参数
    ----
    symbol : 标的代码。
    open/high/low/close : 开高低收价格。
    volume : 成交量（股/张），用于参与率约束与 VWAP 切片。
    index : bar 序号（>=0），-1 表示未指定。
    """

    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    index: int = -1

    @property
    def typical_price(self) -> float:
        """典型价 (H+L+C)/3，作为 bar 内均价的粗略近似。"""
        return (self.high + self.low + self.close) / 3.0

    def contains(self, price: float) -> bool:
        """价格是否落在本 bar 的 [low, high] 区间内。"""
        return self.low <= price <= self.high


@dataclass
class Order:
    """订单。数量为正数，方向由 :attr:`side` 决定。

    ``created_bar`` 记录订单被提交时所在的 bar 序号，用于撮合延迟计算；
    ``filled_qty`` / ``avg_fill_price`` 由 OMS 在成交时增量维护。
    """

    order_id: str
    symbol: str
    side: Side
    qty: float
    order_type: OrderType = OrderType.MARKET
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    parent_id: Optional[str] = None      # 子单指向父单，父单为 None
    created_bar: int = -1
    status: OrderStatus = OrderStatus.PENDING
    filled_qty: float = 0.0
    avg_fill_price: float = 0.0

    def __post_init__(self) -> None:
        if self.qty <= 0:
            raise ValueError(f"订单数量必须为正，收到 {self.qty}")
        if isinstance(self.side, str):     # 容错：允许传入 "BUY"/"SELL"
            self.side = Side[self.side.upper()]
        if isinstance(self.order_type, str):
            self.order_type = OrderType(self.order_type.lower())
        if self.order_type is OrderType.LIMIT and self.limit_price is None:
            raise ValueError("限价单必须提供 limit_price")
        if self.order_type is OrderType.STOP and self.stop_price is None:
            raise ValueError("止损单必须提供 stop_price")

    @property
    def remaining(self) -> float:
        """尚未成交的剩余数量。"""
        return self.qty - self.filled_qty

    @property
    def is_active(self) -> bool:
        """订单是否仍活动（未进入终态）。"""
        return self.status.is_active

    @property
    def is_terminal(self) -> bool:
        """订单是否已进入终态。"""
        return self.status.is_terminal

    @property
    def is_child(self) -> bool:
        """是否为某父单拆分出的子单。"""
        return self.parent_id is not None


@dataclass
class Fill:
    """成交回报。一次（部分）成交对应一条记录，不可变语义。"""

    fill_id: str
    order_id: str
    symbol: str
    side: Side
    qty: float
    price: float
    bar: int = -1
    commission: float = 0.0
    reference_price: float = 0.0   # 撮合参考价（滑点前），用于 TCA 分解
    slippage: float = 0.0          # 本次成交相对参考价的滑点（价格单位，带方向）
    parent_id: Optional[str] = None

    @property
    def notional(self) -> float:
        """成交金额（价格 × 数量）。"""
        return self.qty * self.price


@dataclass
class Position:
    """单标的持仓，维护数量、摊薄成本与已实现盈亏。"""

    symbol: str
    qty: float = 0.0
    avg_price: float = 0.0
    realized_pnl: float = 0.0

    def market_value(self, price: float) -> float:
        """按给定价格计算的持仓市值。"""
        return self.qty * float(price)

    def apply_fill(self, side: Side, fill_qty: float, exec_price: float) -> None:
        """把一笔成交并入持仓。

        买入抬高/摊薄平均成本；卖出按当前均价结算已实现盈亏。
        """
        sign = side.sign
        signed = sign * fill_qty
        new_qty = self.qty + signed
        if sign > 0:                                   # 买入：重算摊薄成本
            if abs(new_qty) > 1e-12:
                self.avg_price = (
                    self.qty * self.avg_price + fill_qty * exec_price
                ) / new_qty
        else:                                          # 卖出：实现盈亏
            self.realized_pnl += (self.avg_price - exec_price) * fill_qty
            if abs(new_qty) < 1e-12:
                self.avg_price = 0.0
        self.qty = new_qty


@dataclass
class Account:
    """资金账户：现金 + 多标的持仓。"""

    cash: float
    positions: Dict[str, Position] = field(default_factory=dict)

    def position(self, symbol: str) -> Position:
        """获取（必要时创建）某标的持仓。"""
        pos = self.positions.get(symbol)
        if pos is None:
            pos = Position(symbol)
            self.positions[symbol] = pos
        return pos

    def market_value(self, prices: Mapping[str, float]) -> float:
        """按价格表计算持仓总市值（缺价的标的按成本价近似计入）。"""
        mv = 0.0
        for sym, pos in self.positions.items():
            if pos.qty == 0:
                continue
            px = prices.get(sym, pos.avg_price)
            if px is None or (isinstance(px, float) and np.isnan(px)):
                px = pos.avg_price
            mv += pos.market_value(float(px))
        return mv

    def equity(self, prices: Mapping[str, float]) -> float:
        """账户权益 = 现金 + 持仓市值。"""
        return self.cash + self.market_value(prices)
