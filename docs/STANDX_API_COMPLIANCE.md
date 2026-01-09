# StandX API 合规性检查报告

根据 [StandX 官方文档](https://docs.standx.com/standx-api/perps-http) 检查代码实现。

## ✅ 已正确实现的部分

### 1. 认证 (Authentication)
- ✅ JWT Token 认证：`Authorization: Bearer <token>` 已正确实现
- ✅ Token 有效期：7 天（由认证工具管理）

### 2. Body Signature
- ✅ `x-request-sign-version: v1` 已实现
- ✅ `x-request-timestamp: <timestamp_in_milliseconds>` 已实现（使用毫秒时间戳）
- ✅ `x-request-signature: <base64_signature>` 已实现
- ✅ 签名消息格式：`{version},{id},{timestamp},{payload}` 已正确实现
- ✅ 使用 ed25519 私钥签名
- ✅ Base64 编码签名结果

### 3. Session ID
- ✅ `x-session-id` header 已实现
- ✅ 在 `new_order` 和 `cancel_order` 请求中添加

### 4. 请求格式
- ✅ `decimal` 参数（如 `qty`, `price`）已转换为 JSON 字符串
- ✅ `int` 参数（如 `order_id`, `leverage`）已转换为 JSON 整数

### 5. 下单参数
- ✅ `symbol`: 字符串
- ✅ `side`: "buy" 或 "sell"
- ✅ `order_type`: "limit" 或 "market"
- ✅ `qty`: 字符串格式的 decimal
- ✅ `price`: 字符串格式的 decimal（限价单必需）
- ✅ `time_in_force`: "gtc", "ioc", "alo"
- ✅ `reduce_only`: boolean
- ✅ `cl_ord_id`: 字符串（可选）
- ✅ `leverage`: int（可选）
- ✅ `margin_mode`: 字符串（可选）

## ⚠️ 潜在问题

### 1. Body Signature 的 `x-request-id`

**文档要求**：
```
x-request-id: <random_string>
```

**当前实现**：
```python
request_id = self._request_id or str(uuid.uuid4())
```

**问题分析**：
- `self._request_id` 是认证时生成的 base58 编码的公钥（用于 `prepare-signin`）
- 文档说 body signature 的 `x-request-id` 应该是 `<random_string>`
- 代码中如果 `self._request_id` 存在，会使用它；否则使用 UUID

**建议**：
- Body signature 的 `x-request-id` 应该使用随机字符串（UUID），而不是认证时的 `request_id`
- 认证时的 `request_id`（base58 编码的公钥）只用于 `prepare-signin` 步骤
- Body signature 的 `x-request-id` 应该每次都生成新的 UUID

**修复建议**：
```python
# 应该始终使用 UUID 作为 body signature 的 request_id
request_id = str(uuid.uuid4())
```

### 2. Payload JSON 序列化

**当前实现**：
```python
payload_str = json.dumps(payload, separators=(',', ':'), sort_keys=True)
```

**文档要求**：
- 文档没有明确要求排序，但排序可以确保一致性
- 使用 `separators=(',', ':')` 去除空格是正确的

**状态**：✅ 当前实现应该是正确的

### 3. 时间戳格式

**文档要求**：
```
x-request-timestamp: <timestamp_in_milliseconds>
```

**当前实现**：
```python
def _current_timestamp(self) -> int:
    """获取当前时间戳（毫秒）"""
    return int(time.time() * 1000)
```

**状态**：✅ 已正确实现（毫秒时间戳）

## 🔍 需要验证的部分

### 1. Body Signature 的 ed25519 密钥对

**问题**：
- 认证时生成的 ed25519 密钥对是否需要在 StandX 注册？
- Body signature 是否必须使用认证时生成的同一个密钥对？

**当前实现**：
- 代码支持从配置中读取认证时生成的 ed25519 密钥对
- 如果没有提供，会生成新的密钥对（可能导致验证失败）

**建议**：
- 根据 StandX 文档，body signature 需要使用认证时生成的 ed25519 密钥对
- 确保 `STANDX_ED25519_PRIVATE_KEY_BYTES` 和 `STANDX_ED25519_REQUEST_ID` 正确配置

## 📝 修复建议

### 修复 1: Body Signature 的 request_id

在 `api/standx_client.py` 的 `_get_auth_headers` 方法中：

```python
# 修改前
request_id = self._request_id or str(uuid.uuid4())

# 修改后
# Body signature 的 x-request-id 应该是随机字符串（UUID）
# 认证时的 request_id（base58 编码的公钥）只用于 prepare-signin
request_id = str(uuid.uuid4())
```

### 修复 2: 确保所有必需参数都正确

检查 `execute_order` 方法确保：
- ✅ `symbol`: 必需
- ✅ `side`: 必需
- ✅ `order_type`: 必需
- ✅ `qty`: 必需（字符串格式）
- ✅ `time_in_force`: 必需
- ✅ `reduce_only`: 必需（boolean）
- ✅ `price`: 限价单必需（字符串格式）

## 📊 总结

| 项目 | 状态 | 说明 |
|------|------|------|
| JWT 认证 | ✅ | 已正确实现 |
| Body Signature | ⚠️ | 需要修复 `x-request-id` 使用 UUID |
| Session ID | ✅ | 已正确实现 |
| 请求格式 | ✅ | int 和 decimal 格式正确 |
| 参数映射 | ✅ | 所有参数映射正确 |

## 🔗 参考文档

- [StandX Perps HTTP API](https://docs.standx.com/standx-api/perps-http)
- [StandX Perps Auth](https://docs.standx.com/standx-api/perps-auth)

