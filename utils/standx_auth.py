"""
StandX 钱包签名认证工具
根据 StandX 官方文档实现完整的认证流程

官方文档: https://docs.standx.com/standx-api/standx-api#perps-auth

使用方法:
1. 安装依赖: pip install eth-account web3 PyJWT cryptography base58 pynacl
2. 运行: python utils/standx_auth.py
3. 或者设置环境变量: METAMASK_PRIVATE_KEY=your_private_key python utils/standx_auth.py
"""
import os
import json
import time
import base64
import requests
from typing import Optional, Dict, Any, Union

try:
    from eth_account import Account
    from eth_account.messages import encode_defunct
    HAS_ETH_ACCOUNT = True
except ImportError:
    HAS_ETH_ACCOUNT = False
    print("警告: 未安装 eth-account，请运行: pip install eth-account web3")

try:
    import jwt
    HAS_JWT = True
except ImportError:
    HAS_JWT = False
    print("警告: 未安装 PyJWT，请运行: pip install PyJWT cryptography")

try:
    import base58
    HAS_BASE58 = True
except ImportError:
    HAS_BASE58 = False
    print("警告: 未安装 base58，请运行: pip install base58")

try:
    from nacl.signing import SigningKey
    from nacl.encoding import Base64Encoder
    HAS_NACL = True
except ImportError:
    HAS_NACL = False
    print("警告: 未安装 pynacl，请运行: pip install pynacl")


