#!/usr/bin/env python3
"""
MCP Server 统一启动器

根据配置文件启动多个 MCP Server (第三方 npm 包 + 本地自定义工具)

用法:
    python start.py                                   # 使用默认配置 config/mcp_config.yaml
    python start.py --config config/my_config.yaml    # 使用指定配置文件
    python start.py --list                            # 列出所有配置的服务

配置文件格式见 config/mcp_config.yaml 或 config/mcp_config.example.yaml
"""

import argparse
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

from launch_config import build_proxy_command, build_server_environment

try:
    import yaml
except ImportError:
    sys.exit("缺少 pyyaml 依赖，请运行: pip install pyyaml")

logger = logging.getLogger("mcp-launcher")

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
# 子进程崩溃后的重启退避：首次 1s，逐次翻倍，上限 60s；稳定运行 60s 后重置。
RESTART_BACKOFF_INITIAL = 1.0
RESTART_BACKOFF_MAX = 60.0
STABLE_RUN_SECONDS = 60.0
MONITOR_INTERVAL_SECONDS = 1.0


class MCPServerManager:
    """MCP Server 管理器"""
    
    def __init__(self, config_path: str):
        self.config_path = Path(config_path)
        self.processes: List[Tuple[str, subprocess.Popen]] = []  # (name, process)
        self.config: Dict[str, Any] = {}
        self._stop_flag = False
        # name -> 重启所需的启动函数/配置及退避状态
        self._restart_specs: Dict[str, Tuple[Callable[[Dict], Any], Dict]] = {}
        self._backoff: Dict[str, float] = {}
        self._started_at: Dict[str, float] = {}
        self._pending_restart: Dict[str, float] = {}  # name -> 计划重启的 monotonic 时间
    
    def _stream_output(self, name: str, process: subprocess.Popen):
        """读取并记录子进程的输出"""
        try:
            for line in iter(process.stdout.readline, ''):
                if line:
                    logger.info("[%s] %s", name, line.rstrip())
                if process.poll() is not None:
                    break
        except (OSError, ValueError):
            # 管道在进程退出/终止时关闭
            pass
        
    def load_config(self) -> bool:
        """加载配置文件"""
        if not self.config_path.exists():
            logger.error("配置文件不存在: %s", self.config_path)
            return False
        
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                self.config = yaml.safe_load(f) or {}
            return True
        except (OSError, yaml.YAMLError) as e:
            logger.error("配置文件解析失败: %s", e)
            return False
    
    def list_servers(self):
        """列出所有配置的服务（CLI 报告输出到 stdout）"""
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
            logger.error("%s: 缺少 package 或 port 配置", name)
            return None
        
        # 准备环境变量（每进程独立 npm 缓存，避免多进程并发安装时缓存冲突）
        env = build_server_environment(os.environ, port, env_vars)
        
        # 优先使用镜像内预装的 mcp-proxy，避免 npx 并发安装
        proxy_available = bool(shutil.which("mcp-proxy"))
        
        # 根据类型构建内部命令
        if pkg_type == 'uv':
            # uv 类型: uv --directory <dir> run <package>
            directory = server.get('directory')
            if not directory:
                logger.error("%s: uv 类型必须配置 directory", name)
                return None
            inner_cmd = f"uv --directory {directory} run {package}"
        else:
            # npm 类型: npx -y <package>
            inner_cmd = f"npx -y {package}"
        
        # 使用 mcp-proxy (支持更好的连接管理和重连)
        cmd = build_proxy_command(port, inner_cmd, proxy_available=proxy_available)
        
        logger.info(
            "%s: 启动中 [%s] 包=%s%s 端口=%s SSE=http://localhost:%s/sse",
            name, pkg_type, package,
            f" 目录={server.get('directory')}" if pkg_type == 'uv' else "",
            port, port,
        )
        
        return self._spawn_server(name, cmd, env)
    
    def start_local_server(self, server: Dict) -> Tuple[str, subprocess.Popen]:
        """启动本地自定义 MCP Server"""
        name = server.get('name', 'unnamed')
        path = server.get('path')
        port = server.get('port')
        env_vars = server.get('env', {})
        
        if not path or not port:
            logger.error("%s: 缺少 path 或 port 配置", name)
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
                logger.error(
                    "%s: 脚本不存在 - %s (尝试路径: %s, %s)",
                    name, path, work_dir_path, config_dir_path,
                )
                return None
        
        # 准备环境变量（每进程独立 npm 缓存，避免多进程并发安装时缓存冲突）
        env = build_server_environment(os.environ, port, env_vars)
        
        # 优先使用镜像内预装的 mcp-proxy，避免 npx 并发安装
        proxy_available = bool(shutil.which("mcp-proxy"))
        # 构建命令 (使用当前 Python 解释器 sys.executable，兼容仅有 python3 的环境)
        cmd = build_proxy_command(
            port, f"{sys.executable} {script_path}", proxy_available=proxy_available
        )
        
        logger.info(
            "%s: 启动中 路径=%s 端口=%s SSE=http://localhost:%s/sse",
            name, script_path, port, port,
        )
        
        return self._spawn_server(name, cmd, env)

    def _spawn_server(self, name: str, cmd: str, env: Dict[str, str]):
        """启动进程及日志线程；配置和命令构造由调用方负责。"""
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
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            logger.error("%s: 启动失败 - %s", name, e)
            return None

    def start_all(self):
        """启动所有已启用的服务"""
        if not self.load_config():
            return

        logger.info("MCP Server 统一启动器")
        started_count = 0
        customer_servers = self.config.get('customermcp', []) or []
        basic_servers = self.config.get('basicmcp', []) or []
        groups = (
            ("第三方 MCP Server", customer_servers, self.start_package_server),
            ("本地 MCP Server", basic_servers, self.start_local_server),
        )
        for label, servers, starter in groups:
            if servers:
                logger.info("启动%s...", label)
            for server in servers:
                if not server.get('enabled', True):
                    logger.info("%s: 已跳过 (disabled)", server.get('name', 'unnamed'))
                    continue
                process = starter(server)
                if process:
                    self.processes.append(process)
                    self._restart_specs[process[0]] = (starter, server)
                    self._started_at[process[0]] = time.monotonic()
                    started_count += 1

        if started_count == 0:
            logger.warning("没有服务被启动，请检查配置文件")
            return

        logger.info("已启动 %d 个 MCP Server", started_count)
        for server in customer_servers + basic_servers:
            if server.get('enabled', True):
                logger.info(
                    "SSE 端点: %s -> http://localhost:%s/sse",
                    server.get('name', 'unnamed'), server.get('port'),
                )
        logger.info("按 Ctrl+C 停止所有服务")

        # 等待并处理信号
        self._wait_for_exit()

    def _wait_for_exit(self):
        """等待退出信号，并监控/重启子进程"""
        def signal_handler(sig, frame):
            self._stop_flag = True
            logger.info("正在停止所有服务...")
            self.stop_all()
            sys.exit(0)

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        # 给服务一点启动时间
        time.sleep(2)

        # 检查服务是否正常启动
        all_running = True
        for name, process in self.processes:
            if process.poll() is None:
                logger.info("%s: 运行中", name)
            else:
                logger.error("%s: 启动失败 (退出码: %s)", name, process.returncode)
                all_running = False
        if not all_running:
            logger.warning("部分服务启动失败，将按退避策略自动重启")
        logger.info("服务运行中，按 Ctrl+C 停止")

        while not self._stop_flag:
            time.sleep(MONITOR_INTERVAL_SECONDS)
            self._supervise_once(time.monotonic())

    def _supervise_once(self, now: float):
        """一次监控：重置稳定进程的退避、安排已退出进程的重启、执行到期重启。"""
        alive = []
        for name, process in self.processes:
            if process.poll() is None:
                if now - self._started_at.get(name, now) >= STABLE_RUN_SECONDS:
                    self._backoff.pop(name, None)
                alive.append((name, process))
            elif name in self._restart_specs:
                self._schedule_restart(name, now, f"进程已退出 (退出码: {process.returncode})")
            else:
                logger.warning("%s: 进程已退出 (退出码: %s)", name, process.returncode)
        self.processes = alive

        for name, due in list(self._pending_restart.items()):
            if self._stop_flag or due > now:
                continue
            del self._pending_restart[name]
            starter, server = self._restart_specs[name]
            logger.info("%s: 重启中", name)
            result = starter(server)
            if result:
                self.processes.append(result)
                self._started_at[name] = now
            else:
                self._schedule_restart(name, now, "重启失败")

    def _schedule_restart(self, name: str, now: float, reason: str):
        delay = self._backoff.get(name, RESTART_BACKOFF_INITIAL)
        self._backoff[name] = min(delay * 2, RESTART_BACKOFF_MAX)
        self._pending_restart[name] = now + delay
        logger.warning("%s: %s，%.0f 秒后重启", name, reason, delay)

    def stop_all(self):
        """停止所有服务"""
        self._pending_restart.clear()
        for _name, process in self.processes:
            try:
                process.terminate()
                process.wait(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                process.kill()
        logger.info("所有服务已停止")


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
        default="config/mcp_config.yaml",
        help="配置文件路径 (默认: config/mcp_config.yaml)"
    )
    parser.add_argument(
        "--list", "-l",
        action="store_true",
        help="列出所有配置的服务"
    )
    
    args = parser.parse_args()
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format=LOG_FORMAT)

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
