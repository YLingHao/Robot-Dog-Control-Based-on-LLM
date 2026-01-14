#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""转发程序测试脚本

用于测试转发程序的功能
"""

import json
from llm_forwarder import LLMForwarder, JSONExtractor

def test_json_extraction():
    """测试JSON提取功能"""
    print("=" * 60)
    print("测试1: JSON提取功能")
    print("=" * 60)
    
    extractor = JSONExtractor()
    
    test_cases = [
        # 纯JSON
        ('{"actions":[{"code":"0x21010130","param":0.5}]}', True),
        # JSON代码块
        ('```json\n{"actions":[{"code":"0x21010130"}]}\n```', True),
        # Markdown代码块
        ('```\n{"actions":[{"code":"0x21010130"}]}\n```', True),
        # 文本中的JSON
        ('这是文本，然后有JSON：{"actions":[{"code":"0x21010130"}]}，后面还有文本', True),
        # 无效JSON
        ('这不是JSON', False),
        # 缺少actions
        ('{"other": "data"}', False),
    ]
    
    for i, (text, should_find) in enumerate(test_cases, 1):
        result = extractor.extract_json(text)
        found = result is not None and extractor.validate_command(result)
        status = "✓" if found == should_find else "✗"
        print(f"{status} 测试 {i}: {text[:50]}... -> {found} (期望: {should_find})")
        if found:
            print(f"   提取的JSON: {json.dumps(result, ensure_ascii=False)[:100]}")


def test_forwarder(dog_ip: str, dog_user: str = "root"):
    """测试转发功能（需要实际的机器狗连接）"""
    print("\n" + "=" * 60)
    print("测试2: 转发功能（需要机器狗连接）")
    print("=" * 60)
    
    forwarder = LLMForwarder(dog_ip, dog_user)
    
    # 测试JSON指令
    test_json = {
        "actions": [
            {"code": "0x21010130", "param": 0.3, "semantic": "move_x"}
        ]
    }
    
    print(f"测试指令: {json.dumps(test_json, ensure_ascii=False)}")
    
    # 启动服务
    if forwarder.dog_controller.start_server():
        # 转发指令
        success = forwarder.forward_from_text(json.dumps(test_json))
        if success:
            print("✓ 转发成功")
        else:
            print("✗ 转发失败")
        
        # 停止服务
        forwarder.dog_controller.stop_server()
    else:
        print("✗ 无法启动机器狗服务")


if __name__ == "__main__":
    import sys
    
    test_json_extraction()
    
    if len(sys.argv) > 1:
        dog_ip = sys.argv[1]
        dog_user = sys.argv[2] if len(sys.argv) > 2 else "root"
        test_forwarder(dog_ip, dog_user)
    else:
        print("\n提示: 要测试转发功能，请提供机器狗IP:")
        print("  python test_forwarder.py 192.168.1.100 [root]")
