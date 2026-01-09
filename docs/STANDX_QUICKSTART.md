# StandX 快速启动指南

## 步骤 1: 获取 JWT Token

### 方法一：使用认证工具（推荐）

1. **安装依赖**
   ```bash
   pip install eth-account web3 PyJWT cryptography base58 pynacl
   ```

2. **运行认证工具**
   ```bash
   # 方式1: 通过环境变量
   export METAMASK_PRIVATE_KEY=your_private_key
   export STANDX_CHAIN=bsc  # 或 solana
   python utils/standx_auth.py
   
   # 方式2: 通过命令行参数
   python utils/standx_auth.py your_private_key bsc
   ```

3. **复制 JWT Token**
   认证成功后，工具会输出 JWT Token，复制它。

### 方法二：通过 StandX 网站

1. 访问 https://perps.standx.com
2. 使用 MetaMask 连接钱包
3. 在账户设置中查找 API 或开发者选项
4. 获取 JWT Token

## 步骤 2: 配置环境变量

创建或编辑 `.env` 文件，添加以下内容：

```bash
# StandX 配置
STANDX_JWT_TOKEN=your_jwt_token_here
STANDX_BASE_URL=https://perps.standx.com

# StandX Body Signature（必需，用于签名请求）
STANDX_ED25519_PRIVATE_KEY_BYTES=your_ed25519_private_key_hex
STANDX_ED25519_REQUEST_ID=your_ed25519_request_id
```

**重要提示**：
- JWT Token 是必需的，用于 API 认证
- Token 默认有效期为 7 天，过期后需要重新获取
- 确保 `.env` 文件在 `.gitignore` 中，不要提交到代码仓库

## 步骤 3: 启动 StandX

### 方式一：命令行直接运行（推荐）

#### 永续合约做市

```bash
python run.py \
  --exchange standx \
  --symbol BTC-USD \
  --spread 0.3 \
  --market-type perp \
  --target-position 1.0 \
  --max-position 1.0 \
  --duration 3600 \
  --interval 60
```

#### 永续合约网格策略

```bash
python run.py \
  --exchange standx \
  --symbol BTC-USD \
  --strategy perp_grid \
  --auto-price \
  --grid-num 10 \
  --market-type perp \
  --max-position 1.0
```

#### 带止损止盈的做市

```bash
python run.py \
  --exchange standx \
  --symbol BTC-USD \
  --spread 0.3 \
  --market-type perp \
  --target-position 1.0 \
  --max-position 1.0 \
  --stop-loss -25 \
  --take-profit 50 \
  --duration 3600 \
  --interval 60
```

### 方式二：CLI 交互模式

```bash
python run.py --cli
```

然后在菜单中选择：
1. 选择 `5 - 執行現貨/合約做市/對沖/網格 策略`
2. 选择交易所时输入 `standx`
3. 选择市场类型（选择 `perp` 永续合约）
4. 选择策略并配置参数

### 方式三：Web 界面

```bash
python run.py --web
```

然后在浏览器中打开 http://localhost:5000，在 Web 界面中选择 StandX 交易所。

## 常用命令示例

### 查询余额

```bash
python run.py --cli
# 选择 2 - 查詢餘額
```

### 查询持仓

```bash
python run.py --cli
# 选择 2 - 查詢餘額（会显示持仓信息）
```

### 查看市场信息

```bash
python run.py --cli
# 选择 3 - 獲取市場信息
```

## StandX 交易对格式

StandX 使用以下格式的交易对：
- `BTC-USD` (不是 BTC_USDC)
- `ETH-USD`
- 等等

注意：StandX 使用 `-` 作为分隔符，不是 `_`。

## 参数说明

### 基本参数

- `--exchange standx`: 指定使用 StandX 交易所
- `--symbol BTC-USD`: 交易对（注意格式）
- `--spread 0.3`: 价差百分比（0.3 表示 0.3%）
- `--market-type perp`: 市场类型（永续合约）

### 永续合约参数

- `--target-position 1.0`: 目标持仓量（绝对值）
- `--max-position 1.0`: 最大允许持仓量
- `--stop-loss -25`: 止损阈值（负数，以报价资产计）
- `--take-profit 50`: 止盈阈值（正数，以报价资产计）

### 运行参数

- `--duration 3600`: 运行时间（秒），3600 = 1小时
- `--interval 60`: 更新间隔（秒），60 = 1分钟

## 验证配置

启动前可以先用 CLI 模式查询余额，验证配置是否正确：

```bash
python run.py --cli
# 选择 2 - 查詢餘額
```

如果配置正确，应该能看到 StandX 的账户余额信息。

## 常见问题

### Q: 提示 "缺少 StandX JWT Token"
A: 需要先运行认证工具获取 JWT Token，然后添加到 `.env` 文件中。

### Q: 提示 "無法解析交易對"
A: 检查交易对格式是否正确，StandX 使用 `BTC-USD` 格式（不是 `BTC_USDC`）。

### Q: JWT Token 过期了怎么办？
A: 重新运行认证工具获取新 Token：
```bash
python utils/standx_auth.py your_private_key bsc
```

### Q: 如何查看 StandX 支持哪些交易对？
A: 可以通过 StandX 网站查看，或者尝试查询特定交易对的市场信息。

## 完整示例

### 示例 1: 简单的永续合约做市

```bash
# 1. 获取 JWT Token
python utils/standx_auth.py your_private_key bsc

# 2. 添加到 .env 文件
echo "STANDX_JWT_TOKEN=your_token_here" >> .env

# 3. 启动做市
python run.py \
  --exchange standx \
  --symbol BTC-USD \
  --spread 0.5 \
  --market-type perp \
  --target-position 0.5 \
  --max-position 1.0
```

### 示例 2: 带风险控制的做市

```bash
python run.py \
  --exchange standx \
  --symbol ETH-USD \
  --spread 0.3 \
  --market-type perp \
  --target-position 1.0 \
  --max-position 2.0 \
  --stop-loss -50 \
  --take-profit 100 \
  --duration 7200 \
  --interval 30
```

## 技术支持

如果遇到问题：
1. 检查 JWT Token 是否有效
2. 确认交易对格式正确
3. 查看日志文件 `market_maker.log`
4. 参考 StandX API 文档: https://docs.standx.com/standx-api/standx-api

