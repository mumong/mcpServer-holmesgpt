#!/usr/bin/env bash
# 本地启动 MCP Server Manager（与 Docker 部署同逻辑，便于调试）
# 用法: ./scripts/run-local.sh  或  CONFIG=mcp_config.local.yaml ./scripts/run-local.sh
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

CONFIG="${CONFIG:-mcp_config.yaml}"
# 若未显式指定 CONFIG 且存在 mcp_config.local.yaml，则优先使用
if [[ "$CONFIG" == "mcp_config.yaml" ]] && [[ -f "mcp_config.local.yaml" ]]; then
  CONFIG="mcp_config.local.yaml"
  echo "📄 Using local config: $CONFIG"
fi

echo "🔧 MCP Server Manager - 本地运行"
echo "   项目根目录: $ROOT_DIR"
echo "   配置文件:   $CONFIG"
echo ""

# 可选：自动使用当前目录下的 .venv
if [[ -d ".venv" ]]; then
  echo "🐍 Activating .venv..."
  # shellcheck source=/dev/null
  source .venv/bin/activate
fi

# 安装依赖（若未装）
if ! python -c "import mcp" 2>/dev/null; then
  echo "📦 Installing Python dependencies..."
  pip install -q -r requirements.txt
fi

# Node/npm 用于 mcp-proxy（本地 server 会通过 npx 调用）
if ! command -v node &>/dev/null; then
  echo "⚠️  未检测到 node，部分 MCP 将依赖 npx mcp-proxy，请安装 Node.js 或使用已预装 mcp-proxy 的镜像调试。"
fi

echo "▶️  Starting: python start.py --config $CONFIG"
echo "   SSE 端点将打印在下方，按 Ctrl+C 停止。"
echo ""

exec python start.py --config "$CONFIG"