class StandXAuth:
    """StandX 认证类，实现完整的认证流程"""
    
    def __init__(self, base_url: str = "https://api.standx.com"):
        self.base_url = base_url
        self.ed25519_private_key = None
        self.ed25519_public_key = None
        self.request_id = None
        
        if not HAS_NACL:
            raise ImportError("需要安装 pynacl: pip install pynacl")
        
        # 生成临时的 ed25519 密钥对
        self._generate_ed25519_keypair()
    
    def _generate_ed25519_keypair(self):
        """生成临时的 ed25519 密钥对"""
        if not HAS_NACL:
            raise ImportError("需要安装 pynacl")
        
        # 生成 ed25519 密钥对
        signing_key = SigningKey.generate()
        self.ed25519_private_key = signing_key
        self.ed25519_public_key = signing_key.verify_key
        
        # 将公钥编码为 base58 作为 requestId
        if HAS_BASE58:
            public_key_bytes = bytes(self.ed25519_public_key)
            self.request_id = base58.b58encode(public_key_bytes).decode('utf-8')
        else:
            # 如果没有 base58，使用 base64 作为备选
            public_key_bytes = bytes(self.ed25519_public_key)
            self.request_id = base64.b64encode(public_key_bytes).decode('utf-8')
            print("警告: 使用 base64 编码 requestId（建议安装 base58）")
    
    def prepare_signin(self, chain: str, wallet_address: str) -> Optional[str]:
        """
        步骤 2: 获取签名数据
        
        Args:
            chain: 区块链网络 ('bsc' 或 'solana')
            wallet_address: 钱包地址
            
        Returns:
            signedData (JWT 字符串) 或 None
        """
        url = f"{self.base_url}/v1/offchain/prepare-signin?chain={chain}"
        
        payload = {
            "address": wallet_address,
            "requestId": self.request_id
        }
        
        headers = {
            "Content-Type": "application/json"
        }
        
        try:
            print(f"正在请求签名数据 (chain={chain}, address={wallet_address})...")
            response = requests.post(url, json=payload, headers=headers, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                if data.get("success"):
                    signed_data = data.get("signedData")
                    print("✓ 成功获取签名数据")
                    return signed_data
                else:
                    print(f"请求失败: {data}")
            else:
                print(f"HTTP 错误 {response.status_code}: {response.text}")
                
        except Exception as e:
            print(f"请求签名数据失败: {e}")
            import traceback
            traceback.print_exc()
        
        return None
    
    def get_verification_public_key(self) -> Optional[str]:
        """
        获取 StandX 的 JWT 验证公钥（可选步骤）
        
        根据文档，可以使用此公钥验证 signedData JWT 的签名
        
        Returns:
            StandX 的公钥（PEM 格式）或 None
        """
        url = f"{self.base_url}/v1/offchain/certs"
        
        try:
            response = requests.get(url, timeout=10)
            if response.status_code == 200:
                # 返回的公钥可能是 PEM 格式或其他格式
                public_key = response.text.strip()
                print(f"✓ 成功获取 StandX 验证公钥")
                return public_key
            else:
                print(f"获取验证公钥失败: HTTP {response.status_code}")
                return None
        except Exception as e:
            print(f"获取验证公钥失败: {e}")
            return None
    
    def parse_jwt(self, token: str, verify: bool = False) -> Optional[Dict[str, Any]]:
        """
        步骤 3: 解析和验证 JWT token
        
        根据文档，signedData 是一个 JWT 字符串，可以使用 StandX 的公钥进行验证。
        但验证不是必须的，因为 StandX 会在登录时验证。
        
        Args:
            token: JWT token 字符串
            verify: 是否验证签名（默认 False，因为需要获取 StandX 公钥）
            
        Returns:
            解析后的 payload 字典或 None
        """
        if not HAS_JWT:
            raise ImportError("需要安装 PyJWT: pip install PyJWT cryptography")
        
        try:
            if verify:
                # 尝试获取 StandX 公钥并验证
                public_key = self.get_verification_public_key()
                if public_key:
                    try:
                        # 使用公钥验证 JWT 签名
                        payload = jwt.decode(token, public_key, algorithms=["ES256", "RS256"])
                        print("✓ JWT 签名验证通过")
                        return payload
                    except jwt.InvalidSignatureError:
                        print("警告: JWT 签名验证失败（但继续解析 payload）")
                    except Exception as e:
                        print(f"警告: JWT 验证过程出错: {e}")
                
                # 如果验证失败，回退到只解析
                print("回退到仅解析模式（不验证签名）")
            
            # 不验证签名，只解析 payload（默认行为）
            payload = jwt.decode(token, options={"verify_signature": False})
            
            # 显示解析的 payload 信息（用于调试）
            if payload.get("message"):
                print(f"✓ 成功解析 JWT payload")
                print(f"  域名: {payload.get('domain', 'N/A')}")
                print(f"  地址: {payload.get('address', 'N/A')}")
                print(f"  请求ID: {payload.get('requestId', 'N/A')}")
                print(f"  消息长度: {len(payload.get('message', ''))} 字符")
            
            return payload
        except Exception as e:
            print(f"解析 JWT 失败: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def sign_message_with_wallet(self, message: str, private_key: str) -> Optional[str]:
        """
        步骤 4: 使用钱包私钥签名消息
        
        Args:
            message: 要签名的消息
            private_key: 钱包私钥
            
        Returns:
            签名字符串（0x 开头）或 None
        """
        if not HAS_ETH_ACCOUNT:
            raise ImportError("需要安装 eth-account: pip install eth-account web3")
        
        try:
            # 确保私钥格式正确
            if not private_key.startswith("0x"):
                private_key = "0x" + private_key
            
            # 创建账户对象
            account = Account.from_key(private_key)
            
            # 使用 eth_account 进行签名（EIP-191 格式）
            # encode_defunct 会自动添加 "\x19Ethereum Signed Message:\n{len(message)}" 前缀
            message_hash = encode_defunct(text=message)
            signed_message = account.sign_message(message_hash)
            
            # 获取签名（65字节：r + s + v）
            signature_bytes = signed_message.signature
            
            # 调试：检查签名格式
            if len(signature_bytes) != 65:
                print(f"警告: 签名长度异常: {len(signature_bytes)} 字节（期望 65 字节）")
            
            # 提取 r, s, v
            r = signature_bytes[:32]
            s = signature_bytes[32:64]
            v = signature_bytes[64]
            
            # 调试信息
            print(f"签名组件:")
            print(f"  r: 0x{r.hex()[:20]}...")
            print(f"  s: 0x{s.hex()[:20]}...")
            print(f"  v: {v} (0x{v:02x})")
            
            # 转换为十六进制字符串（0x 开头）
            signature = "0x" + signature_bytes.hex()
            
            # 验证签名长度（应该是 132 个字符：0x + 130 个十六进制字符）
            if len(signature) != 132:
                print(f"警告: 签名字符串长度异常: {len(signature)} 字符（期望 132）")
            
            # 验证签名：尝试恢复地址
            try:
                recovered_address = Account.recover_message(message_hash, signature=signed_message.signature)
                if recovered_address.lower() != account.address.lower():
                    print(f"警告: 签名验证失败 - 恢复的地址不匹配")
                    print(f"  期望地址: {account.address}")
                    print(f"  恢复地址: {recovered_address}")
                else:
                    print(f"✓ 签名验证通过 - 地址匹配: {account.address}")
            except Exception as e:
                print(f"警告: 无法验证签名: {e}")
            
            print("✓ 成功签名消息")
            return signature
            
        except Exception as e:
            print(f"签名消息失败: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def login(self, chain: str, signature: str, signed_data: str, expires_seconds: int = 604800) -> Optional[Dict[str, Any]]:
        """
        步骤 5: 获取访问令牌
        
        Args:
            chain: 区块链网络 ('bsc' 或 'solana')
            signature: 钱包签名
            signed_data: 从 prepare_signin 获取的 JWT
            expires_seconds: Token 过期时间（秒），默认 7 天
            
        Returns:
            登录响应字典，包含 token 等信息
        """
        url = f"{self.base_url}/v1/offchain/login?chain={chain}"
        
        payload = {
            "signature": signature,
            "signedData": signed_data,
            "expiresSeconds": expires_seconds
        }
        
        headers = {
            "Content-Type": "application/json"
        }
        
        try:
            print("正在登录获取访问令牌...")
            response = requests.post(url, json=payload, headers=headers, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                if "token" in data:
                    print("✓ 成功获取访问令牌")
                    return data
                else:
                    print(f"登录响应格式异常: {data}")
            else:
                error_text = response.text
                print(f"HTTP 错误 {response.status_code}: {error_text}")
                
                # 尝试解析错误信息
                try:
                    error_data = response.json()
                    error_msg = error_data.get("message", error_text)
                    print(f"错误详情: {error_msg}")
                    
                    # 如果是签名验证失败，提供更多调试信息
                    if "signature" in error_msg.lower() or "verification" in error_msg.lower():
                        print()
                        print("签名验证失败的可能原因:")
                        print("1. 签名格式不正确")
                        print("2. 消息格式不匹配")
                        print("3. 钱包地址与私钥不匹配")
                        print("4. StandX 服务器期望的签名格式不同")
                        print()
                        print("调试建议:")
                        print("- 确认私钥与钱包地址匹配")
                        print("- 尝试在 StandX 网站手动连接钱包")
                        print("- 检查消息内容是否完整")
                except:
                    pass
                
        except Exception as e:
            print(f"登录失败: {e}")
            import traceback
            traceback.print_exc()
        
        return None
    
    def authenticate(self, chain: str, wallet_address: str, private_key: str, expires_seconds: int = 604800) -> Optional[Dict[str, Any]]:
        """
        完整的认证流程
        
        Args:
            chain: 区块链网络 ('bsc' 或 'solana')
            wallet_address: 钱包地址
            private_key: 钱包私钥
            expires_seconds: Token 过期时间（秒），默认 7 天
            
        Returns:
            登录响应字典，包含 token 等信息
        """
        print("=" * 60)
        print("StandX 认证流程")
        print("=" * 60)
        print()
        
        # 步骤 2: 获取签名数据
        signed_data = self.prepare_signin(chain, wallet_address)
        if not signed_data:
            print("获取签名数据失败")
            return None
        
        # 步骤 3: 解析 JWT 获取 message
        payload = self.parse_jwt(signed_data)
        if not payload:
            print("解析 JWT 失败")
            return None
        
        message = payload.get("message")
        if not message:
            print("JWT payload 中未找到 message 字段")
            print(f"JWT payload 内容: {json.dumps(payload, indent=2)}")
            return None
        
        print(f"需要签名的消息长度: {len(message)} 字符")
        print(f"消息预览: {message[:150]}...")
        print()
        
        # 步骤 4: 使用钱包私钥签名消息
        signature = self.sign_message_with_wallet(message, private_key)
        if not signature:
            print("签名消息失败")
            return None
        
        print(f"生成的签名: {signature[:50]}...{signature[-20:]}")
        print()
        
        # 步骤 5: 获取访问令牌
        login_response = self.login(chain, signature, signed_data, expires_seconds)
        
        # 如果认证成功，返回 ed25519 密钥对信息（用于 body signature）
        # 重要：StandX 的 body signature 必须使用认证时生成的 ed25519 密钥对
        if login_response and "token" in login_response:
            # 保存 ed25519 密钥对信息（用于后续的 body signature）
            # 注意：私钥需要序列化为字节以便后续使用
            try:
                ed25519_private_key_bytes = bytes(self.ed25519_private_key)
                ed25519_public_key_bytes = bytes(self.ed25519_public_key)
                
                login_response["ed25519_private_key_bytes"] = ed25519_private_key_bytes.hex()
                login_response["ed25519_public_key_bytes"] = ed25519_public_key_bytes.hex()
                login_response["ed25519_request_id"] = self.request_id
                
                print()
                print("=" * 60)
                print("重要：ed25519 密钥对信息（用于 body signature）")
                print("=" * 60)
                print(f"Request ID (base58): {self.request_id}")
                print()
                print("⚠️  注意：StandX 的 body signature 必须使用认证时生成的 ed25519 密钥对")
                print("   当前认证工具已保存密钥对信息，但 StandXClient 需要配置使用这个密钥对")
                print()
                print("解决方案：")
                print("1. 将 ed25519_private_key_bytes 添加到 StandXClient 配置中")
                print("2. 或者修改 StandXClient 使其能够从认证响应中获取密钥对")
                print()
            except Exception as e:
                print(f"警告：无法序列化 ed25519 密钥对: {e}")
        
        return login_response


def get_standx_jwt_token(private_key: str, chain: str = "bsc", base_url: str = "https://api.standx.com") -> Optional[Union[str, Dict[str, Any]]]:
    """
    通过钱包私钥获取 StandX JWT Token（便捷函数）
    
    Args:
        private_key: MetaMask 私钥（0x 开头或没有前缀都可以）
        chain: 区块链网络 ('bsc' 或 'solana')，默认 'bsc'
        base_url: StandX API 基础 URL
        
    Returns:
        如果成功，返回包含以下字段的字典：
        {
            "token": "JWT Token 字符串",
            "ed25519_private_key_bytes": "ed25519 私钥（十六进制）",
            "ed25519_public_key_bytes": "ed25519 公钥（十六进制）",
            "ed25519_request_id": "base58 编码的公钥（requestId）",
            ... 其他登录响应字段
        }
        如果失败返回 None
    """
    if not HAS_ETH_ACCOUNT:
        print("错误: 需要安装 eth-account 库")
        print("请运行: pip install eth-account web3")
        return None
    
    try:
        # 确保私钥格式正确
        if not private_key.startswith("0x"):
            private_key = "0x" + private_key
        
        # 创建账户对象获取钱包地址
        account = Account.from_key(private_key)
        wallet_address = account.address
        
        # 创建认证对象并执行认证
        auth = StandXAuth(base_url=base_url)
        login_response = auth.authenticate(chain, wallet_address, private_key)
        
        if login_response and "token" in login_response:
            # 返回完整的登录响应（包含 ed25519 密钥对信息）
            return login_response
        
        return None
        
    except Exception as e:
        print(f"获取 JWT Token 失败: {e}")
        import traceback
        traceback.print_exc()
        return None


def main():
    """命令行工具入口"""
    import sys
    
    print("=" * 60)
    print("StandX 钱包认证工具")
    print("根据 StandX 官方文档实现")
    print("=" * 60)
    print()
    
    # 检查依赖
    missing_deps = []
    if not HAS_ETH_ACCOUNT:
        missing_deps.append("eth-account")
    if not HAS_JWT:
        missing_deps.append("PyJWT")
    if not HAS_BASE58:
        missing_deps.append("base58")
    if not HAS_NACL:
        missing_deps.append("pynacl")
    
    if missing_deps:
        print("缺少以下依赖:")
        for dep in missing_deps:
            print(f"  - {dep}")
        print()
        print("请运行以下命令安装:")
        print(f"pip install {' '.join(missing_deps)}")
        return
    
    # 从环境变量或命令行参数获取私钥
    private_key = os.getenv("METAMASK_PRIVATE_KEY") or os.getenv("PRIVATE_KEY")
    chain = os.getenv("STANDX_CHAIN", "bsc")  # 默认使用 BSC
    
    if len(sys.argv) > 1:
        private_key = sys.argv[1]
    if len(sys.argv) > 2:
        chain = sys.argv[2]
    
    if not private_key:
        print("请输入 MetaMask 私钥:")
        print("方式1: 设置环境变量 METAMASK_PRIVATE_KEY")
        print("方式2: 作为命令行参数传入: python utils/standx_auth.py YOUR_PRIVATE_KEY [chain]")
        print("方式3: 直接输入（不推荐，不安全）")
        private_key = input("私钥: ").strip()
    
    if not private_key:
        print("错误: 未提供私钥")
        return
    
    # 选择链
    if chain not in ["bsc", "solana"]:
        print(f"警告: 不支持的链 '{chain}'，使用默认值 'bsc'")
        chain = "bsc"
    
    print(f"使用链: {chain}")
    print()
    
    # 移除可能的空格和换行
    private_key = private_key.replace(" ", "").replace("\n", "")
    
    print("正在获取 StandX JWT Token...")
    print()
    
    result = get_standx_jwt_token(private_key, chain=chain)
    
    if result and isinstance(result, dict) and result.get("token"):
        jwt_token = result["token"]
        ed25519_private_key_hex = result.get("ed25519_private_key_bytes", "")
        ed25519_request_id = result.get("ed25519_request_id", "")
        
        print()
        print("=" * 60)
        print("认证成功！")
        print("=" * 60)
        print()
        print("请将以下内容添加到您的 .env 文件或环境变量中:")
        print()
        print(f"STANDX_JWT_TOKEN={jwt_token}")
        print()
        
        if ed25519_private_key_hex:
            print("⚠️  重要：StandX 的 body signature 需要使用认证时生成的 ed25519 密钥对")
            print()
            print("请将以下内容也添加到配置中（用于 body signature）:")
            print()
            print(f"STANDX_ED25519_PRIVATE_KEY_BYTES={ed25519_private_key_hex}")
            if ed25519_request_id:
                print(f"STANDX_ED25519_REQUEST_ID={ed25519_request_id}")
            print()
            print("注意：ed25519 私钥是敏感信息，请妥善保管")
            print()
        
        print("其他注意事项：")
        print("- JWT Token 默认有效期为 7 天")
        print("- Token 过期后需要重新运行此工具获取新 Token")
        print("- 请妥善保管 Token 和 ed25519 私钥，不要泄露给他人")
        print()
    elif result and isinstance(result, str):
        # 向后兼容：如果返回的是字符串（旧版本）
        jwt_token = result
        print()
        print("=" * 60)
        print("认证成功！")
        print("=" * 60)
        print()
        print("请将以下内容添加到您的 .env 文件或环境变量中:")
        print()
        print(f"STANDX_JWT_TOKEN={jwt_token}")
        print()
        print("注意：")
        print("- JWT Token 默认有效期为 7 天")
        print("- Token 过期后需要重新运行此工具获取新 Token")
        print("- ⚠️  警告：未获取到 ed25519 密钥对信息，body signature 可能无法正常工作")
        print("- 请妥善保管 Token，不要泄露给他人")
        print()
    else:
        print()
        print("=" * 60)
        print("认证失败")
        print("=" * 60)
        print()
        print("可能的原因:")
        print("1. 私钥不正确")
        print("2. 网络连接问题")
        print("3. StandX API 服务异常")
        print("4. 钱包地址未在 StandX 注册")
        print()
        print("建议:")
        print("1. 检查私钥是否正确")
        print("2. 访问 StandX 网站确认钱包已连接: https://perps.standx.com")
        print("3. 查看 StandX API 文档: https://docs.standx.com/standx-api/standx-api")
        print()


if __name__ == "__main__":
    main()
