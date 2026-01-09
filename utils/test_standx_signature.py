"""
测试 StandX 签名格式
用于调试签名验证失败的问题
"""
from eth_account import Account
from eth_account.messages import encode_defunct
import json

def test_signature(private_key: str, message: str):
    """测试签名格式"""
    # 确保私钥格式正确
    if not private_key.startswith("0x"):
        private_key = "0x" + private_key
    
    # 创建账户
    account = Account.from_key(private_key)
    print(f"钱包地址: {account.address}")
    print()
    
    # 签名消息
    message_hash = encode_defunct(text=message)
    signed_message = account.sign_message(message_hash)
    
    # 获取签名
    signature_bytes = signed_message.signature
    
    # 提取组件
    r = signature_bytes[:32]
    s = signature_bytes[32:64]
    v = signature_bytes[64]
    
    print("签名信息:")
    print(f"  完整签名 (hex): 0x{signature_bytes.hex()}")
    print(f"  签名长度: {len(signature_bytes)} 字节")
    print(f"  r: 0x{r.hex()}")
    print(f"  s: 0x{s.hex()}")
    print(f"  v: {v} (0x{v:02x})")
    print()
    
    # 验证签名
    try:
        recovered = Account.recover_message(message_hash, signature=signature_bytes)
        print(f"恢复的地址: {recovered}")
        print(f"地址匹配: {recovered.lower() == account.address.lower()}")
    except Exception as e:
        print(f"签名验证失败: {e}")
    
    print()
    print("可能的解决方案:")
    print("1. 确认钱包地址已在 StandX 网站注册")
    print("2. 尝试在 StandX 网站手动连接钱包")
    print("3. 检查 StandX API 文档是否有特殊要求")
    print("4. 联系 StandX 技术支持")

if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 3:
        print("用法: python test_standx_signature.py <private_key> <message>")
        print("示例: python test_standx_signature.py 0x... 'standx.com wants you to sign in...'")
        sys.exit(1)
    
    private_key = sys.argv[1]
    message = sys.argv[2]
    
    test_signature(private_key, message)

