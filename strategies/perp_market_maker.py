"""
永續合約做市策略模塊。
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
    """專為永續合約設計的做市策略。"""

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
        # 版本标识
        logger.info(">>> PerpetualMarketMaker 启动 [2026-01-12 深度加固版 v3.3] <<<")
        
        self.position_threshold = kwargs.pop("position_threshold", 0.1)
        self.leverage = kwargs.pop("leverage", 1.0)
        self.stop_loss = kwargs.pop("stop_loss", None)
        self.take_profit = kwargs.pop("take_profit", None)
        
        # 允许从外部参数（如 --enable-rebalance）开启重平衡
        if "enable_rebalance" not in kwargs:
            kwargs["enable_rebalance"] = False
        
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
        self._update_position_state()

    def get_net_position(self) -> float:
        try:
            result = self.client.get_positions(self.symbol)
            if not isinstance(result, list) or not result: return 0.0
            net = float(result[0].get("netQuantity", 0))
            return net
        except Exception as e:
            logger.error(f"获取仓位异常: {e}")
            return 0.0

    def _update_position_state(self, current_price: Optional[float] = None) -> None:
        net = self.get_net_position()
        self.position_state = {
            "net": net,
            "direction": "LONG" if net > 0 else "SHORT" if net < 0 else "FLAT",
            "max_position": self.max_position,
        }

    def manage_positions(self) -> bool:
        """风控减仓：不再使用市价单，改为使用带保护的限价单"""
        net = self.get_net_position()
        if abs(net) > self.max_position:
            excess = abs(net) - self.max_position
            logger.warning(f"【风控拦截】仓位 {abs(net):.4f} 超过限制 {self.max_position}，尝试以 Maker 方式减仓: {excess:.4f}")
            
            # 价格设置在对手盘价格之后，确保 Maker
            bid, ask = self.get_market_depth()
            price = (ask + self.tick_size) if net > 0 else (bid - self.tick_size)
            
            return self.open_position(
                side="Ask" if net > 0 else "Bid", 
                quantity=excess, 
                price=price,
                order_type="Limit", 
                reduce_only=True
            )
        return False

    def need_rebalance(self) -> bool:
        """永续合约重平衡：检查持仓是否偏离目标仓位超过阈值"""
        if not self.enable_rebalance:
            return False
        
        net = self.get_net_position()
        deviation = abs(net - self.target_position)
        
        if deviation > self.position_threshold:
            logger.info(f"永续重平衡检查: 当前持仓 {net:.4f}, 目标 {self.target_position:.4f}, 偏差 {deviation:.4f} > 阈值 {self.position_threshold}")
            return True
        return False

    def rebalance_position(self) -> bool:
        """永续合约重平衡：强制改为使用限价单（Maker）回归目标仓位"""
        net = self.get_net_position()
        diff = net - self.target_position
        
        if abs(diff) > self.min_order_size:
            side = "Ask" if diff > 0 else "Bid"
            qty = abs(diff)
            
            # 计算一个安全的 Maker 价格
            bid, ask = self.get_market_depth()
            if not bid or not ask: return False
            price = (ask + self.tick_size) if side == "Ask" else (bid - self.tick_size)
            
            logger.warning(f"【重平衡】尝试以 Maker 方式回归目标: {side} {qty:.4f} @ {price}")
            
            return self.open_position(
                side=side, 
                quantity=qty, 
                price=price, 
                order_type="Limit", 
                reduce_only=True
            )
        return False

    def calculate_prices(self):
        # 1. 获取基准价差价格（基类会返回 max_orders 数量的价格点）
        buy_prices, sell_prices = super().calculate_prices()
        if not buy_prices or not sell_prices: return buy_prices, sell_prices

        net = self.get_net_position()
        bid_price, ask_price = self.get_market_depth()
        current_price = (bid_price + ask_price) / 2 if (bid_price and ask_price) else self.get_current_price()
        if not current_price: return buy_prices, sell_prices

        # 2. 基础偏移计算
        skew_ratio = max(-1.0, min(1.0, net / self.max_position))
        max_skew_percent = 0.005 
        skew_offset = current_price * max_skew_percent * self.inventory_skew * skew_ratio

        # 3. 处理多档挂单 (Layering)
        # 每档单子之间拉开一个微小的额外间距 (0.01% - 0.02%)，形成阶梯
        layer_gap_percent = 0.0002  # 每档间隔 2 bps
        
        adjusted_buys = []
        for i, p in enumerate(buy_prices):
            # 原始价格 - 整体偏移 - 档位阶梯
            raw_p = p - skew_offset - (current_price * layer_gap_percent * i)
            # 物理边界保护
            safe_p = min(raw_p, bid_price) if bid_price else raw_p
            adjusted_buys.append(round_to_tick_size(safe_p, self.tick_size))

        adjusted_sells = []
        for i, p in enumerate(sell_prices):
            # 原始价格 - 整体偏移 + 档位阶梯
            raw_p = p - skew_offset + (current_price * layer_gap_percent * i)
            # 物理边界保护
            safe_p = max(raw_p, ask_price) if ask_price else raw_p
            adjusted_sells.append(round_to_tick_size(safe_p, self.tick_size))

        # 增加詳細日志
        if abs(net) >= 0.0001:
            logger.info(f"=== 多档价格计算 (Layers: {self.max_orders}) ===")
            logger.info(f"当前持仓: {net:.4f} | 偏移比例: {skew_ratio:.2%}")
            logger.info(f"首档买单: {adjusted_buys[0]:.2f} | 首档卖单: {adjusted_sells[0]:.2f}")

        return adjusted_buys, adjusted_sells

        return adjusted_buys, adjusted_sells

    def place_limit_orders(self):
        """下限价单（永续合约重写版：强制走 open_position 以确保 Maker 保护）"""
        self.check_ws_connection()
        
        # 1. 强制风控检查（已改为 Maker 模式）
        self.manage_positions()
        
        self.cancel_existing_orders()
        
        buy_prices, sell_prices = self.calculate_prices()
        if not buy_prices or not sell_prices:
            logger.error("無法計算訂單價格，跳過下單")
            return
        
        # 确定下单数量
        if self.order_quantity is not None:
            qty = max(self.min_order_size, round_to_precision(self.order_quantity, self.base_precision))
        else:
            # 如果没设数量，默认计算逻辑（参考基类）
            _, base_total = self.get_asset_balance(self.base_asset)
            _, quote_total = self.get_asset_balance(self.quote_asset)
            avg_price = (buy_prices[0] + sell_prices[0]) / 2
            allocation = min(0.05, 1.0 / (self.max_orders * 4))
            qty = max(self.min_order_size, round_to_precision((quote_total * allocation) / avg_price, self.base_precision))

        # 并发执行下单
        buy_count = 0
        with ThreadPoolExecutor(max_workers=self.max_orders) as executor:
            futures = []
            for p in buy_prices[:self.max_orders]:
                futures.append(executor.submit(self.open_long, qty, p))
            
            for f in futures:
                res = f.result()
                if not (isinstance(res, dict) and "error" in res):
                    buy_count += 1
                    self.active_buy_orders.append(res)
                    self.orders_placed += 1

        sell_count = 0
        with ThreadPoolExecutor(max_workers=self.max_orders) as executor:
            futures = []
            for p in sell_prices[:self.max_orders]:
                futures.append(executor.submit(self.open_short, qty, p))
            
            for f in futures:
                res = f.result()
                if not (isinstance(res, dict) and "error" in res):
                    sell_count += 1
                    self.active_sell_orders.append(res)
                    self.orders_placed += 1
                
        logger.info(f"共下單: {buy_count} 個買單, {sell_count} 個賣單")

    def open_long(self, quantity, price=None, **kwargs):
        return self.open_position("Bid", quantity, price, **kwargs)

    def open_short(self, quantity, price=None, **kwargs):
        return self.open_position("Ask", quantity, price, **kwargs)

    def open_position(self, side, quantity, price=None, order_type="Limit", reduce_only=False, **kwargs):
        normalized_order_type = order_type.capitalize()
        qty = round_to_precision(abs(quantity), self.base_precision)
        if qty < self.min_order_size: 
            return {"error": "too_small"}

        # 持仓上限检查（减仓单除外）
        if not reduce_only:
            net = self.get_net_position()
            if (side == "Bid" and net >= self.max_position) or (side == "Ask" and net <= -self.max_position):
                logger.warning(f"【下单拦截】持仓 {net:.4f} 已达上限 {self.max_position}，拒绝新开仓")
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
            # 核心：物理锁死 Maker 模式
            order_details["time_in_force"] = "POST_ONLY"
            order_details["post_only"] = True
            order_details["postOnly"] = True

        result = self.client.execute_order(order_details)
        
        # 针对 PostOnly Taker 的自动重试逻辑（远离价格中心）
        if isinstance(result, dict) and "error" in result and "POST_ONLY_TAKER" in str(result["error"]):
            logger.info(f"PostOnly 触发保护，调整 {side} 价格并重试...")
            new_price = price - self.tick_size if side == "Bid" else price + self.tick_size
            order_details["price"] = str(round_to_tick_size(new_price, self.tick_size))
            result = self.client.execute_order(order_details)

        if isinstance(result, dict) and "error" in result:
            logger.error(f"下单失败: {result['error']} ({side} @ {price})")
        else:
            order_id = result.get("id") or result.get("request_id") or "unknown"
            logger.info(f"✅ 下单成功 (Maker模式): {side} {qty} @ {price} | ID: {order_id}")
        
        return result

    def close_position(self, quantity=None, price=None, order_type="Market"):
        """主动平仓：同样改为 Limit 模式"""
        net = self.get_net_position()
        if abs(net) < self.min_order_size: return False
        
        bid, ask = self.get_market_depth()
        price = (ask + self.tick_size) if net > 0 else (bid - self.tick_size)
        
        return self.open_position("Ask" if net > 0 else "Bid", abs(net), price, "Limit", reduce_only=True)

    def run(self, duration_seconds=3600, interval_seconds=60):
        super().run(duration_seconds, interval_seconds)
