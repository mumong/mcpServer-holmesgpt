#!/usr/bin/env python3
"""
MCP Server 统一启动器

根据配置文件启动多个 MCP Server (第三方 npm 包 + 本地自定义工具)

用法:
    python start.py                           # 使用默认配置文件 mcp_config.yaml
    python start.py --config my_config.yaml   # 使用指定配置文件
    python start.py --list                    # 列出所有配置的服务

配置文件格式见 mcp_config.yaml
"""

import os
import sys
import subprocess
import argparse
import signal
import time
import threading
import shutil
from pathlib import Path
from typing import List, Dict, Any, Tuple

try:
    import yaml
except ImportError:
    print("❌ 缺少 pyyaml 依赖，请运行: pip install pyyaml")
    sys.exit(1)


class MCPServerManager:
    """MCP Server 管理器"""
    
    def __init__(self, config_path: str):
        self.config_path = Path(config_path)
        self.processes: List[Tuple[str, subprocess.Popen]] = []  # (name, process)
        self.config: Dict[str, Any] = {}
        self._stop_flag = False
    
    def _stream_output(self, name: str, process: subprocess.Popen):
        """读取并打印子进程的输出"""
        try:
            for line in iter(process.stdout.readline, ''):
                if line:
                    print(f"[{name}] {line.rstrip()}", flush=True)
                if process.poll() is not None:
                    break
        except:
            pass
        
    def load_config(self) -> bool:
        """加载配置文件"""
        if not self.config_path.exists():
            print(f"❌ 配置文件不存在: {self.config_path}")
            return False
        
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                self.config = yaml.safe_load(f) or {}
            return True
        except Exception as e:
            print(f"❌ 配置文件解析失败: {e}")
            return False
    
    def list_servers(self):
        """列出所有配置的服务"""
        if not self.load_config():
            return
        
        print("\n📋 MCP Server 配置列表\n")
        print("=" * 60)
        
        # 第三方包 (npm / uv)
        customer_servers = self.config.get('customermcp', []) or []
        if customer_servers:
            print("\n🌐 第三方 MCP 包:")
            for server in customer_servers:
                status = "✅" if server.get('enabled', True) else "⏸️"
                pkg_type = server.get('type', 'npm')
                print(f"  {status} {server.get('name', 'unnamed')} [{pkg_type}]")
                print(f"     包: {server.get('package')}")
                if pkg_type == 'uv':
                    print(f"     目录: {server.get('directory')}")
                print(f"     端口: {server.get('port')}")
        
        # 本地自定义
        basic_servers = self.config.get('basicmcp', []) or []
        if basic_servers:
            print("\n🏠 本地 MCP (自定义):")
            for server in basic_servers:
                status = "✅" if server.get('enabled', True) else "⏸️"
                print(f"  {status} {server.get('name', 'unnamed')}")
                print(f"     路径: {server.get('path')}")
                print(f"     端口: {server.get('port')}")
        
        print("\n" + "=" * 60)
        print("✅ = 已启用  ⏸️ = 已禁用")
    
    def start_package_server(self, server: Dict) -> Tuple[str, subprocess.Popen]:
        """启动第三方 MCP Server (支持 npm / uv)"""
        name = server.get('name', 'unnamed')
        package = server.get('package')
        port = server.get('port')
        pkg_type = server.get('type', 'npm')  # 默认 npm
        env_vars = server.get('env', {})
        
        if not package or not port:
            print(f"  ❌ {name}: 缺少 package 或 port 配置")
            return None
        
        # 准备环境变量（每进程独立 npm 缓存，避免多进程并发安装时缓存冲突）
        env = os.environ.copy()
        env["NODE_TLS_REJECT_UNAUTHORIZED"] = "0"
        env["npm_config_cache"] = f"/tmp/npm-cache-{port}"
        env["NPX_HOME"] = f"/tmp/npx-{port}"
        for key, value in env_vars.items():
            env[key] = str(value)
        
        # 优先使用镜像内预装的 mcp-proxy，避免 npx 并发安装
        mcp_proxy_cmd = "mcp-proxy" if shutil.which("mcp-proxy") else "npx -y mcp-proxy"
        
        # 根据类型构建内部命令
        if pkg_type == 'uv':
            # uv 类型: uv --directory <dir> run <package>
            directory = server.get('directory')
            if not directory:
                print(f"  ❌ {name}: uv 类型必须配置 directory")
                return None
            inner_cmd = f"uv --directory {directory} run {package}"
            type_icon = "🐍"
        else:
            # npm 类型: npx -y <package>
            inner_cmd = f"npx -y {package}"
            type_icon = "📦"
        
        # 使用 mcp-proxy (支持更好的连接管理和重连)
        cmd = f'{mcp_proxy_cmd} --port {port} --server sse -- {inner_cmd}'
        
        print(f"  🚀 {name}: 启动中... [{pkg_type}]")
        print(f"     {type_icon} 包: {package}")
        if pkg_type == 'uv':
            print(f"     📁 目录: {server.get('directory')}")
        print(f"     🔌 端口: {port}")
        print(f"     🌐 SSE: http://localhost:{port}/sse")
        
        try:
            process = subprocess.Popen(
                cmd,
                shell=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )
            # 启动输出读取线程
            threading.Thread(target=self._stream_output, args=(name, process), daemon=True).start()
            return (name, process)
        except Exception as e:
            print(f"  ❌ {name}: 启动失败 - {e}")
            return None
    
    def start_local_server(self, server: Dict) -> Tuple[str, subprocess.Popen]:
        """启动本地自定义 MCP Server"""
        name = server.get('name', 'unnamed')
        path = server.get('path')
        port = server.get('port')
        env_vars = server.get('env', {})
        
        if not path or not port:
            print(f"  ❌ {name}: 缺少 path 或 port 配置")
            return None
        
        # 检查文件是否存在
        script_path = Path(path)
        if not script_path.is_absolute():
            # 优先从工作目录查找，其次从配置文件目录查找
            work_dir_path = Path(__file__).parent / path
            config_dir_path = self.config_path.parent / path
            
            if work_dir_path.exists():
                script_path = work_dir_path
            elif config_dir_path.exists():
                script_path = config_dir_path
            else:
                print(f"  ❌ {name}: 脚本不存在 - {path}")
                print(f"     尝试路径: {work_dir_path}")
                print(f"     尝试路径: {config_dir_path}")
                return None
        
        # 准备环境变量（每进程独立 npm 缓存，避免多进程并发安装时缓存冲突）
        env = os.environ.copy()
        env["NODE_TLS_REJECT_UNAUTHORIZED"] = "0"
        env["npm_config_cache"] = f"/tmp/npm-cache-{port}"
        env["NPX_HOME"] = f"/tmp/npx-{port}"
        for key, value in env_vars.items():
            env[key] = str(value)
        
        # 优先使用镜像内预装的 mcp-proxy，避免 npx 并发安装
        mcp_proxy_cmd = "mcp-proxy" if shutil.which("mcp-proxy") else "npx -y mcp-proxy"
        # 构建命令 (使用当前 Python 解释器 sys.executable，兼容仅有 python3 的环境)
        cmd = f'{mcp_proxy_cmd} --port {port} --server sse -- {sys.executable} {script_path}'
        
        print(f"  🚀 {name}: 启动中...")
        print(f"     路径: {script_path}")
        print(f"     端口: {port}")
        print(f"     SSE: http://localhost:{port}/sse")
        
        try:
            process = subprocess.Popen(
                cmd,
                shell=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )
            # 启动输出读取线程
            threading.Thread(target=self._stream_output, args=(name, process), daemon=True).start()
            return (name, process)
        except Exception as e:
            print(f"  ❌ {name}: 启动失败 - {e}")
            return None
    
    def start_all(self):
        """启动所有已启用的服务"""
        if not self.load_config():
            return
        
        print("\n🔧 MCP Server 统一启动器")
        print("=" * 60)
        
        started_count = 0
        
        # 启动第三方包 (npm / uv)
        customer_servers = self.config.get('customermcp', []) or []
        if customer_servers:
            print("\n📦 启动第三方 MCP Server...")
            for server in customer_servers:
                if not server.get('enabled', True):
                    print(f"  ⏭️  {server.get('name', 'unnamed')}: 已跳过 (disabled)")
                    continue
                process = self.start_package_server(server)
                if process:
                    self.processes.append(process)
                    started_count += 1
        
        # 启动本地自定义
        basic_servers = self.config.get('basicmcp', []) or []
        if basic_servers:
            print("\n🏠 启动本地 MCP Server...")
            for server in basic_servers:
                if not server.get('enabled', True):
                    print(f"  ⏭️  {server.get('name', 'unnamed')}: 已跳过 (disabled)")
                    continue
                process = self.start_local_server(server)
                if process:
                    self.processes.append(process)
                    started_count += 1
        
        print("\n" + "=" * 60)
        
        if started_count == 0:
            print("⚠️  没有服务被启动，请检查配置文件")
            return
        
        print(f"✅ 已启动 {started_count} 个 MCP Server")
        print("\n📡 SSE 端点汇总:")
        
        # 打印所有端点
        for server in customer_servers + basic_servers:
            if server.get('enabled', True):
                port = server.get('port')
                name = server.get('name', 'unnamed')
                print(f"   - {name}: http://localhost:{port}/sse")
        
        print("\n按 Ctrl+C 停止所有服务...\n")
        
        # 等待并处理信号
        self._wait_for_exit()
    
    def _wait_for_exit(self):
        """等待退出信号"""
        def signal_handler(sig, frame):
            self._stop_flag = True
            print("\n\n🛑 正在停止所有服务...")
            self.stop_all()
            sys.exit(0)
        
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        
        # 给服务一点启动时间
        time.sleep(2)
        
        # 检查服务是否正常启动
        print("🔍 检查服务状态...")
        all_running = True
        for name, process in self.processes:
            if process.poll() is None:
                print(f"   ✅ {name}: 运行中")
            else:
                print(f"   ❌ {name}: 启动失败 (退出码: {process.returncode})")
                all_running = False
        
        if not all_running:
            print("\n⚠️  部分服务启动失败，请检查配置")
        
        print("\n" + "=" * 60)
        print("🟢 服务运行中，按 Ctrl+C 停止...\n")
        
        # 保持运行并监控进程
        while not self._stop_flag:
            time.sleep(5)
            # 检查是否有进程意外退出
            for name, process in self.processes:
                if process.poll() is not None:
                    # 只在进程确实退出时报告一次
                    exit_code = process.returncode
                    if exit_code != 0:
                        print(f"⚠️  {name}: 进程已退出 (退出码: {exit_code})")
                    # 从列表中移除已退出的进程避免重复报告
                    self.processes = [(n, p) for n, p in self.processes if p.poll() is None]
    
    def stop_all(self):
        """停止所有服务"""
        for name, process in self.processes:
            try:
                process.terminate()
                process.wait(timeout=5)
            except:
                process.kill()
        print("✅ 所有服务已停止")


def main():
    parser = argparse.ArgumentParser(
        description="MCP Server 统一启动器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python start.py                         # 启动所有配置的服务
  python start.py --config my.yaml        # 使用指定配置
  python start.py --list                  # 列出配置的服务
        """
    )
    parser.add_argument(
        "--config", "-c",
        default="mcp_config.yaml",
        help="配置文件路径 (默认: mcp_config.yaml)"
    )
    parser.add_argument(
        "--list", "-l",
        action="store_true",
        help="列出所有配置的服务"
    )
    
    args = parser.parse_args()
    
    # 确定配置文件路径
    config_path = args.config
    if not Path(config_path).is_absolute():
        config_path = Path(__file__).parent / config_path
    
    manager = MCPServerManager(str(config_path))
    
    if args.list:
        manager.list_servers()
    else:
        manager.start_all()


if __name__ == "__main__":
    main()
