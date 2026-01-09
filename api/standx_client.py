"""StandX exchange REST client implementation."""
from __future__ import annotations

import base64
import json
import os
import time
import uuid
from typing import Any, Dict, List, Optional, Set
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

import requests

try:
    from nacl.signing import SigningKey
    HAS_NACL = True
except ImportError:
    HAS_NACL = False

from .base_client import BaseExchangeClient
from .proxy_utils import get_proxy_config
from logger import setup_logger

logger = setup_logger("api.standx_client")


class StandXClient(BaseExchangeClient):
    """REST client for the StandX perpetual futures API."""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.jwt_token = config.get("jwt_token")  # JWT token for authentication
        self.base_url = config.get("base_url", "https://perps.standx.com")
        
        # 从环境变量读取 ed25519 密钥对（如果配置中没有）
        if not config.get("ed25519_private_key_bytes"):
            ed25519_private_key_hex = os.getenv("STANDX_ED25519_PRIVATE_KEY_BYTES")
            if ed25519_private_key_hex:
                config["ed25519_private_key_bytes"] = ed25519_private_key_hex
                logger.info("从环境变量读取 ed25519 私钥")
        if not config.get("ed25519_request_id"):
            ed25519_request_id = os.getenv("STANDX_ED25519_REQUEST_ID")
            if ed25519_request_id:
                config["ed25519_request_id"] = ed25519_request_id
        self.timeout = float(config.get("timeout", 10))
        self.max_retries = int(config.get("max_retries", 3))
        self.session = requests.Session()

        # 從環境變量讀取代理配置
        try:
            proxies = get_proxy_config()
            if proxies:
                self.session.proxies.update(proxies)
                logger.info(f"StandX 客户端已配置代理: {proxies}")
        except Exception:
            pass

        # Body signature 相关（ed25519 密钥对）
        # ⚠️ 重要：StandX 的 body signature 必须使用认证时生成的 ed25519 密钥对
        # 认证时，StandXAuth 会生成 ed25519 密钥对并注册到 StandX
        # body signature 必须使用同一个密钥对，否则会验证失败
        
        # 尝试从配置中获取 ed25519 密钥对（认证时生成的）
        ed25519_private_key_hex = config.get("ed25519_private_key_bytes")
        ed25519_public_key_hex = config.get("ed25519_public_key_bytes")
        self._request_id = config.get("ed25519_request_id")
        self.session_id = config.get("session_id") or os.getenv("STANDX_SESSION_ID")
        
        if ed25519_private_key_hex and HAS_NACL:
            try:
                # 从十六进制字符串恢复 SigningKey
                from nacl.signing import SigningKey
                private_key_bytes = bytes.fromhex(ed25519_private_key_hex)
                if len(private_key_bytes) != 32:
                    raise ValueError(f"ed25519 私钥长度不正确: {len(private_key_bytes)} 字节（期望 32 字节）")
                self._ed25519_private_key = SigningKey(private_key_bytes)
                self._ed25519_public_key = self._ed25519_private_key.verify_key
                public_key_bytes = bytes(self._ed25519_public_key)
                logger.info("✓ 使用认证时生成的 ed25519 密钥对进行 body signature")
            except Exception as e:
                logger.error(f"✗ 无法从配置恢复 ed25519 密钥对: {e}")
                logger.warning("将生成新的 ed25519 密钥对（可能导致 body signature 失败）")
                self._init_body_signature()
        else:
            # 如果没有提供认证时的密钥对，生成新的（可能不工作）
            logger.error("=" * 60)
            logger.error("⚠️  未提供认证时的 ed25519 密钥对")
            logger.error("=" * 60)
            logger.error("StandX 的 body signature 必须使用认证时生成的同一个 ed25519 密钥对")
            logger.error("如果使用不同的密钥对，StandX 会返回 'invalid body signature' 错误")
            logger.error("")
            logger.error("解决方案：")
            logger.error("1. 运行认证工具获取 ed25519 密钥对:")
            logger.error("   python utils/standx_auth.py <private_key>")
            logger.error("2. 将输出的 ed25519_private_key_bytes 和 ed25519_request_id 添加到 .env 文件")
            logger.error("3. 确保使用认证时生成的同一个密钥对")
            logger.error("=" * 60)
            self._init_body_signature()

        if self.session_id:
            logger.info(f"使用 StandX session_id: {self.session_id}")
        else:
            self.session_id = str(uuid.uuid4())
            logger.warning(
                "未提供 StandX session_id；已生成临时 session_id "
                f"{self.session_id}，建议通过 STANDX_SESSION_ID 环境变量或配置传入"
            )

        self._symbol_cache: Dict[str, str] = {}
        self._market_info_cache: Dict[str, Dict[str, Any]] = {}

    def _init_body_signature(self):
        """初始化 body signature 所需的 ed25519 密钥对"""
        if not HAS_NACL:
            logger.warning("未安装 pynacl，无法使用 body signature 功能")
            return
        
        try:
            signing_key = SigningKey.generate()
            self._ed25519_private_key = signing_key
            self._ed25519_public_key = signing_key.verify_key
            
            # 生成 request_id（base58 编码的公钥）
            try:
                import base58
                public_key_bytes = bytes(self._ed25519_public_key)
                self._request_id = base58.b58encode(public_key_bytes).decode('utf-8')
            except ImportError:
                # 如果没有 base58，使用 base64
                public_key_bytes = bytes(self._ed25519_public_key)
                self._request_id = base64.b64encode(public_key_bytes).decode('utf-8')
                logger.warning("使用 base64 编码 requestId（建议安装 base58）")
        except Exception as e:
            logger.error(f"初始化 body signature 失败: {e}")

    def get_exchange_name(self) -> str:
        return "StandX"

    async def connect(self) -> None:
        logger.info("StandX 客户端已連接")

    async def disconnect(self) -> None:
        self.session.close()
        logger.info("StandX 客户端已斷開連接")

    def _current_timestamp(self) -> int:
        """获取当前时间戳（毫秒）"""
        return int(time.time() * 1000)

    def _sign_request_body(self, payload: str, request_id: str, timestamp: int) -> Optional[str]:
        """
        生成请求体签名（Body Signature）
        
        根据 StandX 文档：
        - 消息格式: "{version},{id},{timestamp},{payload}"
        - 使用 ed25519 私钥签名
        - Base64 编码签名结果
        
        Args:
            payload: JSON 字符串格式的请求体
            request_id: 请求 ID（UUID）
            timestamp: 时间戳（毫秒）
            
        Returns:
            Base64 编码的签名字符串
        """
        if not HAS_NACL or not self._ed25519_private_key:
            logger.error("无法生成 body signature：未初始化 ed25519 密钥对")
            return None
        
        try:
            # 构建签名消息: "{version},{id},{timestamp},{payload}"
            version = "v1"
            sign_msg = f"{version},{request_id},{timestamp},{payload}"
            
            # 使用 ed25519 私钥签名
            message_bytes = sign_msg.encode('utf-8')
            
            # 确保 _ed25519_private_key 是 SigningKey 对象
            if isinstance(self._ed25519_private_key, bytes):
                # 如果提供的是字节，需要重新创建 SigningKey
                from nacl.signing import SigningKey
                signing_key = SigningKey(self._ed25519_private_key)
            else:
                signing_key = self._ed25519_private_key
            
            # 签名消息
            signed_message = signing_key.sign(message_bytes)
            
            # Base64 编码签名（只编码签名部分，不包含消息）
            # StandX 要求只编码签名字节，不包含原始消息
            signature_b64 = base64.b64encode(signed_message.signature).decode('utf-8')
            
            return signature_b64
        except Exception as e:
            logger.error(f"生成 body signature 失败: {e}")
            import traceback
            traceback.print_exc()
            return None

    def _get_auth_headers(self, need_body_signature: bool = False, payload: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
        """
        获取认证 headers
        
        Args:
            need_body_signature: 是否需要 body signature
            payload: 请求体（用于生成签名）
            
        Returns:
            headers 字典
        """
        headers = {
            "Content-Type": "application/json",
        }
        
        # JWT 认证
        if self.jwt_token:
            headers["authorization"] = f"Bearer {self.jwt_token}"
        
        # Body signature（如果需要）
        if need_body_signature and payload:
            if not HAS_NACL:
                logger.error("无法生成 body signature：未安装 pynacl")
            elif not self._ed25519_private_key:
                logger.error("无法生成 body signature：缺少 ed25519 密钥对")
                logger.warning("提示：StandX 的 body signature 需要使用 ed25519 密钥对")
                logger.warning("客户端会自动生成密钥对，但如果认证时注册了密钥对，请通过配置传入")
            else:
                # Body signature 的 x-request-id 应该是随机字符串（UUID）
                # 根据 StandX 文档: x-request-id: <random_string>
                # 注意：认证时的 request_id（base58 编码的公钥）只用于 prepare-signin 步骤
                # Body signature 的 x-request-id 应该每次都生成新的 UUID
                request_id = str(uuid.uuid4())
                timestamp = self._current_timestamp()

                if self.session_id:
                    headers["x-session-id"] = self.session_id
                
                # 确保 payload 是排序后的 JSON 字符串（StandX 可能要求排序）
                # 使用 separators 去除空格，确保格式一致
                payload_str = json.dumps(payload, separators=(',', ':'), sort_keys=True)
                
                signature = self._sign_request_body(payload_str, request_id, timestamp)
                if signature:
                    headers["x-request-sign-version"] = "v1"
                    headers["x-request-id"] = request_id
                    headers["x-request-timestamp"] = str(timestamp)
                    headers["x-request-signature"] = signature
                else:
                    logger.error("生成 body signature 失败，请求可能被拒绝")
        
        return headers

    def _normalize_order_fields(self, order: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize order fields to standard format."""
        if "id" in order and "order_id" not in order:
            order["order_id"] = str(order["id"])
        if "cl_ord_id" in order and "clientId" not in order:
            order["clientId"] = order["cl_ord_id"]

        # StandX 使用 "buy" 和 "sell"，转换为标准格式
        side = order.get("side")
        if side:
            normalized = side.lower()
            if normalized == "buy":
                order["side"] = "Bid"
            elif normalized == "sell":
                order["side"] = "Ask"

        if "qty" in order and "quantity" not in order:
            order["quantity"] = order["qty"]
        if "qty" in order and "size" not in order:
            order["size"] = order["qty"]
        
        # 标准化订单状态
        status = order.get("status")
        if status:
            # StandX 状态: open, canceled, filled, rejected, untriggered
            status_mapping = {
                "open": "OPEN",
                "canceled": "CANCELED",
                "filled": "FILLED",
                "rejected": "REJECTED",
                "untriggered": "UNTRIGGERED",
            }
            order["status"] = status_mapping.get(status.lower(), status.upper())

        return order

    def _lookup_key(self, symbol: str) -> str:
        """Generate a case-insensitive lookup key for exchange symbols."""
        return symbol.upper().replace("_", "-")

    def _ensure_symbol_cache(self) -> None:
        """Lazy-load the symbol cache from exchange info."""
        if self._symbol_cache and self._market_info_cache:
            return

        info = self.get_markets()
        if isinstance(info, dict) and info.get("error"):
            logger.error("獲取交易對列表失敗: %s", info["error"])
            self._symbol_cache = {}
            self._market_info_cache = {}
            return

        # StandX API 返回格式: 数组
        symbols = info if isinstance(info, list) else []
        if not symbols and isinstance(info, dict):
            symbols = info.get("result", [])

        cache: Dict[str, str] = {}
        market_cache: Dict[str, Dict[str, Any]] = {}
        for item in symbols:
            actual_symbol = item.get("symbol")
            if not actual_symbol:
                continue
            cache[self._lookup_key(actual_symbol)] = actual_symbol
            market_cache[actual_symbol] = item

        self._symbol_cache = cache
        self._market_info_cache = market_cache

    def _resolve_symbol(self, symbol: Optional[str]) -> Optional[str]:
        """Resolve user provided symbol aliases to StandX native symbols."""
        if not symbol:
            return None

        self._ensure_symbol_cache()
        if not self._symbol_cache:
            return None

        sanitized = symbol.strip().upper().replace("_", "-")
        resolved = self._symbol_cache.get(self._lookup_key(sanitized))
        if resolved:
            return resolved

        # Try without separator
        no_sep = sanitized.replace("-", "")
        for key, value in self._symbol_cache.items():
            if key.replace("-", "") == no_sep:
                return value

        return None

    def _decimal_to_str(self, value: Decimal) -> str:
        """Format Decimal without scientific notation and trim trailing zeros."""
        normalized = value.normalize() if value != 0 else Decimal("0")
        text = format(normalized, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text or "0"
    
    def _calculate_tick_size(self, price_tick_decimals: int) -> str:
        """根据价格精度计算 tick_size"""
        if price_tick_decimals <= 0:
            return "0.01"
        if price_tick_decimals == 1:
            return "0.1"
        return "0." + "0" * (price_tick_decimals - 1) + "1"

    def _find_symbol_suggestions(self, symbol: str, limit: int = 5) -> List[str]:
        """Suggest possible symbols when lookup fails."""
        self._ensure_symbol_cache()
        if not self._market_info_cache:
            return []

        sanitized = symbol.strip().upper().replace("_", "-")
        token = sanitized.replace("-", "")
        candidates: List[str] = []
        seen: Set[str] = set()

        # Fuzzy match by substring
        if token:
            for actual in self._market_info_cache.keys():
                if token in actual.upper().replace("-", "") and actual not in seen:
                    candidates.append(actual)
                    seen.add(actual)
                    if len(candidates) >= limit:
                        return candidates

        return candidates[:limit]

    def _unknown_symbol_error(self, symbol: str) -> Dict[str, Any]:
        suggestions = self._find_symbol_suggestions(symbol)
        message = f"無法解析交易對: {symbol}"
        if suggestions:
            message += f"。可能的交易對: {', '.join(suggestions)}"
            logger.error(message)
            return {"error": message, "status_code": 400, "details": {"candidates": suggestions}}
        logger.error(message)
        return {"error": message, "status_code": 400}

    def make_request(
        self,
        method: str,
        endpoint: str,
        instruction: Optional[Any] = None,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        retry_count: int = 3,
        need_body_signature: bool = False,
    ) -> Dict[str, Any]:
        """Make HTTP request to StandX API."""
        url = f"{self.base_url}{endpoint}"
        signed = bool(instruction)
        
        # 获取 headers
        headers = self._get_auth_headers(need_body_signature=need_body_signature and signed, payload=data if data else None)

        method_upper = method.upper()
        retry_total = retry_count or self.max_retries

        for attempt in range(retry_total):
            try:
                # 记录请求信息（仅记录关键信息，避免日志过多）
                logger.debug("StandX 请求 %s %s", method_upper, url)
                
                if method_upper in {"GET", "DELETE"}:
                    response = self.session.request(
                        method_upper,
                        url,
                        params=params,
                        timeout=self.timeout,
                        headers=headers,
                    )
                else:
                    # ⚠️ 重要：如果请求需要 body signature，必须确保实际发送的请求体
                    # 与签名时使用的 JSON 格式完全一致（排序后的 JSON）
                    # 否则 StandX 服务器验证签名时会失败
                    if need_body_signature and data:
                        # 使用与 body signature 相同的序列化方式（排序后的 JSON）
                        data_json_str = json.dumps(data, separators=(',', ':'), sort_keys=True)
                        response = self.session.request(
                            method_upper,
                            url,
                            data=data_json_str,  # 使用 data 参数发送字符串，而不是 json 参数
                            params=params,
                            timeout=self.timeout,
                            headers=headers,
                        )
                    else:
                        # 不需要 body signature 的请求，使用 json 参数（自动序列化）
                        response = self.session.request(
                            method_upper,
                            url,
                            json=data if data else None,
                            params=params,
                            timeout=self.timeout,
                            headers=headers,
                        )

                if 200 <= response.status_code < 300:
                    return response.json() if response.text else {}

                if response.status_code == 429:
                    wait_time = min(1 * (2 ** attempt), 8)
                    logger.warning("StandX API 達到速率限制，等待 %.1f 秒後重試", wait_time)
                    time.sleep(wait_time)
                    continue

                try:
                    error_body = response.json()
                    message = error_body.get("msg") or error_body.get("message") or error_body.get("error") or str(error_body)
                    
                    # StandX 错误代码映射
                    status_code = response.status_code
                    if status_code == 400:
                        error_type = "Bad Request - Invalid request parameters"
                    elif status_code == 401:
                        error_type = "Unauthorized - Authentication required or invalid token"
                    elif status_code == 403:
                        error_type = "Forbidden - Insufficient permissions"
                        # 如果是 body signature 相关的错误，提供更多信息
                        if "signature" in message.lower() or "body" in message.lower():
                            logger.error("=" * 80)
                            logger.error("❌ Body signature 验证失败")
                            logger.error("=" * 80)
                            logger.error("")
                            logger.error("可能的原因:")
                            logger.error("1. ed25519 密钥对不匹配")
                            logger.error("   - StandX 的 body signature 必须使用认证时生成的同一个 ed25519 密钥对")
                            logger.error("   - 如果使用的密钥对与认证时注册的不一致，会验证失败")
                            logger.error("")
                            logger.error("2. 密钥对未在 StandX 注册")
                            logger.error("   - 认证时 StandX 会注册 ed25519 密钥对")
                            logger.error("   - body signature 必须使用已注册的密钥对")
                            logger.error("")
                            logger.error("3. 签名格式问题")
                            logger.error("   - 签名消息格式: {version},{id},{timestamp},{payload}")
                            logger.error("   - 签名算法: ed25519")
                            logger.error("   - 编码格式: Base64")
                            logger.error("")
                            logger.error("诊断步骤:")
                            logger.error("1. 检查是否配置了 STANDX_ED25519_PRIVATE_KEY_BYTES")
                            logger.error("2. 运行验证工具: python utils/verify_standx_config.py")
                            logger.error("3. 确认密钥对是认证时生成的（运行认证工具时会输出）")
                            logger.error("4. 检查认证时的 request_id 是否与配置的 STANDX_ED25519_REQUEST_ID 匹配")
                            logger.error("")
                            logger.error("解决方案:")
                            logger.error("1. 重新运行认证工具获取 ed25519 密钥对:")
                            logger.error("   python utils/standx_auth.py <private_key>")
                            logger.error("2. 将输出的 ed25519_private_key_bytes 和 ed25519_request_id 添加到 .env 文件")
                            logger.error("3. 确保使用认证时生成的同一个密钥对（不要生成新的）")
                            logger.error("4. 查看 StandX API 文档: https://docs.standx.com/standx-api/perps-auth")
                            logger.error("")
                            logger.error("当前配置状态:")
                            if hasattr(self, '_ed25519_private_key') and self._ed25519_private_key:
                                logger.error("  ✓ ed25519 密钥对已初始化")
                                if self._request_id:
                                    logger.error(f"  ✓ 认证时的 request_id: {self._request_id}")
                                else:
                                    logger.error("  ⚠ 未配置认证时的 request_id")
                            else:
                                logger.error("  ✗ ed25519 密钥对未初始化")
                            logger.error("=" * 80)
                    elif status_code == 404:
                        error_type = "Not Found - Resource not found"
                    elif status_code == 429:
                        error_type = "Too Many Requests - Rate limit exceeded"
                    elif status_code == 500:
                        error_type = "Internal Server Error - Server error"
                    else:
                        error_type = f"HTTP {status_code}"
                    
                    if message and error_type:
                        message = f"{error_type}: {message}"
                except ValueError:
                    message = response.text or f"HTTP {response.status_code}"
                    error_body = {"msg": message}

                if attempt < retry_total - 1 and response.status_code >= 500:
                    time.sleep(1)
                    continue

                return {"error": message, "status_code": response.status_code, "details": error_body}
            except requests.RequestException as exc:
                if attempt < retry_total - 1:
                    logger.warning("StandX API 請求異常 (%s)，重試中...", exc)
                    time.sleep(1)
                    continue
                return {"error": f"請求失敗: {exc}"}

        return {"error": "達到最大重試次數"}

    def get_deposit_address(self, blockchain: str) -> Dict[str, Any]:
        return {"error": "請使用 StandX 網頁界面獲取充值地址"}

    def get_balance(self) -> Dict[str, Any]:
        """獲取賬戶餘額"""
        result = self.make_request(
            "GET",
            "/api/query_balance",
            instruction=True,
            retry_count=self.max_retries,
        )

        if isinstance(result, dict) and "error" in result:
            return result

        # StandX 返回格式
        balances: Dict[str, Dict[str, Any]] = {}
        
        # StandX 使用 DUSD 作为结算资产
        equity = float(result.get("equity", 0))
        balance = float(result.get("balance", 0))
        cross_available = float(result.get("cross_available", 0))
        locked = float(result.get("locked", 0))
        
        balances["DUSD"] = {
            "available": cross_available,
            "locked": locked,
            "total": balance,
            "equity": equity,
            "asset": "DUSD",
            "raw": result,
        }

        return balances

    def get_collateral(self, subaccount_id: Optional[str] = None) -> Dict[str, Any]:
        """獲取抵押品餘額"""
        result = self.make_request(
            "GET",
            "/api/query_balance",
            instruction=True,
            retry_count=self.max_retries,
        )

        if isinstance(result, dict) and "error" in result:
            return result

        return {
            "totalCollateral": result.get("balance", "0"),
            "availableCollateral": result.get("cross_available", "0"),
            "initialMargin": result.get("cross_margin", "0"),
            "maintenanceMargin": "0",  # StandX 可能不直接提供
            "token": "DUSD",
            "raw": result
        }

    def execute_order(self, order_details: Dict[str, Any]) -> Dict[str, Any]:
        """執行訂單"""
        symbol = order_details.get("symbol")
        if not symbol:
            return {"error": "缺少交易對", "status_code": 400}

        resolved_symbol = self._resolve_symbol(symbol)
        if not resolved_symbol:
            return self._unknown_symbol_error(symbol)

        side = order_details.get("side")
        if not side:
            return {"error": "缺少買賣方向"}

        # StandX 使用 "buy" 和 "sell"
        if side.lower() in {"bid", "buy"}:
            normalized_side = "buy"
        elif side.lower() in {"ask", "sell"}:
            normalized_side = "sell"
        else:
            return {"error": f"不支持的方向: {side}"}

        order_type = order_details.get("orderType") or order_details.get("type")
        if not order_type:
            return {"error": "缺少訂單類型"}

        # StandX 使用 "limit" 和 "market"
        normalized_type = order_type.lower()
        if normalized_type not in ["limit", "market"]:
            return {"error": f"不支持的訂單類型: {order_type}"}

        # 構建請求體（StandX 要求 decimal 參數用字符串）
        payload: Dict[str, Any] = {
            "symbol": resolved_symbol,
            "side": normalized_side,
            "order_type": normalized_type,
        }

        # 數量（必須是字符串）
        quantity = order_details.get("quantity") or order_details.get("size")
        if quantity is not None:
            payload["qty"] = str(quantity)
        else:
            return {"error": "缺少訂單數量"}

        # 價格（限價單需要，必須是字符串）
        price = order_details.get("price")
        if normalized_type == "limit":
            if price is None:
                return {"error": "限價單需要價格"}
            payload["price"] = str(price)
        elif normalized_type == "market":
            # 市價單不需要價格
            pass

        # Time in force (StandX 支持: gtc, ioc, alo)
        time_in_force = order_details.get("timeInForce", "GTC")
        tif_mapping = {
            "GTC": "gtc",
            "IOC": "ioc",
            "FOK": "ioc",  # StandX 不支持 FOK，使用 IOC
            "POST_ONLY": "alo",  # StandX 使用 alo (Add Liquidity Only) 代替 post_only
            "ALO": "alo",
        }
        payload["time_in_force"] = tif_mapping.get(time_in_force.upper(), "gtc")

        # Reduce only（必需参数）
        # 根据 StandX 文档，reduce_only 是必需参数
        if "reduceOnly" in order_details:
            payload["reduce_only"] = bool(order_details["reduceOnly"])
        else:
            # 如果没有提供，默认为 False
            payload["reduce_only"] = False

        # Client order ID
        if "clientId" in order_details:
            payload["cl_ord_id"] = order_details["clientId"]

        # Leverage 和 margin_mode（可選）
        if "leverage" in order_details:
            payload["leverage"] = int(order_details["leverage"])
        if "marginMode" in order_details:
            payload["margin_mode"] = order_details["marginMode"].lower()

        result = self.make_request(
            "POST",
            "/api/new_order",
            instruction=True,
            data=payload,
            need_body_signature=True,
            retry_count=self.max_retries,
        )

        if isinstance(result, dict) and "error" in result:
            return result

        # StandX 返回格式: {"code": 0, "message": "success", "request_id": "xxx"}
        if result.get("code") == 0:
            return {"success": True, "request_id": result.get("request_id")}
        else:
            return {"error": result.get("message", "訂單提交失敗"), "details": result}

    def get_open_orders(self, symbol: Optional[str] = None) -> Any:
        """獲取開放訂單"""
        params: Dict[str, Any] = {}
        if symbol:
            resolved_symbol = self._resolve_symbol(symbol)
            if not resolved_symbol:
                return self._unknown_symbol_error(symbol)
            params["symbol"] = resolved_symbol

        result = self.make_request(
            "GET",
            "/api/query_open_orders",
            instruction=True,
            params=params,
            retry_count=self.max_retries,
        )

        if isinstance(result, dict) and "error" in result:
            return result

        # StandX 返回格式: {"page_size": N, "result": [...], "total": N}
        orders = result.get("result", []) if isinstance(result, dict) else result
        if not isinstance(orders, list):
            orders = [orders] if orders else []

        normalized: List[Dict[str, Any]] = []
        for item in orders:
            normalized.append(self._normalize_order_fields(dict(item)))
        return normalized

    def cancel_all_orders(self, symbol: str) -> Dict[str, Any]:
        """取消所有訂單"""
        # 先獲取所有開放訂單
        open_orders = self.get_open_orders(symbol)
        if isinstance(open_orders, dict) and "error" in open_orders:
            return open_orders

        if not open_orders:
            return {"success": True, "message": "沒有開放訂單"}

        # 構建取消請求
        order_ids = [order.get("id") for order in open_orders if order.get("id")]
        
        if not order_ids:
            return {"success": True, "message": "沒有可取消的訂單"}

        payload = {
            "order_id_list": order_ids
        }

        result = self.make_request(
            "POST",
            "/api/cancel_orders",
            instruction=True,
            data=payload,
            need_body_signature=True,
            retry_count=self.max_retries,
        )

        if isinstance(result, dict) and "error" in result:
            return result

        # StandX 返回空數組表示成功
        return {"success": True, "cancelled_count": len(order_ids)}

    def cancel_order(self, order_id: str, symbol: str) -> Dict[str, Any]:
        """取消指定訂單"""
        # StandX 支持 order_id 或 cl_ord_id
        try:
            order_id_int = int(order_id)
            payload = {"order_id": order_id_int}
        except ValueError:
            payload = {"cl_ord_id": order_id}

        result = self.make_request(
            "POST",
            "/api/cancel_order",
            instruction=True,
            data=payload,
            need_body_signature=True,
            retry_count=self.max_retries,
        )

        if isinstance(result, dict) and "error" in result:
            return result

        # StandX 返回格式: {"code": 0, "message": "success", "request_id": "xxx"}
        if result.get("code") == 0:
            return {"success": True, "request_id": result.get("request_id")}
        else:
            return {"error": result.get("message", "取消訂單失敗"), "details": result}

    def get_ticker(self, symbol: str) -> Dict[str, Any]:
        """獲取行情信息"""
        resolved_symbol = self._resolve_symbol(symbol)
        if not resolved_symbol:
            return self._unknown_symbol_error(symbol)

        result = self.make_request(
            "GET",
            "/api/query_symbol_price",
            params={"symbol": resolved_symbol},
            retry_count=self.max_retries,
        )

        if isinstance(result, dict) and "error" in result:
            return result

        # StandX 返回格式
        data = result
        if "last_price" not in data and "lastPrice" in data:
            data["last_price"] = data["lastPrice"]
        elif "last_price" not in data:
            # 使用 mark_price 或 mid_price 作為備選
            data["last_price"] = data.get("mark_price") or data.get("mid_price") or "0"

        return data

    def get_markets(self) -> Dict[str, Any]:
        """獲取市場信息"""
        # StandX 沒有直接獲取所有交易對的端點
        # 根據 API Reference，已知的交易對包括：BTC-USD 等
        # 這裡返回已知的交易對列表，實際使用時可以查詢特定交易對的詳細信息
        known_symbols = [
            {"symbol": "BTC-USD", "base_asset": "BTC", "quote_asset": "USD"},
            # 可以添加更多已知的交易對
        ]
        return known_symbols

    def get_order_book(self, symbol: str, limit: int = 20) -> Dict[str, Any]:
        """獲取訂單簿"""
        resolved_symbol = self._resolve_symbol(symbol)
        if not resolved_symbol:
            return self._unknown_symbol_error(symbol)

        result = self.make_request(
            "GET",
            "/api/query_depth_book",
            params={"symbol": resolved_symbol},
            retry_count=self.max_retries,
        )

        if isinstance(result, dict) and "error" in result:
            return result

        data = result
        bids = data.get("bids", [])
        asks = data.get("asks", [])

        # Sort bids descending, asks ascending
        try:
            bids = sorted(bids, key=lambda level: float(level[0]), reverse=True)
            asks = sorted(asks, key=lambda level: float(level[0]))
        except (ValueError, TypeError, IndexError):
            pass

        return {"bids": bids, "asks": asks}

    def get_fill_history(self, symbol: Optional[str] = None, limit: int = 100) -> Any:
        """獲取成交歷史"""
        params = {"limit": limit}
        if symbol:
            resolved_symbol = self._resolve_symbol(symbol)
            if not resolved_symbol:
                return self._unknown_symbol_error(symbol)
            params["symbol"] = resolved_symbol

        result = self.make_request(
            "GET",
            "/api/query_trades",
            instruction=True,
            params=params,
            retry_count=self.max_retries,
        )

        if isinstance(result, dict) and "error" in result:
            return result

        # StandX 返回格式: {"page_size": N, "result": [...], "total": N}
        trades = result.get("result", []) if isinstance(result, dict) else result
        return trades if isinstance(trades, list) else []

    def get_klines(self, symbol: str, interval: str = "1h", limit: int = 100) -> Any:
        """獲取K線數據"""
        resolved_symbol = self._resolve_symbol(symbol)
        if not resolved_symbol:
            return self._unknown_symbol_error(symbol)

        # StandX 使用 resolution 參數，需要映射 interval
        # StandX 支持的 resolution: 1T, 3S, 1, 5, 15, 60, 1D, 1W, 1M
        resolution_mapping = {
            "1t": "1T",  # 1 tick
            "3s": "3S",  # 3 seconds
            "1m": "1",   # 1 minute
            "5m": "5",   # 5 minutes
            "15m": "15", # 15 minutes
            "30m": "30", # 30 minutes (可能需要使用 15 或 60)
            "1h": "60",  # 1 hour
            "4h": "240", # 4 hours (可能需要使用 60)
            "1d": "1D",  # 1 day
            "1w": "1W",  # 1 week
            "1M": "1M",  # 1 month
        }
        resolution = resolution_mapping.get(interval.lower(), interval)

        # 計算時間範圍
        to_time = int(time.time())
        from_time = to_time - (limit * 60)  # 假設是分鐘級別

        params = {
            "symbol": resolved_symbol,
            "from": from_time,
            "to": to_time,
            "resolution": resolution,
        }

        result = self.make_request(
            "GET",
            "/api/kline/history",
            params=params,
            retry_count=self.max_retries,
        )

        if isinstance(result, dict) and "error" in result:
            return result

        return result

    def get_market_limits(self, symbol: str) -> Optional[Dict[str, Any]]:
        """獲取市場限制信息"""
        resolved_symbol = self._resolve_symbol(symbol)
        if not resolved_symbol:
            self._unknown_symbol_error(symbol)
            return None

        result = self.make_request(
            "GET",
            "/api/query_symbol_info",
            params={"symbol": resolved_symbol},
            retry_count=self.max_retries,
        )

        if isinstance(result, dict) and "error" in result:
            return None

        # StandX 返回數組格式
        symbol_info = result[0] if isinstance(result, list) and result else result

        if not symbol_info:
            return None

        return {
            "symbol": symbol_info.get("symbol"),
            "base_asset": symbol_info.get("base_asset"),
            "quote_asset": symbol_info.get("quote_asset"),
            "market_type": "PERP",
            "status": "TRADING" if symbol_info.get("enabled") else "INACTIVE",
            "min_order_size": symbol_info.get("min_order_qty", "0.001"),
            "tick_size": self._calculate_tick_size(symbol_info.get("price_tick_decimals", 2)),
            "base_precision": symbol_info.get("qty_tick_decimals", 3),
            "quote_precision": symbol_info.get("price_tick_decimals", 2),
        }

    def get_positions(self, symbol: Optional[str] = None) -> Any:
        """獲取持倉信息"""
        params: Dict[str, Any] = {}
        if symbol:
            resolved_symbol = self._resolve_symbol(symbol)
            if not resolved_symbol:
                return self._unknown_symbol_error(symbol)
            params["symbol"] = resolved_symbol

        result = self.make_request(
            "GET",
            "/api/query_positions",
            instruction=True,
            params=params,
            retry_count=self.max_retries,
        )

        if isinstance(result, dict) and "error" in result:
            return result

        positions_raw = result if isinstance(result, list) else []

        normalized: List[Dict[str, Any]] = []
        for item in positions_raw:
            item_symbol = item.get("symbol", "")

            # Filter by symbol if specified
            if symbol:
                resolved = self._resolve_symbol(symbol)
                if resolved and item_symbol != resolved:
                    continue

            raw_qty = item.get("qty", "0") or "0"
            try:
                pos_dec = Decimal(str(raw_qty))
            except (InvalidOperation, TypeError):
                pos_dec = Decimal("0")

            # 跳過 size 為 0 的無效倉位
            if pos_dec == 0:
                continue

            # StandX 的 qty 正負表示方向
            if pos_dec > 0:
                mapped_side = "LONG"
            elif pos_dec < 0:
                mapped_side = "SHORT"
            else:
                mapped_side = "FLAT"

            long_dec = abs(pos_dec) if mapped_side == "LONG" else Decimal("0")
            short_dec = abs(pos_dec) if mapped_side == "SHORT" else Decimal("0")

            entry_price = item.get("entry_price")
            unrealized = item.get("upnl")

            # netQuantity: 多頭為正，空頭為負
            net_qty = abs(pos_dec) if mapped_side == "LONG" else -abs(pos_dec)

            normalized.append({
                "symbol": item_symbol,
                "side": mapped_side,
                "positionSide": mapped_side,
                "netQuantity": self._decimal_to_str(net_qty),
                "longQuantity": self._decimal_to_str(long_dec),
                "shortQuantity": self._decimal_to_str(short_dec),
                "size": self._decimal_to_str(abs(pos_dec)),
                "entryPrice": entry_price,
                "pnlUnrealized": unrealized,
                "unrealizedPnl": unrealized,
                "raw": item,
            })

        return normalized
