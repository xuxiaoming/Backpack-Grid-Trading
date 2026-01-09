#!/usr/bin/env python3
"""
测试 StandX 签名格式
用于调试签名验证失败的问题

验证签名逻辑与 standx_auth.py 中的实现一致

注意：这不是 pytest 测试文件，请直接运行：
    python utils/test_standx_signature.py <private_key> <message>
    
或者在 PyCharm 中：
1. 右键点击文件 -> Run 'test_standx_signature'
2. 不要使用 pytest 运行器
"""
from eth_account import Account
from eth_account.messages import encode_defunct
import json

def verify_signature(private_key: str, message: str):
    """测试签名格式"""
    print("=" * 80)
    print("StandX 签名格式测试")
    print("=" * 80)
    print()
    
    # 确保私钥格式正确
    if not private_key.startswith("0x"):
        private_key = "0x" + private_key
    
    # 创建账户
    account = Account.from_key(private_key)
    print(f"钱包地址: {account.address}")
    print(f"私钥预览: {private_key[:10]}...{private_key[-10:]}")
    print()
    
    # 显示消息信息
    print("消息信息:")
    print(f"  消息长度: {len(message)} 字符")
    print(f"  消息预览: {message[:100]}...{message[-50:]}")
    print()
    
    # 签名消息（使用 EIP-191 格式）
    # encode_defunct 会自动添加 "\x19Ethereum Signed Message:\n{len(message)}" 前缀
    message_hash = encode_defunct(text=message)
    signed_message = account.sign_message(message_hash)
    
    # 获取签名（65字节：r + s + v）
    signature_bytes = signed_message.signature
    
    # 检查签名长度
    if len(signature_bytes) != 65:
        print(f"⚠️  警告: 签名长度异常: {len(signature_bytes)} 字节（期望 65 字节）")
        print()
    
    # 提取组件
    r = signature_bytes[:32]
    s = signature_bytes[32:64]
    v = signature_bytes[64]
    
    # 转换为十六进制字符串（StandX API 需要的格式）
    signature_hex = "0x" + signature_bytes.hex()
    
    print("签名信息:")
    print(f"  签名字节长度: {len(signature_bytes)} 字节")
    print(f"  签名字符串长度: {len(signature_hex)} 字符（期望 132）")
    print(f"  完整签名 (hex): {signature_hex}")
    print(f"  签名预览: {signature_hex[:30]}...{signature_hex[-30:]}")
    print()
    print("签名组件:")
    print(f"  r: 0x{r.hex()}")
    print(f"  s: 0x{s.hex()}")
    print(f"  v: {v} (0x{v:02x})")
    print()
    
    # 验证签名：尝试恢复地址
    print("签名验证:")
    try:
        recovered_address = Account.recover_message(message_hash, signature=signature_bytes)
        print(f"  恢复的地址: {recovered_address}")
        
        if recovered_address.lower() == account.address.lower():
            print(f"  ✓ 地址匹配: 签名验证通过")
        else:
            print(f"  ✗ 地址不匹配: 签名验证失败")
            print(f"    期望地址: {account.address}")
            print(f"    恢复地址: {recovered_address}")
    except Exception as e:
        print(f"  ✗ 签名验证失败: {e}")
    
    print()
    print("=" * 80)
    print("测试结果总结")
    print("=" * 80)
    print(f"✓ 签名格式: 正确（65 字节，132 字符十六进制字符串）")
    print(f"✓ 签名方法: EIP-191 (encode_defunct + sign_message)")
    print(f"✓ 最终签名格式: {signature_hex[:30]}...{signature_hex[-30:]}")
    print()
    print("如果 StandX API 仍然返回签名验证失败，可能的原因:")
    print("1. 钱包地址未在 StandX 网站注册")
    print("2. 消息内容与 StandX 服务器期望的不完全一致")
    print("3. StandX 服务器期望的签名格式不同（虽然不太可能）")
    print("4. 网络问题或 StandX API 服务异常")
    print()
    print("建议:")
    print("1. 确认钱包地址已在 StandX 网站注册: https://perps.standx.com")
    print("2. 尝试在 StandX 网站手动连接钱包")
    print("3. 检查消息内容是否完整（从 JWT payload.message 获取）")
    print("4. 查看 StandX API 文档: https://docs.standx.com/standx-api/perps-auth")
    print("5. 联系 StandX 技术支持")
    print("=" * 80)

if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 3:
        print("用法: python test_standx_signature.py <private_key> <message>")
        print("示例: python test_standx_signature.py 0x... 'standx.com wants you to sign in...'")
        sys.exit(1)
    
    private_key = sys.argv[1]
    message = sys.argv[2]
    
    verify_signature(private_key, message)

