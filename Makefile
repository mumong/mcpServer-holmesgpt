# MCP Server Manager Makefile

IMAGE_REPOSITORY := xnet.registry.io:8443
PROJECT := xnet-cloud
IMAGE_NAME := mcp-server-manager
DOCKER_NAME := $(IMAGE_REPOSITORY)/$(PROJECT)/$(IMAGE_NAME)
NAMESPACE := mcp

VERSION ?= $(shell cat VERSION)
DOCKER_TAG := $(VERSION)

# ============================================================
# Docker
# ============================================================

.PHONY: build
build:
	@echo "🔨 Building $(DOCKER_NAME):$(DOCKER_TAG)..."
	docker build --build-arg VERSION=$(VERSION) -t $(DOCKER_NAME):$(DOCKER_TAG) .

.PHONY: push
push:
	@echo "📤 Pushing $(DOCKER_NAME):$(DOCKER_TAG)..."
	docker push $(DOCKER_NAME):$(DOCKER_TAG)

.PHONY: build-push
build-push: build push

# ============================================================
# Kubernetes
# ============================================================

.PHONY: deploy
deploy:
	@echo "🚀 Deploying to namespace: $(NAMESPACE)..."
	kubectl apply -f deploy/namespace.yaml
	kubectl apply -f deploy/

.PHONY: undeploy
undeploy:
	kubectl delete -f deploy/ --ignore-not-found

.PHONY: delete
delete:
	@echo "🗑️  Deleting all MCP resources..."
	kubectl delete deployment mcp-server-manager -n $(NAMESPACE) --ignore-not-found
	kubectl delete service mcp-server-manager -n $(NAMESPACE) --ignore-not-found
	kubectl delete configmap mcp-config -n $(NAMESPACE) --ignore-not-found
	@echo "✅ All MCP resources deleted"

.PHONY: reload
reload:
	kubectl apply -f deploy/configmap.yaml
	kubectl rollout restart deployment/mcp-server-manager -n $(NAMESPACE)

.PHONY: status
status:
	kubectl get pods,svc,configmap -n $(NAMESPACE) -l app=mcp-server-manager

.PHONY: logs
logs:
	kubectl logs -n $(NAMESPACE) -l app=mcp-server-manager -f

.PHONY: restart
restart:
	kubectl rollout restart deployment/mcp-server-manager -n $(NAMESPACE)
