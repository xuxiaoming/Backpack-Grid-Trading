# StandX 认证指南

根据 StandX 官方文档实现完整的认证流程。

官方文档: https://docs.standx.com/standx-api/standx-api#perps-auth

## 认证流程概述

StandX 使用钱包签名认证，完整流程包括：

1. **生成临时的 ed25519 密钥对** - 用于请求签名
2. **获取签名数据** - 从服务器获取需要签名的消息（JWT格式）
3. **解析签名数据** - 从 JWT 中提取需要签名的消息
4. **钱包签名** - 使用 MetaMask 私钥签名消息
5. **获取访问令牌** - 提交签名获取最终的 JWT Token

## 方法一：使用自动认证工具（推荐）

1. **安装依赖**
   ```bash
   pip install eth-account web3 PyJWT cryptography base58 pynacl
   ```
   或者安装所有依赖：
   ```bash
   pip install -r requirements.txt
   ```

2. **运行认证工具**
   ```bash
   # 方式1: 通过环境变量（推荐）
   export METAMASK_PRIVATE_KEY=your_private_key
   export STANDX_CHAIN=bsc  # 或 solana
   python utils/standx_auth.py
   
   # 方式2: 通过命令行参数
   python utils/standx_auth.py your_private_key bsc
   # 参数: [私钥] [链名称: bsc 或 solana]
   
   # 方式3: 交互式输入
   python utils/standx_auth.py
   ```

3. **获取结果**
   - 如果成功，工具会输出 JWT Token
   - 将 Token 添加到 `.env` 文件：
     ```
     STANDX_JWT_TOKEN=your_jwt_token_here
     STANDX_BASE_URL=https://perps.standx.com
     ```
   - Token 默认有效期为 7 天，过期后需要重新获取

### 方法二：通过 StandX 网站手动获取

如果自动认证工具无法工作，可以尝试通过网站获取：

1. **访问 StandX 网站**
   - 打开 https://perps.standx.com
   - 点击"连接钱包"或"登录"

2. **连接 MetaMask 钱包**
   - 选择 MetaMask 钱包
   - 确认连接请求

3. **查找 API 设置**
   - 登录后，进入账户设置
   - 查找"API"、"开发者"或"API Keys"选项

4. **配置环境变量**
   在 `.env` 文件中添加：
   ```
   STANDX_JWT_TOKEN=your_jwt_token_here
   STANDX_BASE_URL=https://perps.standx.com
   ```

**注意**: 根据 StandX 官方文档，认证主要通过钱包签名完成，可能不需要单独的 API Key。

### 方法三：在代码中使用 StandXAuth 类

如果需要在代码中集成认证功能，可以直接使用 `StandXAuth` 类：

```python
from utils.standx_auth import StandXAuth

# 创建认证对象
auth = StandXAuth()

# 执行认证
private_key = "your_private_key"
chain = "bsc"  # 或 "solana"

# 获取钱包地址
from eth_account import Account
account = Account.from_key(private_key)
wallet_address = account.address

# 认证
login_response = auth.authenticate(chain, wallet_address, private_key)

if login_response:
    token = login_response["token"]
    print(f"JWT Token: {token}")
```

### 方法四：查看 StandX API 文档

如果遇到问题，请查看 StandX 官方 API 文档：

- API 文档: https://docs.standx.com/standx-api/standx-api
- 认证文档: https://docs.standx.com/standx-api/standx-api#perps-auth

### 注意事项

1. **私钥安全**
   - ⚠️ **永远不要**将私钥提交到代码仓库
   - ⚠️ **永远不要**在公共场合分享私钥
   - 使用环境变量或 `.env` 文件存储私钥（确保 `.env` 在 `.gitignore` 中）

2. **JWT Token 过期**
   - JWT Token 默认有效期为 7 天（604800 秒）
   - 可以在认证时通过 `expiresSeconds` 参数自定义过期时间
   - 如果 Token 过期，需要重新运行认证工具获取新 Token
   - 建议设置较短的过期时间以提高安全性

3. **支持的区块链**
   - 目前支持 `bsc` (Binance Smart Chain) 和 `solana`
   - 默认使用 `bsc`
   - 可以通过环境变量 `STANDX_CHAIN` 或命令行参数指定

4. **API Key vs JWT Token**
   - 根据 StandX 官方文档，认证主要通过 JWT Token 完成
   - 可能不需要单独的 API Key
   - 某些 API 端点可能需要额外的请求签名（Body Signature Flow）

### 验证配置

配置完成后，可以通过以下方式验证：

```bash
# 使用 CLI 模式查询余额
python run.py --cli
# 选择 2 - 查询余额
# 如果能看到 StandX 的余额信息，说明配置成功
```

### 常见问题

**Q: 工具提示"获取签名数据失败"**
A: 
- 检查网络连接
- 确认 StandX API 服务是否正常
- 确认钱包地址格式正确
- 尝试使用不同的链（bsc 或 solana）

**Q: JWT Token 过期了怎么办？**
A: 重新运行认证工具获取新 Token。Token 默认有效期为 7 天。

**Q: 需要 API Key 吗？**
A: 根据 StandX 官方文档，认证主要通过 JWT Token 完成，可能不需要单独的 API Key。

**Q: 私钥格式要求？**
A: 支持带 `0x` 前缀或不带前缀的私钥，工具会自动处理。

**Q: 支持哪些区块链？**
A: 目前支持 `bsc` (Binance Smart Chain) 和 `solana`。默认使用 `bsc`。

**Q: 认证失败怎么办？**
A: 
- 检查私钥是否正确
- 确认钱包地址已连接到 StandX
- 查看错误信息，根据提示排查问题
- 参考 StandX 官方文档: https://docs.standx.com/standx-api/standx-api

### 技术支持

如果遇到问题：
1. 查看 StandX API 文档: https://docs.standx.com/standx-api/standx-api
2. 联系 StandX 技术支持
3. 检查网络连接和 API 端点是否正确

