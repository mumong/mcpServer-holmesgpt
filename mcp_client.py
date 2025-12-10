#!/usr/bin/env python3
"""
MCP SSE 客户端 - 用于连接和测试 MCP Server

用法:
    python mcp_client.py                              # 测试默认服务器
    python mcp_client.py http://localhost:9000/sse   # 测试指定服务器
    python mcp_client.py --call list_indices          # 调用工具
"""

import json
import time
import threading
import sys
from queue import Queue
import requests


class MCPClient:
    """简单的 MCP SSE 客户端"""
    
    def __init__(self, sse_url: str):
        self.sse_url = sse_url
        self.base_url = sse_url.rsplit("/", 1)[0]
        self.session_endpoint = None
        self.responses = {}
        self._running = False
        self._req_id = 0
    
    def connect(self, timeout: int = 10) -> bool:
        """连接服务器"""
        print(f"🔌 连接: {self.sse_url}")
        
        self._running = True
        thread = threading.Thread(target=self._listen_sse, daemon=True)
        thread.start()
        
        # 等待 session endpoint
        for _ in range(timeout * 10):
            if self.session_endpoint:
                break
            time.sleep(0.1)
        
        if not self.session_endpoint:
            print("❌ 连接超时")
            return False
        
        print(f"✅ Session: {self.session_endpoint}")
        
        # 初始化握手
        self._send("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "mcp-client", "version": "1.0"}
        }, wait=True)
        
        self._send("notifications/initialized", wait=False)
        time.sleep(0.3)
        return True
    
    def _listen_sse(self):
        """监听 SSE"""
        try:
            resp = requests.get(self.sse_url, stream=True, timeout=None)
            event_type = None
            for line in resp.iter_lines():
                if not self._running:
                    break
                if not line:
                    continue
                line = line.decode()
                if line.startswith("event:"):
                    event_type = line.split(":", 1)[1].strip()
                elif line.startswith("data:"):
                    data = line.split(":", 1)[1].strip()
                    if event_type == "endpoint":
                        self.session_endpoint = data
                    elif event_type == "message":
                        msg = json.loads(data)
                        if "id" in msg:
                            self.responses[msg["id"]] = msg
        except:
            pass
    
    def _send(self, method: str, params: dict = None, wait: bool = True, timeout: int = 10):
        """发送请求"""
        self._req_id += 1
        req_id = self._req_id
        
        payload = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params:
            payload["params"] = params
        
        url = f"{self.base_url}{self.session_endpoint}"
        requests.post(url, json=payload)
        
        if not wait:
            return None
        
        # 等待响应
        for _ in range(timeout * 10):
            if req_id in self.responses:
                return self.responses.pop(req_id)
            time.sleep(0.1)
        return None
    
    def list_tools(self) -> list:
        """获取工具列表"""
        resp = self._send("tools/list")
        if resp and "result" in resp:
            return resp["result"].get("tools", [])
        return []
    
    def call_tool(self, name: str, args: dict = None):
        """调用工具"""
        resp = self._send("tools/call", {"name": name, "arguments": args or {}})
        if resp and "result" in resp:
            content = resp["result"].get("content", [])
            if content and content[0].get("type") == "text":
                text = content[0].get("text", "")
                try:
                    return json.loads(text)
                except:
                    return text
            return content
        if resp and "error" in resp:
            return {"error": resp["error"].get("message")}
        return None
    
    def disconnect(self):
        """断开连接"""
        self._running = False


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="MCP 客户端")
    parser.add_argument("url", nargs="?", default="http://localhost:8082/sse")
    parser.add_argument("--call", "-c", help="调用工具")
    parser.add_argument("--args", "-a", help="工具参数 (JSON)")
    
    args = parser.parse_args()
    
    client = MCPClient(args.url)
    if not client.connect():
        sys.exit(1)
    
    # 获取工具列表
    tools = client.list_tools()
    print(f"\n📦 可用工具 ({len(tools)} 个):")
    for t in tools:
        print(f"  - {t['name']}: {t.get('description', '')[:60]}")
    
    # 调用工具
    if args.call:
        tool_args = json.loads(args.args) if args.args else {}
        print(f"\n🔧 调用: {args.call}")
        result = client.call_tool(args.call, tool_args)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    
    client.disconnect()


if __name__ == "__main__":
    main()

