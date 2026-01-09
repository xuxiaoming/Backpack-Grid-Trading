# StandX Body Signature 问题排查

## 错误信息

如果遇到以下错误：
```
Forbidden - Insufficient permissions: invalid body signature
```

## 问题分析

StandX 的 body signature 需要使用 ed25519 密钥对。根据 StandX 文档，body signature 的流程是：

1. 生成 ed25519 密钥对
2. 使用 base58 编码的公钥作为 requestId（用于 prepare-signin 认证）
3. 对于每个需要签名的请求，使用 ed25519 私钥签名消息

## 可能的原因

### 1. ed25519 密钥对未在 StandX 注册

StandX 可能需要在认证时注册 ed25519 密钥对。如果客户端生成的密钥对与认证时使用的不同，会导致签名验证失败。

### 2. 签名格式不正确

确保签名消息格式为：`{version},{id},{timestamp},{payload}`

### 3. 密钥对不匹配

如果 StandX 要求在认证时注册密钥对，那么 body signature 必须使用认证时生成的同一个密钥对。

## 解决方案

### 方案一：使用认证时生成的密钥对（推荐）

如果 StandX 要求在认证时注册 ed25519 密钥对，需要：

1. **在认证时保存密钥对信息**

   运行认证工具时，会生成 ed25519 密钥对。如果 StandX 要求使用认证时的密钥对，需要保存这些信息。

2. **在客户端配置中使用认证时的密钥对**

   将认证时生成的 ed25519 密钥对添加到配置中（如果 StandX 支持这种方式）。

### 方案二：检查 StandX 文档

StandX 文档可能说明了 body signature 的具体要求。请查看：
- StandX API 文档: https://docs.standx.com/standx-api/standx-api
- 认证文档: https://docs.standx.com/standx-api/standx-api#perps-auth

### 方案三：联系 StandX 技术支持

如果以上方案都不行，建议：
1. 联系 StandX 技术支持
2. 提供错误信息：`invalid body signature`
3. 询问 body signature 的具体要求和 ed25519 密钥对的注册流程

## 当前实现

当前实现中：
- StandXClient 在初始化时自动生成 ed25519 密钥对
- 每次请求使用这个密钥对生成 body signature
- 如果 StandX 要求在认证时注册密钥对，可能需要调整实现

## 调试信息

启用调试日志可以查看 body signature 的详细信息：

```python
import logging
logging.basicConfig(level=logging.DEBUG)
```

日志会显示：
- Body signature 消息内容
- 签名长度和格式
- Base64 编码后的签名

## 临时解决方案

如果 body signature 一直失败，可以尝试：

1. **检查 StandX 是否真的需要 body signature**
   - 某些端点可能不需要 body signature
   - 查看 StandX API 文档确认哪些端点需要签名

2. **使用 StandX 网站手动操作**
   - 如果 API 调用有问题，可以暂时使用 StandX 网站手动操作
   - 同时联系技术支持获取帮助

3. **检查 JWT Token 是否有效**
   - 确保 JWT Token 未过期
   - 重新运行认证工具获取新 Token

