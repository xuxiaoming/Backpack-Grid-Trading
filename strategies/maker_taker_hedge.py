"""Maker掛單 + Taker對沖策略模組。"""
from __future__ import annotations

import time
from typing import Any, Dict, Optional, Tuple

from logger import setup_logger
from strategies.market_maker import MarketMaker, format_balance
from strategies.perp_market_maker import PerpetualMarketMaker
from utils.helpers import round_to_precision, round_to_tick_size

logger = setup_logger("maker_taker_hedge")


class _MakerTakerHedgeMixin:
    """封裝 Maker 掛單 + Taker 對沖的核心實作。"""

    def __init__(self, *args: Any, hedge_label: str = "現貨", **kwargs: Any) -> None:
        kwargs.pop("max_orders", None)
        kwargs.pop("enable_rebalance", None)
        kwargs.pop("base_asset_target_percentage", None)
        kwargs.pop("rebalance_threshold", None)

        kwargs["max_orders"] = 1
        kwargs["enable_rebalance"] = False

        self._hedge_label = hedge_label
        self._hedge_residuals: Dict[str, float] = {"Bid": 0.0, "Ask": 0.0}
        self._hedge_position_reference: float = 0.0
        self._hedge_flat_tolerance = 1e-8
        
        # 跨交易所對沖支持
        self.hedge_client = kwargs.pop("hedge_client", None)
        self.hedge_symbol = kwargs.pop("hedge_symbol", None) or getattr(self, "symbol", None)
        
        # 套利統計
        self.arb_stats = {
            "total_trades": 0,
            "total_volume": 0.0,
            "est_profit_usd": 0.0,
            "start_time": time.time()
        }
        
        # 本地倉位追蹤
        self._local_position: Optional[float] = None
        self._local_position_synced: bool = False
        self._position_sync_interval: float = 60.0  # 每 60 秒強制同步一次
        self._last_position_sync_ts: float = 0.0

        self._request_intervals: Dict[str, float] = {
            "limit": 0.35,
            "market": 0.45,
            "position": 1.0,
        }
        self._last_request_ts: Dict[str, float] = {key: 0.0 for key in self._request_intervals}
        self._rate_limit_retries = 4
        self._rate_limit_backoff = 0.6
        self._rate_limit_max_backoff = 5.0

        super().__init__(*args, **kwargs)

        self.max_orders = 1

        self._hedge_flat_tolerance = max(getattr(self, "min_order_size", 0.0) / 10, 1e-8)
        self._initialize_hedge_reference_position()

        logger.info("初始化 Maker-Taker 對沖策略 (%s)", self._hedge_label)

    # ------------------------------------------------------------------
    # 下單與倉位管理
    # ------------------------------------------------------------------
    def get_hedge_market_depth(self) -> Tuple[Optional[float], Optional[float]]:
        """獲取對沖交易所的買一/賣一價格（增加 OrderBook 備選方案）。"""
        if not self.hedge_client:
            return None, None
        try:
            # 1. 首先嘗試獲取 Ticker
            ticker = self.hedge_client.get_ticker(self.hedge_symbol)
            if isinstance(ticker, dict) and "error" not in ticker:
                bid = ticker.get("bidPrice") or ticker.get("bid_price")
                ask = ticker.get("askPrice") or ticker.get("ask_price")
                if bid and ask:
                    return float(bid), float(ask)

            # 2. 如果 Ticker 沒拿到買賣價，嘗試獲取 OrderBook
            logger.debug(f"Ticker 未能獲取買賣價，嘗試從 OrderBook 獲取 ({self.hedge_symbol})")
            depth = self.hedge_client.get_order_book(self.hedge_symbol, limit=5)
            if isinstance(depth, dict) and "error" not in depth:
                bids = depth.get("bids", [])
                asks = depth.get("asks", [])
                if bids and asks:
                    # bids/asks 格式通常是 [[price, qty], ...]
                    bid = bids[0][0] if isinstance(bids[0], (list, tuple)) else bids[0].get("price")
                    ask = asks[0][0] if isinstance(asks[0], (list, tuple)) else asks[0].get("price")
                    return float(bid), float(ask)
            
            logger.warning(f"無法從 Ticker 或 OrderBook 獲取對沖交易所 ({self.hedge_symbol}) 的有效行情")
            return None, None
        except Exception as e:
            logger.error(f"獲取對沖交易所行情異常: {e}")
            return None, None

    def calculate_basis_info(self) -> Dict[str, Any]:
        """計算兩端交易所之間的價差信息。"""
        # 獲取 Maker 端價格 (StandX)
        m_bid, m_ask = self.get_market_depth()
        # 獲取對沖端價格 (Backpack)
        h_bid, h_ask = self.get_hedge_market_depth()

        # 獲取即時持倉信息
        m_pos = self._fetch_current_position_reference() or 0.0
        h_pos = self._fetch_hedge_exchange_position() or 0.0
        total_delta = (m_pos - self._hedge_position_reference) + (h_pos - self._hedge_exchange_position_reference)

        logger.info("================ 账户持仓快照 ================")
        logger.info("StandX 持仓: %.6f | Backpack 持仓: %.6f", m_pos, h_pos)
        logger.info("累计对冲偏差 (Net Delta): %.6f", total_delta)
        logger.info("--------------------------------------------")

        if not all([m_bid, m_ask, h_bid, h_ask]):
            return {}

        m_mid = (m_bid + m_ask) / 2
        h_mid = (h_bid + h_ask) / 2

        # 基差 (Basis) = (StandX - Backpack) / Backpack
        basis = (m_mid - h_mid) / h_mid * 100
        
        # 獲取 StandX 資金費率
        funding_rate = 0.0
        try:
            if hasattr(self.client, "query_funding_rates"):
                funding_data = self.client.query_funding_rates(self.symbol)
                if isinstance(funding_data, list) and len(funding_data) > 0:
                    funding_rate = float(funding_data[0].get("funding_rate", 0)) * 100
        except Exception as e:
            logger.debug(f"獲取資金費率失敗: {e}")

        # 使用用戶設置的 spread 作為盈利閾值，如果沒設置則默認 0.11%
        target_profit = getattr(self, "base_spread_percentage", 0.11)
        if target_profit is None or target_profit <= 0:
            target_profit = 0.11
        
        return {
            "maker_mid": m_mid,
            "hedge_mid": h_mid,
            "basis_pct": basis,
            "funding_rate_pct": funding_rate,
            "is_profitable": abs(basis + funding_rate) > target_profit,
            "cost_threshold": target_profit
        }

    def place_limit_orders(self) -> None:
        """根據價差監控決定是否掛單。"""
        self.check_ws_connection()
        self.cancel_existing_orders()

        # 獲取價差信息
        basis_info = self.calculate_basis_info()
        if not basis_info:
            logger.warning("無法獲取完整價差信息，跳過本輪")
            return

        # 獲取價格信息，用於日誌顯示
        m_bid, m_ask = self.get_market_depth()
        if m_bid and m_ask:
            self.last_bid_price = m_bid
            self.last_ask_price = m_ask

        basis = basis_info["basis_pct"]
        funding = basis_info.get("funding_rate_pct", 0)
        total_edge = basis + funding # 賣出套利的總邊際 (StandX高於Backpack)
        buy_edge = -basis - funding # 買入套利的總邊際 (StandX低於Backpack)
        
        cost = basis_info["cost_threshold"]
        
        logger.info(">>> 价差监控 | 卖出套利边际: %.4f%% | 买入套利边际: %.4f%% | 成本线: %.2f%%", 
                    total_edge, buy_edge, cost)

        # 定期輸出統計摘要
        now = time.time()
        if hasattr(self, "last_summary_time") and now - self.last_summary_time > 300: # 每5分鐘一次
            elapsed = (now - self.arb_stats["start_time"]) / 3600
            logger.info("================ 套利盈亏汇总 ================")
            logger.info("运行时间: %.2f 小时", elapsed)
            logger.info("成交笔数: %d 次", self.arb_stats["total_trades"])
            logger.info("累计交易额: %.2f USD", self.arb_stats["total_volume"])
            logger.info("预估累计收益: %.4f USD", self.arb_stats["est_profit_usd"])
            logger.info("============================================")
            self.last_summary_time = now
        elif not hasattr(self, "last_summary_time"):
            self.last_summary_time = now

        # 獲取 Maker 端盤口 (StandX)
        m_bid, m_ask = self.get_market_depth()
        if m_bid is None or m_ask is None:
            return

        # 核心套利過濾邏輯
        # 只要有一邊邊際覆蓋了成本（或接近成本），就允許掛單
        can_buy = buy_edge > (cost - 0.02) # 留 2bps 緩衝
        can_sell = total_edge > (cost - 0.02)

        buy_price = round_to_tick_size(m_bid, self.tick_size)
        sell_price = round_to_tick_size(m_ask, self.tick_size)

        self.active_buy_orders = []
        self.active_sell_orders = []

        # 決定掛單數量
        calc_buy_qty, calc_sell_qty = self._determine_order_sizes(buy_price, sell_price)

        # 只有當買入有利潤時才掛買單
        if can_buy and calc_buy_qty and calc_buy_qty >= self.min_order_size:
            buy_order = self._build_limit_order(side="Bid", price=buy_price, quantity=calc_buy_qty)
            result = self._submit_order(buy_order, slot="limit")
            if not (isinstance(result, dict) and "error" in result):
                logger.info("✅ 發現買入套利空間，掛出買單: %s, 數量: %s (postOnly保護已開啟)", 
                            format_balance(buy_price), format_balance(calc_buy_qty))
                self.active_buy_orders.append(result)
        elif not can_buy:
            logger.debug("買入套利空間不足 (%.4f%% < %.2f%%)，不掛買單", buy_edge, cost)

        # 只有當賣出有利潤時才掛賣單
        if can_sell and calc_sell_qty and calc_sell_qty >= self.min_order_size:
            sell_order = self._build_limit_order(side="Ask", price=sell_price, quantity=calc_sell_qty)
            result = self._submit_order(sell_order, slot="limit")
            if not (isinstance(result, dict) and "error" in result):
                logger.info("✅ 發現賣出套利空間，掛出賣單: %s, 數量: %s (postOnly保護已開啟)", 
                            format_balance(sell_price), format_balance(calc_sell_qty))
                self.active_sell_orders.append(result)
        elif not can_sell:
            logger.debug("賣出套利空間不足 (%.4f%% < %.2f%%)，不掛賣單", total_edge, cost)

        if not can_buy and not can_sell:
            logger.info("⏸ 當前基差無套利空間，暫停掛單等待機會...")

    def _determine_order_sizes(self, buy_price: float, ask_price: float) -> Tuple[Optional[float], Optional[float]]:
        """根據餘額決定單筆買/賣單量。"""

        if self.order_quantity is not None:
            quantity = max(
                self.min_order_size,
                round_to_precision(self.order_quantity, self.base_precision),
            )
            return quantity, quantity

        base_available, base_total = self.get_asset_balance(self.base_asset)
        quote_available, quote_total = self.get_asset_balance(self.quote_asset)

        reference_price = ask_price if ask_price else buy_price
        if reference_price <= 0:
            return None, None

        allocation = 0.05  # 使用總資金的5%
        quote_budget = quote_total * allocation
        base_budget = base_total * allocation

        if quote_budget <= 0 or base_budget <= 0:
            logger.warning("餘額不足，無法掛出Maker訂單")
            return None, None

        buy_qty = round_to_precision(quote_budget / reference_price, self.base_precision)
        sell_qty = round_to_precision(base_budget, self.base_precision)

        buy_qty = max(self.min_order_size, buy_qty)
        sell_qty = max(self.min_order_size, sell_qty)

        if quote_available < buy_qty * reference_price:
            logger.info(
                "可用報價資產不足 (%.8f)，將依賴自動贖回",
                quote_available,
            )
        if base_available < sell_qty:
            logger.info(
                "可用基礎資產不足 (%.8f)，將依賴自動贖回",
                base_available,
            )

        return buy_qty, sell_qty

    # ------------------------------------------------------------------
    # 成交後置處理
    # ------------------------------------------------------------------
    def _after_fill_processed(self, fill_info: Dict[str, Any]) -> None:
        """所有成交後立即以市價對沖。"""

        super()._after_fill_processed(fill_info)

        def _to_bool(value: Any) -> Optional[bool]:
            if isinstance(value, bool):
                return value
            if value in (None, "", "None"):
                return None
            try:
                return str(value).lower() in {"true", "1", "yes"}
            except Exception:
                return None

        # 獲取成交詳情
        side = fill_info.get("side")
        quantity = float(fill_info.get("quantity", 0) or 0)
        price = float(fill_info.get("price", 0) or 0)
        maker_flag = None
        for key in ("is_maker", "maker", "isMaker", "m"):
            if key in fill_info:
                maker_flag = fill_info.get(key)
                break
        is_maker = _to_bool(maker_flag)
        if is_maker is False:
            logger.debug("忽略 Taker 成交事件，無需對沖")
            return
        
        if not side or quantity <= 0:
            logger.warning("成交資訊不完整，跳過對沖")
            return
            
        logger.info(f"處理Maker成交：{side} {quantity}@{price}")
        
        # 更新統計數據
        self.arb_stats["total_trades"] += 1
        trade_value = quantity * price
        self.arb_stats["total_volume"] += trade_value
        
        # 估算這筆成交的利潤 (簡化版：基差 * 成交額 / 100)
        basis_info = self.calculate_basis_info()
        if basis_info and "basis_pct" in basis_info:
            basis = abs(basis_info["basis_pct"] + basis_info.get("funding_rate_pct", 0))
            profit = trade_value * (basis - basis_info["cost_threshold"]) / 100
            self.arb_stats["est_profit_usd"] += max(0, profit)

        # 先根據 Maker 成交更新本地倉位追蹤
        # Maker Bid（買入）成交 = 倉位增加，Maker Ask（賣出）成交 = 倉位減少
        self._update_local_position_from_fill(side, quantity)
        
        # 使用更新後的本地追蹤倉位
        current_position = self._get_tracked_position()
        if current_position is None:
            logger.warning("本地倉位未初始化，嘗試同步 API")
            current_position = self._sync_position_from_api()
            if current_position is None:
                logger.error("無法獲取當前倉位，對沖失敗")
                return

        logger.info(f"當前追蹤倉位：{current_position}")

        net_delta = current_position - self._hedge_position_reference
        if abs(net_delta) <= self._hedge_flat_tolerance:
            logger.info("倉位已在參考水位附近，跳過對沖")
            self._hedge_residuals["Bid"] = 0.0
            self._hedge_residuals["Ask"] = 0.0
            return

        # 根據實際倉位差來決定對沖方向和數量
        # 倉位 > 參考 = 需要賣出(Ask)，倉位 < 參考 = 需要買入(Bid)
        hedge_side = "Ask" if net_delta > 0 else "Bid"
        hedge_qty = round_to_precision(abs(net_delta), self.base_precision)

        if hedge_qty < self.min_order_size:
            # 對沖量太小，累積到下次
            self._hedge_residuals[hedge_side] = abs(net_delta)
            logger.info(
                "對沖目標 %.8f 低於最小下單量，累積至下次 (殘留 %.8f)",
                abs(net_delta),
                abs(net_delta),
            )
            return

        logger.info(
            "偵測到倉位偏移 %.8f，準備以市價對沖 %s %s",
            net_delta,
            format_balance(hedge_qty),
            hedge_side,
        )
        residual_delta = self._execute_taker_hedge(hedge_side, hedge_qty, current_position=current_position)

        if residual_delta is None:
            # 對沖提交失敗，保留完整目標量
            self._hedge_residuals[hedge_side] = target_quantity
            return

        self._hedge_residuals["Bid"] = 0.0
        self._hedge_residuals["Ask"] = 0.0

        if abs(residual_delta) <= self._hedge_flat_tolerance:
            logger.info("市價對沖已完成，倉位回到參考水位")
            return

        residual_side = "Ask" if residual_delta > 0 else "Bid"
        residual_amount = round_to_precision(abs(residual_delta), self.base_precision)
        self._hedge_residuals[residual_side] = residual_amount
        logger.warning(
            "市價對沖後仍剩餘倉位 %.8f，記錄為後續殘量 (方向=%s)",
            residual_amount,
            residual_side,
        )

    def _execute_taker_hedge(
        self,
        side: str,
        quantity: float,
        *,
        current_position: Optional[float] = None,
    ) -> Optional[float]:
        """提交市價單完成對沖，並回傳剩餘倉位差值。"""

        attempt_side = side
        remaining_quantity = round_to_precision(quantity, self.base_precision)
        last_delta: Optional[float] = None

        # 如果是跨交易所對沖，我們直接下單
        if self.hedge_client:
            order = {
                "orderType": "Market",
                "quantity": str(remaining_quantity),
                "side": attempt_side,
                "symbol": self.hedge_symbol,
            }
            target_client = self.hedge_client
            target_exchange = target_client.get_exchange_name().lower()

            if target_exchange == "backpack":
                order["timeInForce"] = "IOC"
                # Spot 對沖不需要其它參數
            
            logger.info(f"🚀 [對沖執行] 正在 {target_exchange.upper()} 以市價 {attempt_side} {remaining_quantity} 對沖")
            result = target_client.execute_order(order)
            
            if isinstance(result, dict) and ("id" in result or "orderId" in result or "uuid" in result):
                logger.info(f"✅ 跨交易所對沖訂單已提交: {result.get('id') or result.get('orderId')}")
                # 簡單等待同步
                time.sleep(1.0)
                self._fetch_hedge_exchange_position() # 觸发一次同步
                return 0.0
            else:
                logger.error(f"❌ 跨交易所對沖失敗: {result}")
                return remaining_quantity

        # --- 以下是同交易所對沖（原有邏輯） ---
        current_delta = self._calculate_position_delta(current_position=current_position)
        if current_delta is not None:
            if abs(current_delta) <= self._hedge_flat_tolerance:
                latest_position = current_position if current_position is not None else self._fetch_current_position_reference()
                if latest_position is not None:
                    self._hedge_position_reference = latest_position
                logger.info("目前倉位已接近參考水位，無需對沖")
                return 0.0

            attempt_side = "Ask" if current_delta > 0 else "Bid"
            remaining_quantity = round_to_precision(abs(current_delta), self.base_precision)
            logger.debug(
                "以實際倉位差 %.8f 重新設定對沖方向為 %s", current_delta, attempt_side
            )

        for attempt in range(1, 4):
            if remaining_quantity < self.min_order_size:
                logger.info(
                    "剩餘對沖量 %.8f 低於最小下單量，停止提交", remaining_quantity
                )
                break

            order = {
                "orderType": "Market",
                "quantity": str(remaining_quantity),
                "side": attempt_side,
                "symbol": self.hedge_symbol if self.hedge_client else self.symbol,
            }

            # 根據目標交易所設置特定參數
            target_client = self.hedge_client if self.hedge_client else self.client
            target_exchange = target_client.get_exchange_name().lower()

            if target_exchange == "backpack":
                order["timeInForce"] = "IOC"
                order["autoLendRedeem"] = True
                order["autoLend"] = True

            # 只有當對沖目標是永續合約時才使用 reduceOnly
            # 注意：如果跨交易所對沖（如 StandX Perp -> Backpack Spot），對沖側是 Spot，不能用 reduceOnly
            if self.hedge_client:
                # 這裡簡單判斷對沖交易對是否包含 PERP 或 USD-PERP 等
                is_hedge_perp = any(x in self.hedge_symbol.upper() for x in ["PERP", "-USD"])
                if is_hedge_perp:
                    order["reduceOnly"] = True
            elif isinstance(self, PerpetualMarketMaker):
                order["reduceOnly"] = True

            logger.info(
                "提交市價對沖訂單 [%s]: %s %s (第 %d 次嘗試)",
                target_exchange.upper(),
                attempt_side,
                format_balance(remaining_quantity),
                attempt,
            )
            result = self._request_with_backoff(slot, target_client.execute_order, order)
            if isinstance(result, dict) and "error" in result:
                logger.error(f"市價對沖失敗: {result['error']}")
                # 對沖失敗，強制同步 API 校正本地追蹤
                if not self.hedge_client:
                    self._sync_position_from_api()
                return None

            logger.info("市價對沖訂單已提交: %s", result.get("id", "未知ID"))

            # 計算預期倉位變化：Ask(賣出)=-quantity, Bid(買入)=+quantity
            expected_change = -remaining_quantity if attempt_side == "Ask" else remaining_quantity
            last_delta = self._poll_position_delta(expected_change=expected_change)
            if last_delta is None:
                logger.warning("無法確認倉位變化，保留殘量待下次對沖")
                return None

            if abs(last_delta) <= self._hedge_flat_tolerance:
                # 對沖成功，更新參考倉位（使用本地追蹤，避免額外 API 請求）
                if self._local_position is not None:
                    self._hedge_position_reference = self._local_position
                logger.info("對沖成功，倉位已回到參考水位")
                return 0.0

            attempt_side = "Ask" if last_delta > 0 else "Bid"
            remaining_quantity = round_to_precision(abs(last_delta), self.base_precision)
            logger.warning(
                "市價對沖後仍有倉位差 %.8f，將再次以市價 %s 對沖",
                abs(last_delta),
                attempt_side,
            )

        return last_delta

    def _poll_position_delta(self, expected_change: float = 0.0) -> Optional[float]:
        """等待並確認倉位變化，對沖後從 API 驗證實際倉位。
        
        Args:
            expected_change: 預期的倉位變化量（正=買入，負=賣出）
        """
        # 等待訂單處理時間
        time.sleep(1.0)
        
        # 對沖後必須從 API 確認實際倉位，避免追蹤偏差
        # 這是唯一需要請求 API 的關鍵時刻
        actual_position = self._sync_position_from_api()
        if actual_position is None:
            logger.warning("無法從 API 確認倉位，使用本地預估")
            # 備用：使用本地追蹤 + 預期變化
            if self._local_position is not None:
                self._local_position += expected_change
                actual_position = self._local_position
            else:
                return None
        
        delta = actual_position - self._hedge_position_reference
        logger.debug("倉位差值: %.8f (實際 %.8f, 參考 %.8f)", delta, actual_position, self._hedge_position_reference)
        return delta

    def _initialize_hedge_reference_position(self) -> None:
        """初始化倉位參考水位和本地追蹤。"""

        reference = self._fetch_current_position_reference()
        if reference is None:
            reference = 0.0
        self._hedge_position_reference = reference
        
        # 如果是跨交易所對沖，還需要記錄對沖交易所的初始倉位
        if self.hedge_client:
            hedge_ref = self._fetch_hedge_exchange_position()
            if hedge_ref is None:
                hedge_ref = 0.0
            self._hedge_exchange_position_reference = hedge_ref
            logger.info("跨交易所對沖初始化: Maker端參考=%.8f, Hedge端參考=%.8f", reference, hedge_ref)
        
        # 同時初始化本地追蹤
        self._local_position = reference
        self._local_position_synced = True
        self._last_position_sync_ts = time.monotonic()
        if not self.hedge_client:
            logger.info("對沖參考倉位初始化為 %.8f（本地追蹤已同步）", reference)

    def _fetch_hedge_exchange_position(self) -> Optional[float]:
        """獲取對沖交易所的當前倉位。"""
        if not self.hedge_client:
            return None
        try:
            # 這裡需要根據對沖交易所是 Spot 還是 Perp 來獲取倉位
            is_perp = any(x in self.hedge_symbol.upper() for x in ["PERP", "-USD"])
            if is_perp:
                positions = self.hedge_client.get_positions(self.hedge_symbol)
                if isinstance(positions, list) and positions:
                    pos = positions[0]
                    for field in ["netQuantity", "size", "position_size", "amount"]:
                        if field in pos:
                            return float(pos[field] or 0)
                return 0.0
            else:
                # Spot - 獲取基礎資產餘額 (同時檢查普通餘額和抵押品)
                base_asset = self.hedge_symbol.split('_')[0] if '_' in self.hedge_symbol else self.hedge_symbol.split('-')[0]
                total_h_pos = 0.0
                
                # 1. 檢查普通餘額 (Capital)
                try:
                    balances = self.hedge_client.get_balances()
                    if isinstance(balances, dict):
                        # 情況 A: 直接 Key 匹配
                        if base_asset in balances:
                            info = balances[base_asset]
                            total_h_pos += float(info.get("available") or info.get("total") or 0) if isinstance(info, dict) else float(info or 0)
                        # 情況 B: 遍歷匹配
                        else:
                            for k, v in balances.items():
                                if k.upper() == base_asset.upper():
                                    total_h_pos += float(v.get("available") or v.get("total") or 0) if isinstance(v, dict) else float(v or 0)
                                    break
                except Exception as e:
                    logger.debug(f"獲取普通餘額異常: {e}")

                # 2. 檢查抵押品餘額 (Backpack 交易賬户)
                try:
                    collateral_data = self.hedge_client.get_collateral()
                    if isinstance(collateral_data, dict) and "collateral" in collateral_data:
                        for item in collateral_data["collateral"]:
                            if item.get("symbol") == base_asset:
                                total_h_pos += float(item.get("totalQuantity") or item.get("availableQuantity") or 0)
                                break
                except Exception as e:
                    logger.debug(f"獲取抵押品餘額異常: {e}")

                if total_h_pos == 0:
                    logger.debug(f"Hedge 端資產 {base_asset} 讀取為 0 (已檢查 Capital 和 Collateral)")
                
                return total_h_pos
        except Exception as e:
            logger.error(f"獲取對沖交易所倉位異常: {e}")
            return None

    def _get_tracked_position(self) -> Optional[float]:
        """獲取本地追蹤的倉位，必要時自動同步。"""
        now = time.monotonic()
        
        # 檢查是否需要強制同步（超過 60 秒）
        if self._local_position_synced and (now - self._last_position_sync_ts) > self._position_sync_interval:
            logger.info("定期同步：重新從 API 獲取倉位")
            self._sync_position_from_api()
        
        return self._local_position
    
    def _sync_position_from_api(self) -> Optional[float]:
        """從 API 同步倉位到本地追蹤。"""
        position = self._fetch_current_position_reference()
        if position is not None:
            self._local_position = position
            self._local_position_synced = True
            self._last_position_sync_ts = time.monotonic()
            logger.debug("倉位已從 API 同步: %.8f", position)
        return position
    
    def _update_local_position_from_fill(self, side: str, quantity: float) -> None:
        """根據成交更新本地追蹤倉位。
        
        Args:
            side: 成交方向 ("Bid" = 買入, "Ask" = 賣出)
            quantity: 成交數量
        """
        if self._local_position is None:
            return
            
        if side == "Bid":
            self._local_position += quantity
        elif side == "Ask":
            self._local_position -= quantity
        
        logger.debug("成交更新本地倉位: %s %.8f -> 當前 %.8f", side, quantity, self._local_position)

    def _calculate_position_delta(
        self,
        *,
        current_position: Optional[float] = None,
    ) -> Optional[float]:
        """計算當前倉位相對參考水位的差值（跨交易所時計算總 Delta）。"""

        # 1. 計算 Maker 交易所的 Delta
        if current_position is not None:
            maker_current = current_position
        else:
            maker_current = self._get_tracked_position()
            if maker_current is None:
                maker_current = self._sync_position_from_api()
        
        if maker_current is None:
            return None
            
        maker_delta = maker_current - self._hedge_position_reference
        
        # 2. 如果沒有跨交易所，直接返回 Maker Delta
        if not self.hedge_client:
            return maker_delta
            
        # 3. 如果是跨交易所，加上 Hedge 交易所的 Delta
        hedge_current = self._fetch_hedge_exchange_position()
        if hedge_current is None:
            return None # 無法確認對沖端倉位，安全起見返回 None
            
        hedge_delta = hedge_current - self._hedge_exchange_position_reference
        
        # 總 Delta = Maker Delta + Hedge Delta
        # 注意：如果是期現套利，一個是 Perp (-1), 一个是 Spot (+1)，和應該為 0
        total_delta = maker_delta + hedge_delta
        logger.debug("跨交易所 Delta 計算: Maker(%.8f) + Hedge(%.8f) = Total(%.8f)", 
                     maker_delta, hedge_delta, total_delta)
        return total_delta

    def _fetch_current_position_reference(self, force_refresh: bool = False) -> Optional[float]:
        """透過 API 獲取當前倉位指標。
        
        Args:
            force_refresh: 已棄用，保留供相容性
        """
        try:
            if isinstance(self, PerpetualMarketMaker):
                positions = self._request_positions()
                
                if isinstance(positions, dict) and "error" in positions:
                    error_msg = positions.get("error", "")
                    if "404" in str(error_msg) or "RESOURCE_NOT_FOUND" in str(error_msg):
                        logger.debug("無倉位記錄，當前倉位為0")
                        return 0.0
                    logger.error(f"獲取倉位失敗: {error_msg}")
                    return None

                if isinstance(positions, list):
                    if not positions:
                        logger.debug("無倉位記錄，當前倉位為0")
                        return 0.0
                        
                    position = positions[0]
                    for field in ["netQuantity", "size", "position_size", "amount"]:
                        if field in position:
                            return float(position[field] or 0)
                            
                    logger.warning(f"倉位信息中找不到數量字段: {position}")
                    return 0.0
                
                logger.error(f"意外的API響應格式: {positions}")
                return None

            _, total = self.get_asset_balance(self.base_asset)
            return float(total or 0.0)
            
        except Exception as exc:
            logger.error("獲取倉位資訊時發生錯誤: %s", exc)
            return None

    # ------------------------------------------------------------------
    # 節流與重試工具
    # ------------------------------------------------------------------
    def _respect_request_interval(self, slot: str) -> None:
        interval = self._request_intervals.get(slot)
        if not interval:
            return
        last_ts = self._last_request_ts.get(slot, 0.0)
        now = time.monotonic()
        wait_for = interval - (now - last_ts)
        if wait_for > 0:
            time.sleep(wait_for)
        self._last_request_ts[slot] = time.monotonic()

    def _detect_rate_limit(self, payload: Any) -> Optional[str]:
        if payload is None:
            return None
        message = None
        if isinstance(payload, dict):
            for key in ("error", "message", "detail"):
                value = payload.get(key)
                if value:
                    message = str(value)
                    break
        else:
            message = str(payload)

        if not message:
            return None

        lowered = message.lower()
        keywords = ("too many", "rate limit", "429", "request limit")
        if any(keyword in lowered for keyword in keywords):
            return message
        return None

    def _request_with_backoff(self, slot: str, func: Any, *args: Any, **kwargs: Any) -> Any:
        backoff = self._rate_limit_backoff
        result: Any = None
        for attempt in range(1, self._rate_limit_retries + 1):
            self._respect_request_interval(slot)
            try:
                result = func(*args, **kwargs)
            except Exception as exc:  # pragma: no cover - log for visibility
                message = self._detect_rate_limit(exc)
                if not message or attempt == self._rate_limit_retries:
                    raise
                logger.warning(
                    "API %s 請求觸發限制 (%s)，將在 %.2fs 後重試 (%d/%d)",
                    slot,
                    message,
                    backoff,
                    attempt,
                    self._rate_limit_retries,
                )
                time.sleep(backoff)
                backoff = min(backoff * 1.6, self._rate_limit_max_backoff)
                continue

            message = self._detect_rate_limit(result)
            if not message:
                return result

            if attempt == self._rate_limit_retries:
                logger.error("API %s 持續遭遇請求限制: %s", slot, message)
                return result

            logger.warning(
                "API %s 請求觸發限制 (%s)，將在 %.2fs 後重試 (%d/%d)",
                slot,
                message,
                backoff,
                attempt,
                self._rate_limit_retries,
            )
            time.sleep(backoff)
            backoff = min(backoff * 1.6, self._rate_limit_max_backoff)

        return result

    def _submit_order(self, order: Dict[str, Any], slot: str) -> Any:
        return self._request_with_backoff(slot, self.client.execute_order, order)

    def _request_positions(self) -> Any:
        return self._request_with_backoff("position", self.client.get_positions, self.symbol)

    def _build_limit_order(self, side: str, price: float, quantity: float) -> Dict[str, str]:
        """依交易所特性構建單向限價訂單負載。"""

        order = {
            "orderType": "Limit",
            "price": str(round_to_tick_size(price, self.tick_size)),
            "quantity": str(round_to_precision(quantity, self.base_precision)),
            "side": side,
            "symbol": self.symbol,
            "timeInForce": "GTC",
        }

        if getattr(self, "exchange", "") in ["backpack", "standx"]:
            order["postOnly"] = True
            if self.exchange == "backpack":
                order["autoLendRedeem"] = True
                order["autoLend"] = True

        return order


