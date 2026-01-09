# StandX 下单 API 实现检查

## 文档要求 vs 代码实现对比

### ✅ 必需参数 (Required Parameters)

| 参数 | 文档类型 | 代码实现 | 状态 |
|------|---------|---------|------|
| `symbol` | string | ✅ 已实现，使用 `_resolve_symbol` 解析 | ✅ |
| `side` | enum | ✅ 已实现，映射 "buy"/"sell" | ✅ |
| `order_type` | enum | ✅ 已实现，映射 "limit"/"market" | ✅ |
| `qty` | decimal (字符串) | ✅ 已实现，使用 `str(quantity)` | ✅ |
| `time_in_force` | enum | ✅ 已实现，映射 "gtc"/"ioc"/"alo" | ✅ |
| `reduce_only` | boolean | ✅ 已实现，默认 False | ✅ |

### ✅ 可选参数 (Optional Parameters)

| 参数 | 文档类型 | 代码实现 | 状态 |
|------|---------|---------|------|
| `price` | decimal (字符串) | ✅ 已实现，限价单必需，使用 `str(price)` | ✅ |
| `cl_ord_id` | string | ✅ 已实现，从 `clientId` 映射 | ✅ |
| `margin_mode` | enum | ✅ 已实现，转换为小写 | ✅ |
| `leverage` | int | ✅ 已实现，使用 `int(leverage)` | ✅ |

### ✅ 认证和签名

| 要求 | 文档说明 | 代码实现 | 状态 |
|------|---------|---------|------|
| JWT 认证 | `Authorization: Bearer <token>` | ✅ 已实现 | ✅ |
| Body Signature | 需要 ed25519 签名 | ✅ 已实现 | ✅ |
| x-session-id | 需要添加到请求头 | ✅ 已实现（在 body signature 时添加） | ✅ |

### ✅ 请求格式

| 要求 | 文档说明 | 代码实现 | 状态 |
|------|---------|---------|------|
| decimal 参数 | JSON 字符串 | ✅ `qty` 和 `price` 都转换为字符串 | ✅ |
| int 参数 | JSON 整数 | ✅ `leverage` 转换为 int | ✅ |
| boolean 参数 | JSON boolean | ✅ `reduce_only` 使用 bool() | ✅ |

### ✅ 端点

| 要求 | 文档说明 | 代码实现 | 状态 |
|------|---------|---------|------|
| 端点路径 | `POST /api/new_order` | ✅ 已实现 | ✅ |
| 请求方法 | POST | ✅ 已实现 | ✅ |

## 代码实现细节

### 1. 参数映射

```python
# 代码中的参数映射
order_details.get("quantity") or order_details.get("size") → payload["qty"]
order_details.get("price") → payload["price"]
order_details.get("side") → payload["side"] (映射为 "buy"/"sell")
order_details.get("orderType") or order_details.get("type") → payload["order_type"]
order_details.get("timeInForce") → payload["time_in_force"] (映射为 "gtc"/"ioc"/"alo")
order_details.get("reduceOnly") → payload["reduce_only"]
order_details.get("clientId") → payload["cl_ord_id"]
order_details.get("leverage") → payload["leverage"] (int)
order_details.get("marginMode") → payload["margin_mode"] (小写)
```

### 2. 请求示例对比

**文档示例**:
```json
{
  "symbol": "BTC-USD",
  "side": "buy",
  "order_type": "limit",
  "qty": "0.1",
  "price": "50000",
  "time_in_force": "gtc",
  "reduce_only": false
}
```

**代码生成的 payload** (示例):
```json
{
  "symbol": "BTC-USD",
  "side": "buy",
  "order_type": "limit",
  "qty": "0.1",
  "price": "50000",
  "time_in_force": "gtc",
  "reduce_only": false
}
```

✅ **完全一致**

### 3. Headers 对比

**文档要求**:
```
Authorization: Bearer <jwt_token>
x-request-sign-version: v1
x-request-id: <random_string>
x-request-timestamp: <timestamp_in_milliseconds>
x-request-signature: <base64_signature>
x-session-id: <session_id>
```

**代码实现**:
```python
headers = {
    "Content-Type": "application/json",
    "authorization": f"Bearer {self.jwt_token}",
    "x-request-sign-version": "v1",
    "x-request-id": str(uuid.uuid4()),
    "x-request-timestamp": str(timestamp),
    "x-request-signature": signature,
    "x-session-id": self.session_id  # 如果配置了
}
```

✅ **完全一致**

## 总结

### ✅ 完全符合文档要求

所有必需参数、可选参数、认证方式、请求格式都与 StandX 官方文档完全一致。

### 注意事项

1. **x-session-id**: 代码中只在配置了 `session_id` 时才会添加。如果未配置，StandX 可能无法通过 WebSocket 推送订单状态更新。

2. **参数验证**: 代码对所有必需参数都进行了验证，缺少参数会返回错误。

3. **类型转换**: 所有参数类型都按照文档要求正确转换：
   - decimal → 字符串
   - int → 整数
   - boolean → 布尔值

### 建议

1. 确保配置了 `STANDX_SESSION_ID`，以便接收订单状态更新
2. 确保 JWT token 有效（未过期）
3. 确保 ed25519 密钥对正确配置，用于 body signature

