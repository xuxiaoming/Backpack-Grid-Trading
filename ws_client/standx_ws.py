"""
StandX WebSocket 客户端模块
"""
import json
import time
import threading
import os
import uuid
from collections import deque
from typing import Dict, Any, Optional, List, Callable
import websocket as ws
from api.standx_client import StandXClient
from utils.helpers import calculate_volatility
from logger import setup_logger
from urllib.parse import urlparse

logger = setup_logger("standx_ws")

class StandXWebSocket:
    """
    StandX WebSocket 客户端
    
    支持 Market Stream (wss://perps.standx.com/ws-stream/v1)
    提供行情、订单、仓位和余额的实时更新
    """
    def __init__(self, jwt_token, symbol, on_message_callback=None, auto_reconnect=True, proxy=None):
        """
        初始化 StandX WebSocket 客户端

        Args:
            jwt_token: StandX JWT 令牌
            symbol: 交易对符号 (例如: BTC-USD)
            on_message_callback: 消息回调函数
            auto_reconnect: 是否自动重连
            proxy: 代理设置
        """
        self.jwt_token = jwt_token
        self.symbol = symbol
        self.ws = None
        self.on_message_callback = on_message_callback
        self.connected = False
        self.last_price = None
        self.bid_price = None
        self.ask_price = None
        self.orderbook = {"bids": [], "asks": []}
        self.order_updates = []
        self.historical_prices = []
        self.max_price_history = 100

        # 重连相关参数
        self.auto_reconnect = auto_reconnect
        self.reconnect_delay = 1
        self.max_reconnect_delay = 1800
        self.reconnect_attempts = 0
        self.max_reconnect_attempts = 5
        self.reconnect_cooldown_until = 0.0
        self.running = False
        self.ws_thread = None
        self.reconnecting = False

        # 记录已订阅的频道
        self.subscriptions = []
        self.ws_lock = threading.Lock()

        # 心跳检测
        self.last_heartbeat = time.time()
        self.heartbeat_interval = 10  # StandX 服务器每10秒发送一次 Ping
        self.heartbeat_thread = None

        # 代理设置
        if proxy is None:
            proxy = os.getenv('HTTPS_PROXY') or os.getenv('HTTP_PROXY')
        self.proxy = proxy

        # StandX 特有配置
        self.market_stream_url = "wss://perps.standx.com/ws-stream/v1"
        
        # 客户端缓存，用于 API 备援
        self.client = StandXClient({"jwt_token": jwt_token})
        
        # API 备援模式
        self.api_fallback_active = False
        self.api_poll_interval = 2
        self.api_fallback_thread = None

    def connect(self):
        """建立 WebSocket 连接"""
        try:
            self.running = True
            self.reconnect_attempts = 0
            self.reconnect_cooldown_until = 0.0
            self.reconnecting = False
            
            # 初始化 WebSocketApp
            self.ws = ws.WebSocketApp(
                self.market_stream_url,
                on_open=self.on_open,
                on_message=self.on_message,
                on_error=self.on_error,
                on_close=self.on_close,
                on_ping=self.on_ping,
                on_pong=self.on_pong
            )
            
            # 启动连接线程
            self.ws_thread = threading.Thread(target=self._ws_run_forever, daemon=True)
            self.ws_thread.start()
            
            # 启动心跳检测线程
            self._start_heartbeat()
            
            logger.info(f"StandX WebSocket 连接已启动: {self.market_stream_url}")
        except Exception as e:
            logger.error(f"初始化 StandX WebSocket 连接失败: {e}")
            self._start_api_fallback()

    def _ws_run_forever(self):
        """WebSocket 运行循环"""
        try:
            proxy_host = None
            proxy_port = None
            proxy_auth = None
            proxy_type = None

            if self.proxy:
                parsed = urlparse(self.proxy)
                proxy_host = parsed.hostname
                proxy_port = parsed.port
                if parsed.username and parsed.password:
                    proxy_auth = (parsed.username, parsed.password)
                proxy_type = parsed.scheme if parsed.scheme in ['http', 'socks4', 'socks5'] else 'http'

            self.ws.run_forever(
                ping_interval=0,  # 禁用内置 ping，我们手动响应服务器 ping
                http_proxy_host=proxy_host,
                http_proxy_port=proxy_port,
                http_proxy_auth=proxy_auth,
                proxy_type=proxy_type
            )
        except Exception as e:
            logger.error(f"StandX WebSocket 运行异常: {e}")
        finally:
            self.connected = False

    def on_open(self, ws):
        """WebSocket 连接打开时的回调"""
        logger.info("StandX WebSocket 连接已打开")
        self.connected = True
        self.reconnect_attempts = 0
        self.last_heartbeat = time.time()
        
        # 停止 API 备援模式
        self._stop_api_fallback()
        
        # 1. 身份认证 (Market Stream)
        self._authenticate()
        
        # 2. 订阅公共频道
        self.subscribe_price()
        self.subscribe_depth()
        
        # 3. 订阅私有频道
        self.subscribe_order()
        self.subscribe_position()
        self.subscribe_balance()

    def _authenticate(self):
        """发送身份认证请求"""
        if not self.jwt_token:
            logger.warning("未提供 JWT 令牌，跳过私有频道认证")
            return

        auth_msg = {
            "auth": {
                "token": self.jwt_token
            }
        }
        try:
            self.ws.send(json.dumps(auth_msg))
            logger.info("已发送 StandX WebSocket 认证请求")
        except Exception as e:
            logger.error(f"发送认证请求失败: {e}")

    def subscribe_bookTicker(self):
        """订阅价格频道 (兼容性方法)"""
        return self.subscribe_price()

    def subscribe_price(self):
        """订阅价格频道"""
        msg = {
            "subscribe": {
                "channel": "price",
                "symbol": self.symbol
            }
        }
        return self._send_message(msg, "price")

    def subscribe_depth(self):
        """订阅深度频道"""
        msg = {
            "subscribe": {
                "channel": "depth_book",
                "symbol": self.symbol
            }
        }
        return self._send_message(msg, "depth_book")

    def initialize_orderbook(self):
        """通过 REST API 获取订单簿初始快照"""
        try:
            order_book = self.client.get_order_book(self.symbol, limit=20)
            if order_book and "error" not in order_book:
                self.orderbook = {
                    "bids": [[float(p), float(q)] for p, q in order_book.get("bids", [])],
                    "asks": [[float(p), float(q)] for p, q in order_book.get("asks", [])]
                }
                logger.info(f"StandX 订单簿初始化成功: {len(self.orderbook['bids'])} bids, {len(self.orderbook['asks'])} asks")
                return True
            else:
                logger.error(f"StandX 订单簿初始化失败: {order_book.get('error') if order_book else 'No response'}")
                return False
        except Exception as e:
            logger.error(f"StandX 订单簿初始化异常: {e}")
            return False

    def private_subscribe(self, stream):
        """订阅私有数据流 (兼容性方法)"""
        if "orderUpdate" in stream:
            return self.subscribe_order()
        elif "positionUpdate" in stream:
            return self.subscribe_position()
        elif "balanceUpdate" in stream:
            return self.subscribe_balance()
        else:
            logger.warning(f"StandX 不支持订阅流: {stream}")
            return False

    def subscribe_order(self):
        """订阅订单更新频道 (私有)"""
        msg = {
            "subscribe": {
                "channel": "order"
            }
        }
        self._send_message(msg, "order")

    def subscribe_position(self):
        """订阅仓位更新频道 (私有)"""
        msg = {
            "subscribe": {
                "channel": "position"
            }
        }
        self._send_message(msg, "position")

    def subscribe_balance(self):
        """订阅余额更新频道 (私有)"""
        msg = {
            "subscribe": {
                "channel": "balance"
            }
        }
        self._send_message(msg, "balance")

    def _send_message(self, msg, channel_name):
        """发送订阅消息并记录"""
        try:
            if self.connected and self.ws:
                self.ws.send(json.dumps(msg))
                if channel_name not in self.subscriptions:
                    self.subscriptions.append(channel_name)
                logger.info(f"已发送订阅请求: {channel_name}")
            else:
                logger.warning(f"WebSocket 未连接，无法订阅: {channel_name}")
        except Exception as e:
            logger.error(f"发送订阅消息失败 ({channel_name}): {e}")

    def on_message(self, ws, message):
        """处理接收到的消息"""
        try:
            data = json.loads(message)
            self.last_heartbeat = time.time()
            
            channel = data.get("channel")
            if not channel:
                # 可能是认证响应或其他非数据消息
                if "channel" in data.get("data", {}) == "auth":
                    logger.info(f"认证状态: {data.get('data', {}).get('msg')}")
                return

            event_data = data.get("data")
            if not event_data:
                return

            # 处理不同频道的数据
            if channel == "price":
                self._handle_price_update(event_data)
            elif channel == "depth_book":
                self._handle_depth_update(event_data)
            elif channel == "order":
                self._handle_order_update(event_data)
            elif channel == "position":
                self._handle_position_update(event_data)
            elif channel == "balance":
                self._handle_balance_update(event_data)

            # 调用外部回调
            if self.on_message_callback:
                # 转换频道名以适配策略层 (模拟 Backpack 的格式)
                mapped_channel = self._map_channel_name(channel)
                mapped_data = self._map_event_data(channel, event_data)
                self.on_message_callback(mapped_channel, mapped_data)

        except Exception as e:
            logger.error(f"处理 WebSocket 消息异常: {e}, 原始消息: {message}")

    def _map_channel_name(self, channel):
        """将 StandX 频道映射为策略层预期的格式"""
        mapping = {
            "price": f"bookTicker.{self.symbol}",
            "depth_book": f"depth.{self.symbol}",
            "order": f"account.orderUpdate.{self.symbol}",
            "position": "account.positionUpdate",
            "balance": "account.balanceUpdate"
        }
        return mapping.get(channel, channel)

    def _map_event_data(self, channel, data):
        """将 StandX 数据格式映射为策略层预期的格式 (主要适配 Backpack 格式)"""
        if channel == "price":
            # StandX price -> Backpack bookTicker
            # 这里的 spread[0] 是 bid, spread[1] 是 ask
            spread = data.get("spread", ["0", "0"])
            return {
                "b": spread[0],
                "a": spread[1],
                "p": data.get("last_price"),
                "s": data.get("symbol"),
                "T": int(time.time() * 1000)
            }
        elif channel == "depth_book":
            # StandX depth_book -> Backpack depth
            return {
                "b": data.get("bids", []),
                "a": data.get("asks", []),
                "s": data.get("symbol")
            }
        elif channel == "order":
            # StandX order -> Backpack orderUpdate
            # 转换方向和状态
            side = data.get("side", "").lower()
            mapped_side = "Bid" if side == "buy" else "Ask" if side == "sell" else side
            
            return {
                "e": "orderUpdate",
                "i": str(data.get("id")),
                "c": data.get("cl_ord_id"),
                "S": mapped_side,
                "o": data.get("order_type"),
                "f": data.get("time_in_force"),
                "p": data.get("price"),
                "q": data.get("qty"),
                "X": data.get("status", "").upper(),
                "l": data.get("fill_qty"),
                "L": data.get("fill_avg_price"),
                "n": data.get("fee", "0"),
                "N": data.get("fee_asset", "DUSD"),
                "T": int(time.time() * 1000)
            }
        return data

    def _handle_price_update(self, data):
        """更新内部价格状态"""
        spread = data.get("spread", [])
        if len(spread) >= 2:
            self.bid_price = float(spread[0])
            self.ask_price = float(spread[1])
            self.last_price = float(data.get("last_price", (self.bid_price + self.ask_price) / 2))
            self.historical_prices.append(self.last_price)
            if len(self.historical_prices) > self.max_price_history:
                self.historical_prices.pop(0)

    def _handle_depth_update(self, data):
        """更新内部订单簿状态"""
        self.orderbook = {
            "bids": [[float(p), float(q)] for p, q in data.get("bids", [])],
            "asks": [[float(p), float(q)] for p, q in data.get("asks", [])]
        }

    def _handle_order_update(self, data):
        """处理订单更新"""
        self.order_updates.append(data)
        if len(self.order_updates) > 100:
            self.order_updates.pop(0)

    def _handle_position_update(self, data):
        """处理仓位更新 (根据需要实现)"""
        pass

    def _handle_balance_update(self, data):
        """处理余额更新 (根据需要实现)"""
        pass

    def _start_heartbeat(self):
        """启动心跳检测线程"""
        if self.heartbeat_thread is None or not self.heartbeat_thread.is_alive():
            self.heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
            self.heartbeat_thread.start()

    def _heartbeat_loop(self):
        """定期检查连接状态"""
        while self.running:
            time.sleep(5)
            if not self.connected:
                continue
                
            current_time = time.time()
            # 如果 60 秒没收到消息或 ping，尝试重连
            if current_time - self.last_heartbeat > 60:
                logger.warning("StandX WebSocket 心跳超时，触发重连")
                self.reconnect()

    def on_ping(self, ws, message):
        """处理服务器发送的 Ping"""
        self.last_heartbeat = time.time()
        try:
            # 响应 Pong (WebSocket 库会自动处理部分，但我们明确记录)
            ws.send(message, opcode=0x0a)  # 0x0a 是 Pong
            logger.debug("已响应 StandX Server Ping")
        except Exception as e:
            logger.error(f"响应 Pong 失败: {e}")

    def on_pong(self, ws, message):
        """处理 Pong"""
        self.last_heartbeat = time.time()

    def on_error(self, ws, error):
        """处理连接错误"""
        logger.error(f"StandX WebSocket 错误: {error}")
        self._start_api_fallback()

    def on_close(self, ws, close_status_code, close_msg):
        """处理连接关闭"""
        self.connected = False
        logger.info(f"StandX WebSocket 连接已关闭: {close_msg} (code: {close_status_code})")
        
        if self.running and self.auto_reconnect:
            self.reconnect()

    def check_and_reconnect_if_needed(self):
        """检查连接状态并在需要时重连 - 供外部调用"""
        current_time = time.time()
        
        # 如果在冷却期内，确保 API 备援模式激活，但不触发重连
        if self.reconnect_cooldown_until and current_time < self.reconnect_cooldown_until:
            if not self.is_connected() and not self.api_fallback_active:
                remaining_cooldown = int(self.reconnect_cooldown_until - current_time)
                logger.debug(f"冷卻期內檢查到連接斷開（剩餘 {remaining_cooldown} 秒），啟動 API 備援模式")
                self._start_api_fallback()
            return self.is_connected()

        # 冷却期已结束，重置重连计数器
        if self.reconnect_cooldown_until and current_time >= self.reconnect_cooldown_until:
            self.reconnect_attempts = 0
            self.reconnect_cooldown_until = 0.0
            logger.info("冷卻期結束，重置重連計數器")

        # 非冷却期，检查连接并触发重连
        if not self.is_connected() and not self.reconnecting:
            logger.info("外部檢查發現連接斷開，觸發重連...")
            self._trigger_reconnect()
            self._start_api_fallback()

        return self.is_connected()

    def _trigger_reconnect(self):
        """触发重连（异步）"""
        threading.Thread(target=self.reconnect, daemon=True).start()

    def reconnect(self):
        """执行重连"""
        if self.reconnecting:
            return
            
        with self.ws_lock:
            self.reconnecting = True
            self.connected = False
            
            if self.reconnect_attempts >= self.max_reconnect_attempts:
                logger.error("StandX WebSocket 重连次数达到上限")
                self._start_api_fallback()
                self.reconnecting = False
                return

            self.reconnect_attempts += 1
            delay = min(self.reconnect_delay * (2 ** (self.reconnect_attempts - 1)), self.max_reconnect_delay)
            
            logger.info(f"将在 {delay} 秒后进行第 {self.reconnect_attempts} 次重连")
            time.sleep(delay)
            
            if self.ws:
                try:
                    self.ws.close()
                except:
                    pass
            
            self.connect()
            self.reconnecting = False

    def _start_api_fallback(self):
        """启动 API 备援轮询"""
        if self.api_fallback_active:
            return
            
        logger.info("启动 StandX API 备援模式")
        self.api_fallback_active = True
        self.api_fallback_thread = threading.Thread(target=self._api_fallback_loop, daemon=True)
        self.api_fallback_thread.start()

    def _stop_api_fallback(self):
        """停止 API 备援轮询"""
        self.api_fallback_active = False

    def _api_fallback_loop(self):
        """定期通过 REST API 获取数据"""
        while self.running and self.api_fallback_active:
            try:
                # 获取行情
                ticker = self.client.get_ticker(self.symbol)
                if ticker and "error" not in ticker:
                    self.last_price = float(ticker.get("lastPrice", 0))
                    self.bid_price = float(ticker.get("bidPrice", 0))
                    self.ask_price = float(ticker.get("askPrice", 0))
                    
                    if self.on_message_callback:
                        self.on_message_callback(f"bookTicker.{self.symbol}", {
                            "b": str(self.bid_price),
                            "a": str(self.ask_price),
                            "p": str(self.last_price),
                            "source": "api"
                        })
                
                # 获取深度
                depth = self.client.get_order_book(self.symbol, limit=20)
                if depth and "error" not in depth:
                    self.orderbook = depth
                    if self.on_message_callback:
                        self.on_message_callback(f"depth.{self.symbol}", depth)
                        
            except Exception as e:
                logger.error(f"StandX API 备援轮询异常: {e}")
            
            time.sleep(self.api_poll_interval)

    def close(self):
        """关闭客户端"""
        self.running = False
        self.connected = False
        self._stop_api_fallback()
        if self.ws:
            self.ws.close()
        logger.info("StandX WebSocket 客户端已关闭")

    def get_current_price(self):
        return self.last_price

    def get_bid_ask(self):
        return self.bid_price, self.ask_price

    def get_orderbook(self):
        return self.orderbook

    def get_volatility(self, window=20):
        return calculate_volatility(self.historical_prices, window)
    
    def is_connected(self):
        return self.connected
