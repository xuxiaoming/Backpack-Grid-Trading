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
        """
        self.jwt_token = jwt_token
        self.symbol = symbol
        self.ws = None
        self.on_message_callback = on_message_callback
        self.connected = False
        self.authenticated = False  # 追踪认证状态
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
        self.pending_subscriptions = []  # 等待认证后发送的订阅
        self.ws_lock = threading.Lock()

        # 心跳检测
        self.last_heartbeat = time.time()
        self.heartbeat_interval = 10
        self.heartbeat_thread = None

        # 代理设置
        if proxy is None:
            proxy = os.getenv('HTTPS_PROXY') or os.getenv('HTTP_PROXY')
        self.proxy = proxy

        self.market_stream_url = "wss://perps.standx.com/ws-stream/v1"
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
            self.authenticated = False
            
            self.ws = ws.WebSocketApp(
                self.market_stream_url,
                on_open=self.on_open,
                on_message=self.on_message,
                on_error=self.on_error,
                on_close=self.on_close,
                on_ping=self.on_ping,
                on_pong=self.on_pong
            )
            
            self.ws_thread = threading.Thread(target=self._ws_run_forever, daemon=True)
            self.ws_thread.start()
            self._start_heartbeat()
            
            logger.info(f"StandX WebSocket 连接已启动: {self.market_stream_url}")
        except Exception as e:
            logger.error(f"初始化 StandX WebSocket 连接失败: {e}")
            self._start_api_fallback()

    def _ws_run_forever(self):
        """WebSocket 运行循环"""
        try:
            proxy_host, proxy_port, proxy_auth, proxy_type = None, None, None, None
            if self.proxy:
                parsed = urlparse(self.proxy)
                proxy_host, proxy_port = parsed.hostname, parsed.port
                if parsed.username and parsed.password:
                    proxy_auth = (parsed.username, parsed.password)
                proxy_type = parsed.scheme if parsed.scheme in ['http', 'socks4', 'socks5'] else 'http'

            self.ws.run_forever(
                ping_interval=0,
                http_proxy_host=proxy_host,
                http_proxy_port=proxy_port,
                http_proxy_auth=proxy_auth,
                proxy_type=proxy_type
            )
        except Exception as e:
            logger.error(f"StandX WebSocket 运行异常: {e}")
        finally:
            self.connected = False
            self.authenticated = False

    def on_open(self, ws):
        """WebSocket 连接打开时的回调"""
        logger.info("StandX WebSocket 连接已打开")
        self.connected = True
        self.reconnect_attempts = 0
        self.last_heartbeat = time.time()
        self._stop_api_fallback()
        
        # 1. 身份认证 (Market Stream)
        self._authenticate()
        
        # 2. 订阅公共频道 (不需要认证)
        self.subscribe_price()
        self.subscribe_depth()

    def _authenticate(self):
        """发送身份认证请求"""
        if not self.jwt_token:
            logger.warning("未提供 JWT 令牌，跳过私有频道认证")
            return

        auth_msg = {"auth": {"token": self.jwt_token}}
        try:
            self.ws.send(json.dumps(auth_msg))
            logger.info("已发送 StandX WebSocket 认证请求")
        except Exception as e:
            logger.error(f"发送认证请求失败: {e}")

    def on_message(self, ws, message):
        """处理接收到的消息"""
        try:
            data = json.loads(message)
            self.last_heartbeat = time.time()
            
            channel = data.get("channel")
            
            # 处理认证响应
            if channel == "auth":
                res_data = data.get("data", {})
                # StandX 成功代码可能是 200 或 0
                if res_data.get("code") in [0, 200]:
                    logger.info("StandX WebSocket 认证成功")
                    self.authenticated = True
                    # 订阅排队中的频道
                    for sub_msg, name in self.pending_subscriptions:
                        self._send_message(sub_msg, name)
                    self.pending_subscriptions.clear()
                    
                    # 默认订阅核心频道
                    self.subscribe_order()
                    self.subscribe_position()
                    self.subscribe_balance()
                else:
                    logger.error(f"StandX WebSocket 认证失败: {json.dumps(res_data)}")
                    self.authenticated = False
                return

            if not channel: return

            event_data = data.get("data")
            if not event_data: return

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

            if self.on_message_callback:
                mapped_channel = self._map_channel_name(channel)
                mapped_data = self._map_event_data(channel, event_data)
                self.on_message_callback(mapped_channel, mapped_data)

        except Exception as e:
            logger.error(f"处理 WebSocket 消息异常: {e}, 原始消息: {message}")

    def subscribe_price(self):
        msg = {"subscribe": {"channel": "price", "symbol": self.symbol}}
        return self._send_message(msg, "price")

    def subscribe_bookTicker(self):
        """订阅价格频道 (兼容性方法，适配策略层调用)"""
        return self.subscribe_price()

    def subscribe_depth(self):
        msg = {"subscribe": {"channel": "depth_book", "symbol": self.symbol}}
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
        return False

    def subscribe_order(self):
        msg = {"subscribe": {"channel": "order"}}
        return self._send_message(msg, "order", is_private=True)

    def subscribe_position(self):
        msg = {"subscribe": {"channel": "position"}}
        return self._send_message(msg, "position", is_private=True)

    def subscribe_balance(self):
        msg = {"subscribe": {"channel": "balance"}}
        return self._send_message(msg, "balance", is_private=True)

    def _send_message(self, msg, channel_name, is_private=False):
        """发送订阅消息并记录"""
        try:
            if not self.connected or not self.ws:
                return False
            
            # 如果是私有频道但尚未认证，加入待处理队列
            if is_private and not self.authenticated:
                logger.info(f"等待认证成功后再订阅私有频道: {channel_name}")
                if (msg, channel_name) not in self.pending_subscriptions:
                    self.pending_subscriptions.append((msg, channel_name))
                return True # 返回 True 停止策略层的重试

            self.ws.send(json.dumps(msg))
            if channel_name not in self.subscriptions:
                self.subscriptions.append(channel_name)
            logger.info(f"已发送订阅请求: {channel_name}")
            return True
        except Exception as e:
            logger.error(f"发送订阅消息失败 ({channel_name}): {e}")
            return False

    def _map_channel_name(self, channel):
        mapping = {
            "price": f"bookTicker.{self.symbol}",
            "depth_book": f"depth.{self.symbol}",
            "order": f"account.orderUpdate.{self.symbol}",
            "position": "account.positionUpdate",
            "balance": "account.balanceUpdate"
        }
        return mapping.get(channel, channel)

    def _map_event_data(self, channel, data):
        if channel == "price":
            spread = data.get("spread", ["0", "0"])
            return {
                "b": spread[0], "a": spread[1],
                "p": data.get("last_price"), "s": data.get("symbol"),
                "T": int(time.time() * 1000)
            }
        elif channel == "depth_book":
            return {"b": data.get("bids", []), "a": data.get("asks", []), "s": data.get("symbol")}
        elif channel == "order":
            side = data.get("side", "").lower()
            mapped_side = "Bid" if side == "buy" else "Ask" if side == "sell" else side
            return {
                "e": "orderUpdate", "i": str(data.get("id")), "c": data.get("cl_ord_id"),
                "S": mapped_side, "o": data.get("order_type"), "f": data.get("time_in_force"),
                "p": data.get("price"), "q": data.get("qty"), "X": data.get("status", "").upper(),
                "l": data.get("fill_qty"), "L": data.get("fill_avg_price"),
                "n": data.get("fee", "0"), "N": data.get("fee_asset", "DUSD"),
                "T": int(time.time() * 1000)
            }
        return data

    def _handle_price_update(self, data):
        spread = data.get("spread", [])
        if len(spread) >= 2:
            self.bid_price, self.ask_price = float(spread[0]), float(spread[1])
            self.last_price = float(data.get("last_price", (self.bid_price + self.ask_price) / 2))
            self.historical_prices.append(self.last_price)
            if len(self.historical_prices) > self.max_price_history: self.historical_prices.pop(0)

    def _handle_depth_update(self, data):
        self.orderbook = {
            "bids": [[float(p), float(q)] for p, q in data.get("bids", [])],
            "asks": [[float(p), float(q)] for p, q in data.get("asks", [])]
        }

    def _handle_order_update(self, data):
        self.order_updates.append(data)
        if len(self.order_updates) > 100: self.order_updates.pop(0)

    def _handle_position_update(self, data): pass
    def _handle_balance_update(self, data): pass

    def _start_heartbeat(self):
        if self.heartbeat_thread is None or not self.heartbeat_thread.is_alive():
            self.heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
            self.heartbeat_thread.start()

    def _heartbeat_loop(self):
        while self.running:
            time.sleep(5)
            if not self.connected: continue
            if time.time() - self.last_heartbeat > 60:
                logger.warning("StandX WebSocket 心跳超时，触发重连")
                self.reconnect()

    def on_ping(self, ws, message):
        self.last_heartbeat = time.time()
        try:
            ws.send(message, opcode=0x0a)
            logger.debug("已响应 StandX Server Ping")
        except Exception as e:
            logger.error(f"响应 Pong 失败: {e}")

    def on_pong(self, ws, message):
        self.last_heartbeat = time.time()

    def on_error(self, ws, error):
        logger.error(f"StandX WebSocket 错误: {error}")
        self._start_api_fallback()

    def on_close(self, ws, close_status_code, close_msg):
        self.connected = False
        self.authenticated = False
        logger.info(f"StandX WebSocket 连接已关闭: {close_msg} (code: {close_status_code})")
        if self.running and self.auto_reconnect: self.reconnect()

    def check_and_reconnect_if_needed(self):
        current_time = time.time()
        if self.reconnect_cooldown_until and current_time < self.reconnect_cooldown_until:
            if not self.is_connected() and not self.api_fallback_active: self._start_api_fallback()
            return self.is_connected()
        if self.reconnect_cooldown_until and current_time >= self.reconnect_cooldown_until:
            self.reconnect_attempts = 0
            self.reconnect_cooldown_until = 0.0
        if not self.is_connected() and not self.reconnecting:
            self._trigger_reconnect()
            self._start_api_fallback()
        return self.is_connected()

    def _trigger_reconnect(self):
        threading.Thread(target=self.reconnect, daemon=True).start()

    def reconnect(self):
        if self.reconnecting: return
        with self.ws_lock:
            self.reconnecting, self.connected, self.authenticated = True, False, False
            if self.reconnect_attempts >= self.max_reconnect_attempts:
                self._start_api_fallback()
                self.reconnecting = False
                return
            self.reconnect_attempts += 1
            delay = min(self.reconnect_delay * (2 ** (self.reconnect_attempts - 1)), self.max_reconnect_delay)
            logger.info(f"将在 {delay} 秒后进行第 {self.reconnect_attempts} 次重连")
            time.sleep(delay)
            if self.ws:
                try: self.ws.close()
                except: pass
            self.connect()
            self.reconnecting = False

    def _start_api_fallback(self):
        if self.api_fallback_active: return
        logger.info("启动 StandX API 备援模式")
        self.api_fallback_active = True
        self.api_fallback_thread = threading.Thread(target=self._api_fallback_loop, daemon=True)
        self.api_fallback_thread.start()

    def _stop_api_fallback(self): self.api_fallback_active = False

    def _api_fallback_loop(self):
        while self.running and self.api_fallback_active:
            try:
                ticker = self.client.get_ticker(self.symbol)
                if ticker and "error" not in ticker:
                    self.last_price, self.bid_price, self.ask_price = float(ticker.get("lastPrice", 0)), float(ticker.get("bidPrice", 0)), float(ticker.get("askPrice", 0))
                    if self.on_message_callback:
                        self.on_message_callback(f"bookTicker.{self.symbol}", {"b": str(self.bid_price), "a": str(self.ask_price), "p": str(self.last_price), "source": "api"})
                depth = self.client.get_order_book(self.symbol, limit=20)
                if depth and "error" not in depth:
                    self.orderbook = depth
                    if self.on_message_callback: self.on_message_callback(f"depth.{self.symbol}", depth)
            except Exception as e: logger.error(f"StandX API 备援轮询异常: {e}")
            time.sleep(self.api_poll_interval)

    def close(self):
        self.running, self.connected, self.authenticated = False, False, False
        self._stop_api_fallback()
        if self.ws: self.ws.close()
        logger.info("StandX WebSocket 客户端已关闭")

    def is_connected(self): return self.connected
    def get_current_price(self): return self.last_price
    def get_bid_ask(self): return self.bid_price, self.ask_price
    def get_orderbook(self): return self.orderbook
    def get_volatility(self, window=20): return calculate_volatility(self.historical_prices, window)