class _SpotMakerTakerHedgeStrategy(_MakerTakerHedgeMixin, MarketMaker):
    """現貨 Maker 掛單 + Taker 對沖實作。"""

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        symbol: str,
        base_spread_percentage: float = 0.0,
        order_quantity: Optional[float] = None,
        exchange: str = "backpack",
        exchange_config: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            api_key=api_key,
            secret_key=secret_key,
            symbol=symbol,
            base_spread_percentage=base_spread_percentage,
            order_quantity=order_quantity,
            exchange=exchange,
            exchange_config=exchange_config,
            hedge_label="現貨僅掛買一/賣一",
            **kwargs,
        )


class _PerpMakerTakerHedgeStrategy(_MakerTakerHedgeMixin, PerpetualMarketMaker):
    """永續合約 Maker 掛單 + Taker 對沖實作。"""

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        symbol: str,
        base_spread_percentage: float = 0.0,
        order_quantity: Optional[float] = None,
        target_position: float = 0.0,
        max_position: float = 1.0,
        position_threshold: float = 0.1,
        inventory_skew: float = 0.0,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        exchange: str = "backpack",
        exchange_config: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            api_key=api_key,
            secret_key=secret_key,
            symbol=symbol,
            base_spread_percentage=base_spread_percentage,
            order_quantity=order_quantity,
            target_position=target_position,
            max_position=max_position,
            position_threshold=position_threshold,
            inventory_skew=inventory_skew,
            stop_loss=stop_loss,
            take_profit=take_profit,
            exchange=exchange,
            exchange_config=exchange_config,
            hedge_label="永續合約僅掛買一/賣一",
            **kwargs,
        )


class MakerTakerHedgeStrategy:
    """根據市場類型返回對應的 Maker-Taker 對沖策略實例。"""

    def __new__(cls, *args: Any, market_type: str = "spot", **kwargs: Any):
        market = (market_type or "spot").lower()
        if market == "perp":
            return _PerpMakerTakerHedgeStrategy(*args, **kwargs)
        return _SpotMakerTakerHedgeStrategy(*args, **kwargs)

