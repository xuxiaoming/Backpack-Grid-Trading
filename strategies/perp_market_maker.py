"""
永續合約做市策略模塊 - 旗艦專業版 v4.0
"""
from __future__ import annotations

import math
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any
from concurrent.futures import ThreadPoolExecutor

from logger import setup_logger
from strategies.market_maker import MarketMaker, format_balance
from utils.helpers import round_to_precision, round_to_tick_size, calculate_atr

logger = setup_logger("perp_market_maker")


class PerpetualMarketMaker(MarketMaker):
    """專業級永續合約做市策略：支持動態價差與實時倉位同步。"""

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
        # 1. 优先初始化所有私有变量，防止 AttributeError
        self._cached_net_position: Optional[float] = None
        self.unrealized_pnl: float = 0.0
        
        # 版本标识
        logger.info(">>> PerpetualMarketMaker 启动 [2026-01-13 旗舰版 v4.3.1] <<<")
        
        self.position_threshold = kwargs.pop("position_threshold", 0.1)
        self.leverage = kwargs.pop("leverage", 1.0)
        self.stop_loss = kwargs.pop("stop_loss", None)
        self.take_profit = kwargs.pop("take_profit", None)
        # 止盈触发阈值（百分比）
        self.profit_threshold_pct = kwargs.pop("profit_threshold", 0.0002)
        # 波动率熔断阈值 (百分比，如 0.05 代表 0.05%)
        self.volatility_ceiling = kwargs.pop("volatility_ceiling", None)
        # 趋势保护阈值 (百分比，如 0.08 代表 0.08%)
        self.trend_threshold_pct = kwargs.pop("trend_threshold", 0.0008)
        
        # ATR 轨道追踪
        self.historical_highs: List[float] = []
        self.historical_lows: List[float] = []
        self.historical_closes: List[float] = []
        self._current_window_high = 0.0
        self._current_window_low = float('inf')
        
        if "enable_rebalance" not in kwargs:
            kwargs["enable_rebalance"] = False
        
        # 2. 调用父类初始化
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

    def get_volatility(self, window=20) -> float:
        """获取当前波动率，优先从 WS 获取"""
        if hasattr(self, 'ws') and self.ws and hasattr(self.ws, 'get_volatility'):
            return self.ws.get_volatility(window)
        return 0.0

    def get_roc(self, window=10) -> float:
        """计算价格变化率 (Rate of Change)"""
        if len(self.historical_closes) < window:
            return 0.0
        start_price = self.historical_closes[-window]
        end_price = self.historical_closes[-1]
        return (end_price - start_price) / start_price

    def get_net_position(self) -> float:
        """优先使用缓存仓位（由 WS 实时更新），兜底才用 API"""
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
        """重载 WS 消息回调，实现秒级仓位同步"""
        # 调用父类处理订单更新等逻辑
        super().on_ws_message(stream, data)
        
        # 实时更新 ATR 所需的 OHLC 数据 (兼容 StandX 和 Backpack)
        if stream.startswith("bookTicker.") or stream == "ticker":
            # StandX 映射后为 'p', Backpack 映射后可能不同，但 MarketMaker 会处理
            # 这里直接从 data 获取价格，优先使用 common 字段
            price = float(data.get("p") or data.get("lastPrice") or 0)
            if price > 0:
                self._current_window_high = max(self._current_window_high, price)
                self._current_window_low = min(self._current_window_low, price)
        
        # 实时更新仓位缓存
        if stream == "account.positionUpdate":
            # logger.debug(f"[WS] 收到仓位更新消息: {data}")
            # data 通常是一个列表
            positions = data if isinstance(data, list) else [data]
            for pos in positions:
                if pos.get("symbol") == self.symbol:
                    new_net = float(pos.get("netQuantity", 0))
                    # 更新未实现盈亏，用于智能止盈
                    self.unrealized_pnl = float(pos.get("unrealizedPnl", 0.0))
                    
                    if self._cached_net_position != new_net:
                        # logger.info(f"⚡ [WS] 仓位实时同步: {self._cached_net_position} -> {new_net}")
                        self._cached_net_position = new_net

    def need_rebalance(self) -> bool:
        """永续合约的重平衡逻辑：基于持仓量与目标的偏差"""
        if not self.enable_rebalance:
            return False
            
        net = self.get_net_position()
        diff = abs(net - self.target_position)
        
        # 如果偏差超过阈值（position_threshold），则需要重平衡
        if diff > self.position_threshold:
            logger.info(f"⚖️ [重平衡检查] 当前持仓 {net:.4f}, 目标 {self.target_position:.4f}, 偏差 {diff:.4f} > 阈值 {self.position_threshold}")
            return True
        return False

    def calculate_prices(self):
        # 1. 更新 ATR 数据窗口
        mid_price = self.get_current_price()
        if mid_price:
            self.historical_highs.append(self._current_window_high if self._current_window_high > 0 else mid_price)
            self.historical_lows.append(self._current_window_low if self._current_window_low != float('inf') else mid_price)
            self.historical_closes.append(mid_price)
            # 重置当前窗口
            self._current_window_high = 0.0
            self._current_window_low = float('inf')
            # 保持列表长度
            if len(self.historical_closes) > 100:
                self.historical_highs.pop(0)
                self.historical_lows.pop(0)
                self.historical_closes.pop(0)

        # 2. 获取基准价格和波动率
        bid_price, ask_price = self.get_market_depth()
        current_price = (bid_price + ask_price) / 2 if (bid_price and ask_price) else mid_price
        if not current_price: return None, None

        # 3. 计算 ATR 和波动率状态
        atr = calculate_atr(self.historical_highs, self.historical_lows, self.historical_closes, window=14)
        vol = self.get_volatility()
        vol_status = "NORMAL"
        
        # 4. 动态价差计算 (Volatility Adaptive)
        volatility_factor = 1.0
        try:
            if vol > 0:
                # --- 波动率熔断逻辑 ---
                if self.volatility_ceiling and vol > self.volatility_ceiling:
                    vol_status = "HIGH_VOLATILITY"
                    logger.warning(f"🚨 [波动率熔断] 波动率 {vol:.4f}% > {self.volatility_ceiling:.4f}%")
                    return [], []
                
                # --- 优化：限制缩放系数，确保不跑出积分区 ---
                # 即使波动大，也最多只放大到 1.05 倍，确保 9.5bps 不会超过 10bps
                volatility_factor = max(0.8, min(1.05, vol / 0.0001))
        except: pass

        dynamic_spread = self.base_spread_percentage * volatility_factor
        
        # 5. 强化版偏移计算 (Aggressive Skew + Profit Boost)
        net = self.get_net_position()
        # ... 原有偏移计算逻辑 ...
        skew_ratio = max(-1.0, min(1.0, (net - self.target_position) / self.max_position))
        
        # --- 利润落袋加速逻辑 ---
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
        
        max_skew_percent = 0.004 
        skew_offset = current_price * max_skew_percent * self.inventory_skew * adjusted_skew_ratio

        # 6. 优化版阶梯挂单 (Pro Layering)
        adjusted_buys = []
        adjusted_sells = []
        
        half_spread_val = current_price * (dynamic_spread / 200)
        base_buy = current_price - half_spread_val - skew_offset
        base_sell = current_price + half_spread_val - skew_offset

        # 打印状态日志 (参考高手格式)
        logger.info(f"📊 [状态] 价格:{current_price:.2f} | 持仓:{net:.4f} | ATR:{atr:.4f} | 波动检查:{vol_status}")

        # --- 趋势卫士 (Trend Guard) ---
        roc = self.get_roc(window=10)
        filter_buys = False
        filter_sells = False
        if abs(roc) > self.trend_threshold_pct:
            if roc > 0:
                filter_sells = True
                logger.warning(f"📈 [趋势保护] 单边拉升 (ROC: {roc:.3%})，屏蔽卖单")
            else:
                filter_buys = True
                logger.warning(f"📉 [趋势保护] 单边下跌 (ROC: {roc:.3%})，屏蔽买单")

        for i in range(self.max_orders):
            if i < 3:
                gap = 0.0001 * i 
            else:
                gap = (0.0001 * 2) + (0.0003 * (i - 2)) 
                
            p_buy = base_buy - (current_price * gap)
            safe_buy = min(p_buy, bid_price) if bid_price else p_buy
            p_buy_final = round_to_tick_size(safe_buy, self.tick_size)
            # --- 强制约束：买单不能跑出 10 bps 积分区 ---
            max_buy_price = current_price * (1 - 0.0001) # 至少留 1 bps 距离
            min_buy_price = current_price * (1 - 0.00099) # 强制拉回到 9.9 bps
            if p_buy_final < min_buy_price:
                p_buy_final = round_to_tick_size(min_buy_price, self.tick_size)
            
            buy_bps = abs(p_buy_final - current_price) / current_price * 10000
            if not filter_buys:
                adjusted_buys.append(p_buy_final)
            
            p_sell = base_sell + (current_price * gap)
            safe_sell = max(p_sell, ask_price) if ask_price else p_sell
            p_sell_final = round_to_tick_size(safe_sell, self.tick_size)
            # --- 强制约束：卖单不能跑出 10 bps 积分区 ---
            max_sell_price = current_price * (1 + 0.00099) # 强制拉回到 9.9 bps
            if p_sell_final > max_sell_price:
                p_sell_final = round_to_tick_size(max_sell_price, self.tick_size)

            sell_bps = abs(p_sell_final - current_price) / current_price * 10000
            if not filter_sells:
                adjusted_sells.append(p_sell_final)
            
            if i == 0: # 只打印第一档的 BPS，保持日志清爽
                buy_log = f"{p_buy_final} ({buy_bps:.2f} bps)" if not filter_buys else "PAUSED"
                sell_log = f"{p_sell_final} ({sell_bps:.2f} bps)" if not filter_sells else "PAUSED"
                logger.info(f"🔹 [挂单预判] 买单:{buy_log} | 卖单:{sell_log}")

        return adjusted_buys, adjusted_sells

    def place_limit_orders(self):
        """下限价单（智能保持版：如果价格变化极小，则不撤单）"""
        self.check_ws_connection()
        
        # 1. 强制风控检查
        self.manage_positions()
        
        # 2. 计算最新目标价格
        buy_prices, sell_prices = self.calculate_prices()
        if buy_prices is None or sell_prices is None:
            return
            
        # 3. 智能保持判断 (优化版：放宽至 1.5 bps 以保住排队优先级)
        current_active_buys = [float(o.get('price', 0)) for o in self.active_buy_orders]
        current_active_sells = [float(o.get('price', 0)) for o in self.active_sell_orders]
        
        needs_replace = True
        mid_price = self.get_current_price()
        if mid_price and len(current_active_buys) == len(buy_prices) and len(current_active_sells) == len(sell_prices):
            if len(buy_prices) > 0:
                # 容忍度设为 1.5 bps，BTC 约 14 刀空间，不乱动，保住优先级
                tolerance = mid_price * 0.00015 
                buy_diff = abs(current_active_buys[0] - buy_prices[0])
                sell_diff = abs(current_active_sells[0] - sell_prices[0])
                
                if buy_diff <= tolerance and sell_diff <= tolerance:
                    needs_replace = False
                    # logger.debug(f"✨ [智能保持] 波动 {buy_diff:.2f} < 阈值 {tolerance:.2f}, 维持现有 Maker 订单")

        if not needs_replace:
            return

        # 4. 如果需要更新，则撤单重挂
        self.cancel_existing_orders()
        
        # 5. 确定下单数量
        qty = max(self.min_order_size, round_to_precision(self.order_quantity, self.base_precision))

        # 4. 这里的并发下单已经通过 open_position 实现了物理层 PostOnly
        with ThreadPoolExecutor(max_workers=10) as executor:
            # 这里的并发是为了极速占领订单簿
            futures = []
            for p in buy_prices:
                futures.append(executor.submit(self.open_long, qty, p))
            for p in sell_prices:
                futures.append(executor.submit(self.open_short, qty, p))
            
            for f in futures:
                res = f.result()
                if not (isinstance(res, dict) and "error" in res):
                    self.orders_placed += 1
                    # 只有成功的单子计入活跃列表
                    if "side" in res:
                        if res["side"] == "Bid": self.active_buy_orders.append(res)
                        else: self.active_sell_orders.append(res)

    def open_long(self, quantity, price=None, **kwargs):
        return self.open_position("Bid", quantity, price, **kwargs)

    def open_short(self, quantity, price=None, **kwargs):
        return self.open_position("Ask", quantity, price, **kwargs)

    def open_position(self, side, quantity, price=None, order_type="Limit", reduce_only=False, **kwargs):
        normalized_order_type = order_type.capitalize()
        qty = round_to_precision(abs(quantity), self.base_precision)
        if qty < self.min_order_size: return {"error": "too_small"}

        # 持仓上限检查（仅开仓单）
        if not reduce_only:
            net = self.get_net_position()
            if (side == "Bid" and net >= self.max_position) or (side == "Ask" and net <= -self.max_position):
                logger.warning(f"【持仓超限】当前 {net:.4f}，拒绝 {side} 开仓")
                return {"error": "max_reached"}

        order_details = {
            "orderType": normalized_order_type,
            "quantity": str(qty),
            "side": side,
            "symbol": self.symbol,
            "reduce_only": reduce_only,
            "leverage": self.leverage,
        }

        if normalized_order_type == "Limit":
            if price is None: return {"error": "no_price"}
            order_details["price"] = str(round_to_tick_size(price, self.tick_size))
            order_details["time_in_force"] = "POST_ONLY"
            order_details["post_only"] = True

        result = self.client.execute_order(order_details)
        
        # 自动重试逻辑（针对 PostOnly Taker）
        if isinstance(result, dict) and "error" in result and "POST_ONLY_TAKER" in str(result["error"]):
            new_price = price - self.tick_size if side == "Bid" else price + self.tick_size
            order_details["price"] = str(round_to_tick_size(new_price, self.tick_size))
            result = self.client.execute_order(order_details)

        if isinstance(result, dict) and "error" in result:
            # logger.error(f"下单失败: {result['error']}")
            pass
        else:
            # 补全返回信息以便统计
            result["side"] = side
            result["price"] = price
            result["quantity"] = qty
            
            order_id = result.get("id") or result.get("request_id") or "unknown"
            # 记录成功日志
            if not reduce_only:
                logger.info(f"✅ [Maker] {side} {qty} @ {price} | ID: {order_id}")
        
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

    def run(self, duration_seconds=3600, interval_seconds=60):
        super().run(duration_seconds, interval_seconds)
