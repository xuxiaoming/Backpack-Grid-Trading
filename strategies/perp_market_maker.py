"""
永續合約做市策略模塊 - 旗艦專業版 v4.5
"""
from __future__ import annotations

import math
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any
from concurrent.futures import ThreadPoolExecutor

from logger import setup_logger
from strategies.market_maker import MarketMaker, format_balance
from utils.helpers import round_to_precision, round_to_tick_size

logger = setup_logger("perp_market_maker")


class PerpetualMarketMaker(MarketMaker):
    """專業級永續合約做市策略：全幣種通用版。支持動態參數配置。"""

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        symbol: str,
        target_position: float = 0.0,
        max_position: float = 1.0,
        inventory_skew: float = 0.0,
        **kwargs,
    ) -> None:
        # 1. 优先初始化所有私有变量
        self._cached_net_position: Optional[float] = None
        self.unrealized_pnl: float = 0.0
        
        # 版本标识
        logger.info(">>> PerpetualMarketMaker 启动 [2026-01-14 全币种通用版 v4.5] <<<")
        
        # 2. 提取配置参数（提供 BTC 级别的安全默认值）
        self.position_threshold = kwargs.pop("position_threshold", 0.1)
        self.leverage = kwargs.pop("leverage", 1.0)
        self.stop_loss = kwargs.pop("stop_loss", None)
        self.take_profit = kwargs.pop("take_profit", None)
        
        # 止盈触发阈值
        self.profit_threshold_pct = kwargs.pop("profit_threshold", 0.0002)
        # 趋势感应阈值
        self.trend_threshold = kwargs.pop("trend_threshold", 0.0015)
        # 最大偏移限额
        self.max_skew_limit = kwargs.pop("max_skew_limit", 0.006)
        # 波动率基准分母
        self.volatility_scale = kwargs.pop("volatility_scale", 0.00008)
        
        if "enable_rebalance" not in kwargs:
            kwargs["enable_rebalance"] = False
        
        # 3. 调用父类初始化
        super().__init__(
            api_key=api_key,
            secret_key=secret_key,
            symbol=symbol,
            **kwargs,
        )

        self.target_position = abs(target_position)
        self.max_position = max(abs(max_position), self.min_order_size)
        self.inventory_skew = max(0.0, min(1.0, inventory_skew))
        self.position_state: Dict[str, Any] = {"net": 0.0, "direction": "FLAT"}

    def get_net_position(self) -> float:
        if self._cached_net_position is not None:
            return self._cached_net_position
        try:
            result = self.client.get_positions(self.symbol)
            if not isinstance(result, list) or not result: return 0.0
            net = float(result[0].get("netQuantity", 0))
            self._cached_net_position = net
            return net
        except Exception as e:
            logger.error(f"获取仓位异常: {e}")
            return 0.0

    def on_ws_message(self, stream: str, data: Any):
        super().on_ws_message(stream, data)
        if stream == "account.positionUpdate":
            positions = data if isinstance(data, list) else [data]
            for pos in positions:
                if pos.get("symbol") == self.symbol:
                    new_net = float(pos.get("netQuantity", 0))
                    self.unrealized_pnl = float(pos.get("unrealizedPnl", 0.0))
                    if self._cached_net_position != new_net:
                        self._cached_net_position = new_net

    def need_rebalance(self) -> bool:
        if not self.enable_rebalance: return False
        net = self.get_net_position()
        diff = abs(net - self.target_position)
        if diff > self.position_threshold:
            logger.info(f"⚖️ [重平衡检查] 当前持仓 {net:.4f}, 目标 {self.target_position:.4f}, 偏差 {diff:.4f} > 阈值 {self.position_threshold}")
            return True
        return False

    def calculate_prices(self):
        # 1. 获取基准价格和波动率
        bid_price, ask_price = self.get_market_depth()
        current_price = (bid_price + ask_price) / 2 if (bid_price and ask_price) else self.get_current_price()
        if not current_price: return None, None

        # 2. 增强型波动率保护 (使用可配置的分母)
        volatility_factor = 1.0
        try:
            vol = self.get_volatility()
            if vol > 0:
                volatility_factor = max(0.8, min(4.0, vol / self.volatility_scale))
        except: pass

        dynamic_spread = self.base_spread_percentage * volatility_factor
        
        # 3. 趋势感知逻辑 (使用可配置阈值)
        trend_side = "NONE"
        try:
            if hasattr(self.ws, 'historical_prices') and len(self.ws.historical_prices) >= 5:
                recent_change = (self.ws.historical_prices[-1] / self.ws.historical_prices[-5]) - 1
                if recent_change > self.trend_threshold:
                    trend_side = "UP"
                elif recent_change < -self.trend_threshold:
                    trend_side = "DOWN"
        except: pass

        # 4. 强化版偏移计算
        net = self.get_net_position()
        skew_ratio = max(-1.0, min(1.0, (net - self.target_position) / self.max_position))
        
        profit_boost = 0.0
        try:
            unrealized_pnl = getattr(self, 'unrealized_pnl', 0.0)
            if abs(net) > 0:
                position_value = abs(net) * current_price
                current_profit_pct = unrealized_pnl / position_value if position_value > 0 else 0
                if current_profit_pct > self.profit_threshold_pct:
                    profit_boost = min(1.0, (current_profit_pct / (self.profit_threshold_pct * 5)))
        except: pass

        skew_power = 1.2
        effective_skew_ratio = skew_ratio * (1.0 + profit_boost)
        effective_skew_ratio = max(-1.0, min(1.0, effective_skew_ratio))
        adjusted_skew_ratio = (abs(effective_skew_ratio) ** skew_power) * (1 if effective_skew_ratio > 0 else -1)
        
        if trend_side == "UP": adjusted_skew_ratio = max(adjusted_skew_ratio, 0.3) 
        elif trend_side == "DOWN": adjusted_skew_ratio = min(adjusted_skew_ratio, -0.3)

        # 偏移计算使用可配置的上限
        skew_offset = current_price * self.max_skew_limit * self.inventory_skew * adjusted_skew_ratio

        # 5. 优化版阶梯挂单
        adjusted_buys = []
        adjusted_sells = []
        half_spread_val = current_price * (dynamic_spread / 200)
        base_buy = current_price - half_spread_val - skew_offset
        base_sell = current_price + half_spread_val - skew_offset

        for i in range(self.max_orders):
            if i < 3: gap = 0.0001 * i 
            else: gap = (0.0001 * 2) + (0.0003 * (i - 2)) 
                
            p_buy = base_buy - (current_price * gap)
            safe_buy = min(p_buy, bid_price) if bid_price else p_buy
            adjusted_buys.append(round_to_tick_size(safe_buy, self.tick_size))
            
            if trend_side == "UP" and i >= 3: continue
            if trend_side == "DOWN" and i >= 3: continue

            p_sell = base_sell + (current_price * gap)
            safe_sell = max(p_sell, ask_price) if ask_price else p_sell
            adjusted_sells.append(round_to_tick_size(safe_sell, self.tick_size))

        if trend_side != "NONE" or abs(net) >= self.max_position * 0.2 or volatility_factor > 1.2:
            logger.warning(f"🚨 [全币种监控] 方向:{trend_side} | 波动:{volatility_factor:.2f}x | 偏移:{adjusted_skew_ratio:.1%}")

        return adjusted_buys, adjusted_sells

    def place_limit_orders(self):
        self.check_ws_connection()
        self.manage_positions()
        self.cancel_existing_orders()
        buy_prices, sell_prices = self.calculate_prices()
        if not buy_prices or not sell_prices: return
        qty = max(self.min_order_size, round_to_precision(self.order_quantity, self.base_precision))
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = []
            for p in buy_prices: futures.append(executor.submit(self.open_long, qty, p))
            for p in sell_prices: futures.append(executor.submit(self.open_short, qty, p))
            for f in futures:
                res = f.result()
                if not (isinstance(res, dict) and "error" in res):
                    self.orders_placed += 1
                    if "side" in res:
                        if res["side"] == "Bid": self.active_buy_orders.append(res)
                        else: self.active_sell_orders.append(res)

    def open_long(self, quantity, price=None, **kwargs): return self.open_position("Bid", quantity, price, **kwargs)
    def open_short(self, quantity, price=None, **kwargs): return self.open_position("Ask", quantity, price, **kwargs)

    def open_position(self, side, quantity, price=None, order_type="Limit", reduce_only=False, **kwargs):
        normalized_order_type = order_type.capitalize()
        qty = round_to_precision(abs(quantity), self.base_precision)
        if qty < self.min_order_size: return {"error": "too_small"}
        if not reduce_only:
            net = self.get_net_position()
            if (side == "Bid" and net >= self.max_position) or (side == "Ask" and net <= -self.max_position):
                logger.warning(f"【持仓超限】当前 {net:.4f}，拒绝 {side} 开仓")
                return {"error": "max_reached"}
        order_details = {
            "orderType": normalized_order_type, "quantity": str(qty), "side": side, "symbol": self.symbol,
            "reduce_only": reduce_only, "leverage": self.leverage,
        }
        if normalized_order_type == "Limit":
            if price is None: return {"error": "no_price"}
            order_details["price"] = str(round_to_tick_size(price, self.tick_size))
            order_details["time_in_force"] = "POST_ONLY"
            order_details["post_only"] = True
        result = self.client.execute_order(order_details)
        if isinstance(result, dict) and "error" in result and "POST_ONLY_TAKER" in str(result["error"]):
            new_price = price - self.tick_size if side == "Bid" else price + self.tick_size
            order_details["price"] = str(round_to_tick_size(new_price, self.tick_size))
            result = self.client.execute_order(order_details)
        if not isinstance(result, dict) or "error" not in result:
            result["side"] = side; result["price"] = price; result["quantity"] = qty
            order_id = result.get("id") or result.get("request_id") or "unknown"
            if not reduce_only: logger.info(f"✅ [Maker] {side} {qty} @ {price} | ID: {order_id}")
        return result

    def manage_positions(self) -> bool:
        net = self.get_net_position()
        if abs(net) > self.max_position:
            excess = abs(net) - self.max_position
            bid, ask = self.get_market_depth()
            price = (ask + self.tick_size) if net > 0 else (bid - self.tick_size)
            logger.warning(f"⚠️ [风控] 超仓 {net:.4f}，Maker 减仓: {excess:.4f}")
            return self.open_position(side="Ask" if net > 0 else "Bid", quantity=excess, price=price, reduce_only=True)
        return False

    def rebalance_position(self) -> bool:
        net = self.get_net_position()
        diff = net - self.target_position
        if abs(diff) > self.position_threshold:
            bid, ask = self.get_market_depth()
            side = "Ask" if diff > 0 else "Bid"
            price = (ask + self.tick_size) if side == "Ask" else (bid - self.tick_size)
            logger.warning(f"⚖️ [重平衡] 偏差 {diff:.4f}，Maker 调回目标")
            return self.open_position(side=side, quantity=abs(diff), price=price, reduce_only=True)
        return False

    def run(self, duration_seconds=3600, interval_seconds=60): super().run(duration_seconds, interval_seconds)
