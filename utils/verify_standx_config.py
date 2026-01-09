#!/usr/bin/env python3
"""
验证 StandX 配置是否正确
检查 ed25519 密钥对是否匹配
"""
import os
import base64
import sys
from pathlib import Path

# 加载 .env 文件（如果存在）
try:
    from dotenv import load_dotenv
    # 尝试从项目根目录加载 .env 文件
    project_root = Path(__file__).parent.parent
    env_file = project_root / '.env'
    if env_file.exists():
        load_dotenv(env_file)
        print(f"✓ 已加载 .env 文件: {env_file}")
    else:
        # 尝试从当前目录加载
        load_dotenv()
        if Path('.env').exists():
            print(f"✓ 已加载 .env 文件: {Path('.env').absolute()}")
        else:
            print("⚠ 未找到 .env 文件，将使用环境变量")
    print()
except ImportError:
    print("⚠ 未安装 python-dotenv，将只使用系统环境变量")
    print("  建议安装: pip install python-dotenv")
    print()

try:
    from nacl.signing import SigningKey
    HAS_NACL = True
except ImportError:
    HAS_NACL = False
    print("错误: 需要安装 pynacl")
    print("请运行: pip install pynacl")
    sys.exit(1)

try:
    import base58
    HAS_BASE58 = True
except ImportError:
    HAS_BASE58 = False
    print("警告: 未安装 base58，将使用 base64 作为备选")

