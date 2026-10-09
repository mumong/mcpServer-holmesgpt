# MCP Server Manager Makefile

IMAGE_REPOSITORY := xnet.registry.io:8443
PROJECT := xnet-cloud
IMAGE_NAME := mcp-server-manager
DOCKER_NAME := $(IMAGE_REPOSITORY)/$(PROJECT)/$(IMAGE_NAME)
NAMESPACE := mcp

VERSION ?= $(shell cat VERSION)
DOCKER_TAG := $(VERSION)
PYTHON ?= python3

.PHONY: build push build-push deploy delete restart reload logs status sync-version run run-local list test test-unit check check-syntax help

# ============================================================
# Docker
# ============================================================

build:
	@echo "🔨 Building $(DOCKER_NAME):$(DOCKER_TAG)..."
	docker build --build-arg VERSION=$(VERSION) -t $(DOCKER_NAME):$(DOCKER_TAG) .

push:
	@echo "📤 Pushing $(DOCKER_NAME):$(DOCKER_TAG)..."
	docker push $(DOCKER_NAME):$(DOCKER_TAG)

build-push: build push

# ============================================================
# Kubernetes
# ============================================================

deploy:
	@echo "🚀 Deploying $(DOCKER_NAME):$(DOCKER_TAG)..."
	# 1. 同步版本到 deployment.yaml
	@sed -i 's|image: $(IMAGE_REPOSITORY)/$(PROJECT)/$(IMAGE_NAME):.*|image: $(DOCKER_NAME):$(DOCKER_TAG)|' deploy/deployment.yaml
	# 2. 创建 namespace（如果不存在）
	@kubectl create namespace $(NAMESPACE) --dry-run=client -o yaml | kubectl apply -f -
	# 3. 应用所有配置（会自动触发滚动更新）
	kubectl apply -f deploy/
	# 4. 等待滚动更新完成
	@echo "⏳ Waiting for rollout to complete..."
	kubectl rollout status deployment/mcp-server-manager -n $(NAMESPACE) --timeout=120s
	@echo "✅ Deploy completed!"

delete:
	@echo "🗑️  Deleting MCP resources (keeping namespace)..."
	kubectl delete -f deploy/deployment.yaml --ignore-not-found
	kubectl delete -f deploy/service.yaml --ignore-not-found
	kubectl delete -f deploy/configmap.yaml --ignore-not-found
	@echo "✅ All MCP resources deleted"

restart:
	@echo "🔄 Restarting pods..."
	kubectl rollout restart deployment/mcp-server-manager -n $(NAMESPACE)
	kubectl rollout status deployment/mcp-server-manager -n $(NAMESPACE) --timeout=120s
	@echo "✅ Restart completed!"

reload:
	@echo "🔃 Reloading config and restarting..."
	kubectl apply -f deploy/configmap.yaml
	kubectl rollout restart deployment/mcp-server-manager -n $(NAMESPACE)
	kubectl rollout status deployment/mcp-server-manager -n $(NAMESPACE) --timeout=120s
	@echo "✅ Reload completed!"

logs:
	kubectl logs -f deployment/mcp-server-manager -n $(NAMESPACE)

status:
	@echo "📊 MCP Server Manager Status:"
	@echo "----------------------------------------"
	kubectl get pods,svc,configmap -n $(NAMESPACE) -l app=mcp-server-manager
	@echo "----------------------------------------"

# 同步 VERSION 到 deployment.yaml
sync-version:
	@echo "🔄 Syncing version to $(DOCKER_TAG)..."
	sed -i 's|image: $(IMAGE_REPOSITORY)/$(PROJECT)/$(IMAGE_NAME):.*|image: $(DOCKER_NAME):$(DOCKER_TAG)|' deploy/deployment.yaml
	@echo "✅ Version synced!"

# ============================================================
# 开发辅助
# ============================================================

# 本地运行（默认 config/mcp_config.yaml）
run:
	@echo "🏃 Running locally..."
	python start.py

# 本地部署/调试：安装依赖后启动，优先使用 config/mcp_config.local.yaml（若存在）
run-local:
	@chmod +x scripts/run-local.sh 2>/dev/null || true
	@./scripts/run-local.sh

# 列出配置的服务
list:
	@python start.py --list

# 测试客户端连接
test:
	@echo "🧪 Testing MCP client..."
	python mcp_client.py

# 离线检查不启动服务、不安装依赖，也不连接 Kubernetes 或后端。
test-unit:
	$(PYTHON) -B scripts/test_unit.py

check-syntax:
	$(PYTHON) -B scripts/check_syntax.py

check: check-syntax test-unit

# 显示帮助
help:
	@echo "MCP Server Manager - 可用命令:"
	@echo ""
	@echo "  Docker:"
	@echo "    make build        - 构建镜像"
	@echo "    make push         - 推送镜像"
	@echo "    make build-push   - 构建并推送"
	@echo ""
	@echo "  Kubernetes:"
	@echo "    make deploy       - 部署到 K8s（自动同步版本）"
	@echo "    make delete       - 删除资源（保留 namespace）"
	@echo "    make restart      - 重启 pods"
	@echo "    make reload       - 更新配置并重启"
	@echo "    make status       - 查看状态"
	@echo "    make logs         - 查看日志"
	@echo "    make sync-version - 同步版本到 yaml"
	@echo ""
	@echo "  开发/本地部署:"
	@echo "    make run          - 本地运行（使用 config/mcp_config.yaml）"
	@echo "    make run-local    - 本地部署/调试（自动依赖检查，优先 config/mcp_config.local.yaml）"
	@echo "    make list         - 列出配置的服务"
	@echo "    make test         - 测试客户端"
	@echo "    make test-unit    - 离线单元/合同测试（不连接外部服务）"
	@echo "    make check        - Python 语法检查和离线测试"
	@echo "    make check-syntax - Python 语法检查（不生成字节码）"
	@echo ""
	@echo "  当前版本: $(DOCKER_TAG)"
	@echo "  镜像地址: $(DOCKER_NAME):$(DOCKER_TAG)"
