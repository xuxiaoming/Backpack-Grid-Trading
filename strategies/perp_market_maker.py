"""
永續合約做市策略模塊。
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any

from logger import setup_logger
from strategies.market_maker import MarketMaker, format_balance
from utils.helpers import round_to_precision, round_to_tick_size

logger = setup_logger("perp_market_maker")


class PerpetualMarketMaker(MarketMaker):
    """專為永續合約設計的做市策略。"""

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        symbol: str,
        target_position: float = 0.0,
        max_position: float = 1.0,
        position_threshold: float = 0.1,
        inventory_skew: float = 0.0,
        leverage: float = 1.0,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        exchange: str = 'backpack',
        exchange_config: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> None:
        kwargs.setdefault("enable_rebalance", False)
        super().__init__(
            api_key=api_key,
            secret_key=secret_key,
            symbol=symbol,
            exchange=exchange,
            exchange_config=exchange_config,
            **kwargs,
        )

        self.target_position = abs(target_position)
        self.max_position = max(abs(max_position), self.min_order_size)
        self.position_threshold = max(position_threshold, self.min_order_size)
        self.inventory_skew = max(0.0, min(1.0, inventory_skew))
        self.leverage = max(1.0, leverage)
        self.stop_loss = abs(stop_loss) if stop_loss not in (None, 0) else None
        self.take_profit = abs(take_profit) if take_profit and take_profit > 0 else None
        
        self.position_state: Dict[str, Any] = {"net": 0.0, "avg_entry": 0.0, "direction": "FLAT", "unrealized": 0.0}
        self.total_volume_quote = 0.0
        self.session_total_volume_quote = 0.0

        self._update_position_state()

    def get_net_position(self) -> float:
        """取得目前的永續合約淨倉位。"""
        try:
            result = self.client.get_positions(self.symbol)
            if isinstance(result, dict) and "error" in result:
                return 0.0
            if not isinstance(result, list) or not result:
                return 0.0
            return float(result[0].get("netQuantity", 0))
        except:
            return 0.0

    def _update_position_state(self, current_price: Optional[float] = None) -> None:
        net = self.get_net_position()
        if current_price is None:
            current_price = self.get_current_price()
        
        self.position_state = {
            "net": net,
            "direction": "LONG" if net > 0 else "SHORT" if net < 0 else "FLAT",
            "target": self.target_position,
            "max_position": self.max_position,
            "current_price": current_price or 0.0,
        }

    def manage_positions(self) -> bool:
        """風控管理：僅在超過最大持倉時才執行強制平倉。"""
        net = self.get_net_position()
        current_size = abs(net)
        
        if current_size > self.max_position:
            excess = current_size - self.max_position
            logger.warning(f"【風控】持倉 {current_size} 超過上限 {self.max_position}，市價平掉多餘部分 {excess}")
            return self.close_position(quantity=excess, order_type="Market")
        
        return False

    def calculate_prices(self):
        """核心邏輯：計算掛單價格並執行微小偏移。"""
        buy_prices, sell_prices = super().calculate_prices()
        if not buy_prices or not sell_prices:
            return buy_prices, sell_prices

        net = self.get_net_position()
        bid_price, ask_price = self.get_market_depth()
        current_price = (bid_price + ask_price) / 2 if (bid_price and ask_price) else self.get_current_price()
        if not current_price:
            return buy_prices, sell_prices

        if abs(net) < (self.min_order_size / 100) or abs(net) < 0.0001:
            return buy_prices, sell_prices

        skew_ratio = max(-1.0, min(1.0, net / self.max_position))
        max_skew_percent = 0.005 
        skew_offset = current_price * max_skew_percent * self.inventory_skew * skew_ratio

        adjusted_buys = [round_to_tick_size(p - skew_offset, self.tick_size) for p in buy_prices]
        adjusted_sells = [round_to_tick_size(p - skew_offset, self.tick_size) for p in sell_prices]

        logger.info(f"=== 價格計算 ===")
        logger.info(f"當前倉位: {net:.4f} | 偏移比例: {skew_ratio:.2%} | 偏移金額: {skew_offset:.2f}")
        logger.info(f"原始報價: 買 {buy_prices[0]:.2f} | 賣 {sell_prices[0]:.2f}")
        logger.info(f"調整報價: 買 {adjusted_buys[0]:.2f} | 賣 {adjusted_sells[0]:.2f}")

        if adjusted_buys[0] >= adjusted_sells[0]:
            return buy_prices, sell_prices

        return adjusted_buys, adjusted_sells

    def open_position(self, side, quantity, price=None, order_type="Limit", reduce_only=False, **kwargs):
        normalized_order_type = order_type.capitalize()
        qty = round_to_precision(abs(quantity), self.base_precision)
        if qty < self.min_order_size: return {"error": "too_small"}

        # ⚠️ 硬拦截：如果是非减仓单，且当前仓位已达上限，禁止下单
        if not reduce_only:
            net = self.get_net_position()
            if (side == "Bid" and net >= self.max_position) or (side == "Ask" and net <= -self.max_position):
                logger.warning(f"【拦截】当前持仓 {net:.4f} 已达上限 {self.max_position}，禁止继续开仓")
                return {"error": "max_position_reached"}

        order_details = {
            "orderType": normalized_order_type,
            "quantity": str(qty),
            "side": side,
            "symbol": self.symbol,
            "reduceOnly": reduce_only,
        }
        if normalized_order_type == "Limit":
            order_details["price"] = str(round_to_tick_size(price, self.tick_size))
            order_details["postOnly"] = True 

        result = self.client.execute_order(order_details)
        if isinstance(result, dict) and "error" in result:
            logger.error(f"下單失敗: {result['error']}")
        else:
            logger.info(f"下單成功: {side} {qty} @ {price or 'Market'}")
        return result

    def close_position(self, quantity=None, price=None, order_type="Market"):
        net = self.get_net_position()
        if abs(net) < self.min_order_size: return False
        
        order_side = "Ask" if net > 0 else "Bid"
        qty = abs(net) if quantity is None else min(abs(quantity), abs(net))
        
        return self.open_position(side=order_side, quantity=qty, price=price, order_type=order_type, reduce_only=True)
    def run(self, duration_seconds=3600, interval_seconds=60):
        super().run(duration_seconds, interval_seconds)

