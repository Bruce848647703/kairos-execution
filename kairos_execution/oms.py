"""自研内存订单管理系统（OMS）。

职责：维护订单簿与订单状态机、记录成交回报、保存完整事件日志，并提供按 id /
父单 / 活动状态的查询。OMS **只负责订单生命周期**，不做撮合、不动账户资金
——那些交给 :mod:`kairos_execution.broker`。这种「关注点分离」让状态机可以
被独立、确定性地测试。

状态机的非法迁移（如对已成交订单再次成交、撤单）会抛出
:class:`IllegalTransitionError`，从而把 bug 暴露在最早的位置。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from .types import Fill, Order, OrderStatus, Side


class IllegalTransitionError(Exception):
    """订单状态机的非法迁移异常。"""


# 合法迁移表：键为当前状态，值为允许迁移到的目标状态集合。
_TRANSITIONS: Dict[OrderStatus, frozenset] = {
    OrderStatus.PENDING: frozenset(
        {OrderStatus.ACCEPTED, OrderStatus.REJECTED, OrderStatus.CANCELLED}
    ),
    OrderStatus.ACCEPTED: frozenset(
        {OrderStatus.PARTIAL, OrderStatus.FILLED, OrderStatus.CANCELLED}
    ),
    OrderStatus.PARTIAL: frozenset(
        {OrderStatus.PARTIAL, OrderStatus.FILLED, OrderStatus.CANCELLED}
    ),
    OrderStatus.FILLED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
}

_TOL = 1e-9


@dataclass
class OrderEvent:
    """事件日志中的一条记录，描述一次状态迁移或动作。"""

    seq: int
    order_id: str
    action: str                       # submit/accept/reject/cancel/fill/partial_fill
    from_status: OrderStatus
    to_status: OrderStatus
    qty: float = 0.0
    price: float = 0.0
    bar: int = -1
    reason: str = ""

    def as_dict(self) -> dict:
        """转成普通 dict，便于打印或转 DataFrame。"""
        return {
            "seq": self.seq,
            "order_id": self.order_id,
            "action": self.action,
            "from_status": self.from_status.value,
            "to_status": self.to_status.value,
            "qty": self.qty,
            "price": self.price,
            "bar": self.bar,
            "reason": self.reason,
        }


class OrderManagementSystem:
    """内存 OMS：订单状态机 + 活动订单簿 + 事件日志。

    典型用法（手动驱动状态机）::

        oms = OrderManagementSystem()
        oms.submit(order)                       # PENDING
        oms.accept(order.order_id)              # ACCEPTED
        oms.partial_fill(order.order_id, 3, 100.0, bar=1)  # PARTIAL
        oms.fill(order.order_id, 100.1, bar=2)  # FILLED（吃掉剩余）
    """

    def __init__(self) -> None:
        self._orders: Dict[str, Order] = {}     # 保持插入顺序
        self._fills: List[Fill] = []
        self._events: List[OrderEvent] = []
        self._fill_seq: int = 0
        self._event_seq: int = 0
        self._current_bar: int = -1

    # ------------------------------------------------------------------
    # 查询接口
    # ------------------------------------------------------------------
    @property
    def event_log(self) -> List[OrderEvent]:
        """完整事件日志（按发生顺序）。"""
        return list(self._events)

    @property
    def current_bar(self) -> int:
        """OMS 记录的当前 bar 序号（由外部撮合推进）。"""
        return self._current_bar

    def set_current_bar(self, bar_index: int) -> None:
        """推进 OMS 的当前 bar（供 broker 在每根 bar 调用）。"""
        self._current_bar = int(bar_index)

    def get(self, order_id: str) -> Order:
        """按 id 获取订单，不存在则抛 ``KeyError``。"""
        try:
            return self._orders[order_id]
        except KeyError:
            raise KeyError(f"订单不存在: {order_id}") from None

    def has(self, order_id: str) -> bool:
        """是否存在指定 id 的订单。"""
        return order_id in self._orders

    def all_orders(self) -> List[Order]:
        """全部订单（按提交顺序）。"""
        return list(self._orders.values())

    def active_orders(self, symbol: Optional[str] = None) -> List[Order]:
        """活动订单（PENDING/ACCEPTED/PARTIAL），可按标的过滤。"""
        out = [o for o in self._orders.values() if o.is_active]
        if symbol is not None:
            out = [o for o in out if o.symbol == symbol]
        return out

    def historical_orders(self) -> List[Order]:
        """历史（终态）订单：FILLED/CANCELLED/REJECTED。"""
        return [o for o in self._orders.values() if o.is_terminal]

    def children_of(self, parent_id: str) -> List[Order]:
        """某父单拆分出的全部子单。"""
        return [o for o in self._orders.values() if o.parent_id == parent_id]

    def fills(self, order_id: Optional[str] = None) -> List[Fill]:
        """全部成交回报；给定 order_id 时只返回该订单的成交。"""
        if order_id is None:
            return list(self._fills)
        return [f for f in self._fills if f.order_id == order_id]

    def events_for(self, order_id: str) -> List[OrderEvent]:
        """某订单相关的全部事件。"""
        return [e for e in self._events if e.order_id == order_id]

    # ------------------------------------------------------------------
    # 状态机动作
    # ------------------------------------------------------------------
    def submit(self, order: Order) -> Order:
        """登记一张新订单，初始状态为 PENDING。

        若 order_id 重复或数量非法则报错。
        """
        if order.order_id in self._orders:
            raise ValueError(f"订单 id 重复: {order.order_id}")
        if order.qty <= 0:
            raise ValueError(f"订单数量必须为正: {order.qty}")
        order.status = OrderStatus.PENDING
        order.filled_qty = 0.0
        order.avg_fill_price = 0.0
        self._orders[order.order_id] = order
        self._record("submit", order, OrderStatus.PENDING, OrderStatus.PENDING)
        return order

    def accept(self, order_id: str) -> Order:
        """受理订单：PENDING → ACCEPTED。"""
        order = self.get(order_id)
        self._transition(order, OrderStatus.ACCEPTED, "accept")
        return order

    def reject(self, order_id: str, reason: str = "") -> Order:
        """拒绝订单：PENDING → REJECTED（终态）。"""
        order = self.get(order_id)
        self._transition(order, OrderStatus.REJECTED, "reject", reason=reason)
        return order

    def cancel(self, order_id: str, reason: str = "") -> Order:
        """撤销订单：活动态 → CANCELLED（终态）。

        对终态订单撤单会抛 :class:`IllegalTransitionError`。
        """
        order = self.get(order_id)
        self._transition(order, OrderStatus.CANCELLED, "cancel", reason=reason)
        return order

    def partial_fill(
        self, order_id: str, qty: float, price: float, bar: int = -1
    ) -> Fill:
        """部分成交：吃进 ``qty``（应 < 剩余量），状态转 PARTIAL。

        若 ``qty`` 恰好等于剩余量，则状态直接转 FILLED（合法且符合语义）。
        """
        return self._apply_fill(order_id, qty, price, bar, action="partial_fill")

    def fill(
        self,
        order_id: str,
        price: float,
        qty: Optional[float] = None,
        bar: int = -1,
    ) -> Fill:
        """（全部）成交：默认吃掉剩余数量，状态转 FILLED。

        也可显式传入 ``qty``；若 ``qty`` 小于剩余量则退化为部分成交（PARTIAL）。
        """
        order = self.get(order_id)
        target = order.remaining if qty is None else qty
        return self._apply_fill(order_id, target, price, bar, action="fill")

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------
    def _transition(
        self,
        order: Order,
        to_status: OrderStatus,
        action: str,
        qty: float = 0.0,
        price: float = 0.0,
        bar: int = -1,
        reason: str = "",
    ) -> None:
        """校验并执行一次状态迁移，同时写入事件日志。"""
        allowed = _TRANSITIONS.get(order.status, frozenset())
        if to_status not in allowed:
            raise IllegalTransitionError(
                f"非法状态迁移: {order.order_id} {order.status.value} → "
                f"{to_status.value} (action={action})"
            )
        from_status = order.status
        order.status = to_status
        self._record(
            action, order, from_status, to_status,
            qty=qty, price=price, bar=bar, reason=reason,
        )

    def _apply_fill(
        self, order_id: str, qty: float, price: float, bar: int, action: str
    ) -> Fill:
        """成交的核心逻辑：校验状态与数量、增量更新订单、生成成交回报。"""
        order = self.get(order_id)
        # 只有已受理（ACCEPTED）或部分成交（PARTIAL）的订单才能继续成交；
        # PENDING（未受理）与任意终态都会被状态机拒绝。
        if order.status not in (OrderStatus.ACCEPTED, OrderStatus.PARTIAL):
            raise IllegalTransitionError(
                f"订单 {order_id} 处于 {order.status.value}，不能成交 (action={action})"
            )
        if qty <= 0:
            raise ValueError(f"成交数量必须为正: {qty}")
        if qty > order.remaining + _TOL:
            raise ValueError(
                f"成交数量 {qty} 超过剩余量 {order.remaining} (order={order_id})"
            )
        if price <= 0:
            raise ValueError(f"成交价格必须为正: {price}")

        eff_bar = bar if bar >= 0 else self._current_bar
        # 增量更新已成交量与摊薄成交价
        new_filled = order.filled_qty + qty
        order.avg_fill_price = (
            order.filled_qty * order.avg_fill_price + qty * price
        ) / new_filled
        order.filled_qty = new_filled

        # 剩余量归零则转 FILLED，否则转 PARTIAL；统一走迁移校验与事件记录。
        to_status = OrderStatus.FILLED if order.remaining <= _TOL else OrderStatus.PARTIAL
        self._transition(
            order, to_status, action, qty=qty, price=price, bar=eff_bar
        )

        self._fill_seq += 1
        fill = Fill(
            fill_id=f"{order_id}-F{self._fill_seq}",
            order_id=order_id,
            symbol=order.symbol,
            side=order.side,
            qty=qty,
            price=price,
            bar=eff_bar,
            parent_id=order.parent_id,
        )
        self._fills.append(fill)
        return fill

    def _record(
        self,
        action: str,
        order: Order,
        from_status: OrderStatus,
        to_status: OrderStatus,
        qty: float = 0.0,
        price: float = 0.0,
        bar: int = -1,
        reason: str = "",
    ) -> None:
        """追加一条事件日志。"""
        self._event_seq += 1
        self._events.append(
            OrderEvent(
                seq=self._event_seq,
                order_id=order.order_id,
                action=action,
                from_status=from_status,
                to_status=to_status,
                qty=qty,
                price=price,
                bar=bar,
                reason=reason,
            )
        )