def verify_config():
    """验证 StandX 配置"""
    print("=" * 80)
    print("StandX 配置验证")
    print("=" * 80)
    print()
    
    # 读取配置
    jwt_token = os.getenv("STANDX_JWT_TOKEN", "")
    ed25519_private_key_hex = os.getenv("STANDX_ED25519_PRIVATE_KEY_BYTES", "")
    ed25519_request_id = os.getenv("STANDX_ED25519_REQUEST_ID", "")
    base_url = os.getenv("STANDX_BASE_URL", "https://perps.standx.com")
    
    # 检查必需配置
    print("1. 检查必需配置:")
    print("-" * 80)
    
    if jwt_token:
        print(f"✓ STANDX_JWT_TOKEN: 已配置 (长度: {len(jwt_token)} 字符)")
        print(f"  预览: {jwt_token[:30]}...{jwt_token[-30:]}")
    else:
        print("✗ STANDX_JWT_TOKEN: 未配置（必需）")
    
    if ed25519_private_key_hex:
        print(f"✓ STANDX_ED25519_PRIVATE_KEY_BYTES: 已配置 (长度: {len(ed25519_private_key_hex)} 字符)")
        print(f"  预览: {ed25519_private_key_hex[:20]}...{ed25519_private_key_hex[-20:]}")
    else:
        print("✗ STANDX_ED25519_PRIVATE_KEY_BYTES: 未配置（必需）")
    
    if ed25519_request_id:
        print(f"✓ STANDX_ED25519_REQUEST_ID: 已配置 (长度: {len(ed25519_request_id)} 字符)")
        print(f"  值: {ed25519_request_id}")
    else:
        print("✗ STANDX_ED25519_REQUEST_ID: 未配置（必需）")
    
    if base_url:
        print(f"✓ STANDX_BASE_URL: {base_url}")
    else:
        print("✗ STANDX_BASE_URL: 未配置（将使用默认值）")
    
    print()
    
    # StandX 不使用 API key/secret key，跳过可选配置检查
    print("2. 其他配置:")
    print("-" * 80)
    print("  StandX 使用 JWT token 认证，不需要传统的 API key/secret key")
    print()
    
    # 验证 ed25519 密钥对
    if ed25519_private_key_hex and ed25519_request_id:
        print("3. 验证 ed25519 密钥对:")
        print("-" * 80)
        
        try:
            # 从十六进制恢复私钥
            private_key_bytes = bytes.fromhex(ed25519_private_key_hex)
            
            if len(private_key_bytes) != 32:
                print(f"✗ 错误: ed25519 私钥长度不正确: {len(private_key_bytes)} 字节（期望 32 字节）")
            else:
                print(f"✓ 私钥长度正确: {len(private_key_bytes)} 字节")
                
                # 创建 SigningKey
                signing_key = SigningKey(private_key_bytes)
                public_key = signing_key.verify_key
                public_key_bytes = bytes(public_key)
                
                # 计算 request_id
                if HAS_BASE58:
                    calculated_request_id = base58.b58encode(public_key_bytes).decode('utf-8')
                else:
                    calculated_request_id = base64.b64encode(public_key_bytes).decode('utf-8')
                    print("  警告: 使用 base64 编码（建议安装 base58）")
                
                print(f"  计算的 request_id: {calculated_request_id}")
                print(f"  配置的 request_id: {ed25519_request_id}")
                
                if calculated_request_id == ed25519_request_id:
                    print("✓ 密钥对匹配: request_id 验证通过")
                else:
                    print("✗ 密钥对不匹配: request_id 验证失败")
                    print("  可能的原因:")
                    print("  1. ed25519_private_key_bytes 与 ed25519_request_id 不是同一密钥对")
                    print("  2. 认证时生成的密钥对与配置中的不一致")
                    print("  3. request_id 编码格式不正确（应该是 base58）")
        except Exception as e:
            print(f"✗ 验证失败: {e}")
            import traceback
            traceback.print_exc()
    else:
        print("3. 验证 ed25519 密钥对:")
        print("-" * 80)
        print("⚠ 跳过验证: 缺少必需配置")
    
    print()
    print("=" * 80)
    print("验证完成")
    print("=" * 80)
    print()
    
    # 检查 .env 文件
    project_root = Path(__file__).parent.parent
    env_file = project_root / '.env'
    if env_file.exists():
        print(f"📄 .env 文件位置: {env_file}")
        print()
        print("如果配置未加载，请检查:")
        print("1. .env 文件格式是否正确（每行一个 KEY=VALUE）")
        print("2. 变量名是否正确（区分大小写）")
        print("3. 值是否包含引号（不需要引号，直接写值）")
        print()
        print("示例 .env 文件内容:")
        print("-" * 80)
        print("STANDX_JWT_TOKEN=eyJhbGciOiJFUzI1NiIs...")
        print("STANDX_ED25519_PRIVATE_KEY_BYTES=d6e1fb5e4d02873cd5e1ed31d1ec59dddbefe48848acc77b6a31877732b8d0ef")
        print("STANDX_ED25519_REQUEST_ID=5rbEtih5mMr7uXFZkwXEnhFovocPCoSTydyY1RNJcYTc")
        print("STANDX_BASE_URL=https://perps.standx.com")
        print("-" * 80)
    else:
        print("⚠ 未找到 .env 文件")
        print()
        print("建议:")
        print("1. 在项目根目录创建 .env 文件")
        print(f"   位置: {env_file}")
        print("2. 添加 StandX 配置（参考上面的示例）")
        print("3. 确保 .env 文件在 .gitignore 中（不要提交到代码仓库）")
        print()
    
    print("配置建议:")
    print("1. 确保 STANDX_JWT_TOKEN 有效（未过期，默认有效期 7 天）")
    print("2. 确保 ed25519 密钥对是认证时生成的同一密钥对")
    print("3. StandX 使用 JWT token 认证，不需要 API key/secret key")
    print("4. 如果遇到 body signature 错误，检查 ed25519 密钥对是否匹配")
    print()
    print("获取配置的方法:")
    print("1. 运行认证工具: python utils/standx_auth.py <private_key>")
    print("2. 认证工具会输出所有需要的配置")
    print("3. 将输出复制到 .env 文件中")
    print()

if __name__ == "__main__":
    verify_config()

