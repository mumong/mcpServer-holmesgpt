# MCP Server Manager
![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)

English | [简体中文](README.md)

## Table of contents
- [Overview](#overview)
- [Key features](#key-features)
- [Quick start](#quick-start)
- [Architecture](#architecture)
- [Documentation](#documentation)
- [License](#license)

## Overview
The tool service of K8s AIOps Copilot: it starts several stdio MCP servers and exposes each as an HTTP SSE endpoint that the AIOps Agent connects to through `mcp_servers`.
- One Pod and one config manage every MCP server
- A single cluster is accessed directly; with multiple clusters, kubectl goes through the xnet tunnel to the edge runner
- Command tools (bash / kubectl) are bound by the dangerous-command rules; repairs run only after the Agent's approval

## Key features
### Built-in tools
✔️ Kubernetes: get / describe / events / logs / top / object lineage (`k8s-core`, 8093)  
✔️ Metrics, logs, traces: PromQL and Pod-scoped observability queries (`prometheus` 8095, `aiops-observability-query` 8100)  
✔️ Helm, controlled bash, connectivity checks, runbook search and more (ports 8088–8100, see `deploy/configmap.yaml`)  

### Multi-cluster
⚡ Every tool takes a `cluster` argument; `list_clusters` returns the clusters and whether they are online  
⚡ Metrics and logs come from the center's VictoriaMetrics / VictoriaLogs, filtered by the `cluster` label  
⚡ kubectl is forwarded by the `bin/kubectl` shim through xnet to the edge runner; helm runs on the local cluster only  

## Quick start
### Requirements
- A Kubernetes cluster; a deploy host with `make` and `kubectl`
- Images are published on `xnet.registry.io:8443`, so nothing to build
- To deploy all three services (MCP, Agent, frontend) together, follow "Quick start" in the Agent README

### Deploy
```bash
make deploy        # single cluster: apply + rolling restart + wait; a failed rollout is rolled back
make deploy-mc     # multi-cluster center (run make mc-init in the Agent repository first)
make status        # Pods / Service / ConfigMap
```

### Change configuration or upgrade
```bash
vi deploy/configmap.yaml && make deploy                  # tool addresses, switches and other settings
echo <new version> > VERSION && make build push deploy   # after a code change
```

### Run locally
```bash
python3 -m pip install -r requirements.txt
make run           # or make run-local (reads config/mcp_config.local.yaml)
make check         # syntax check + offline unit tests
```

## Architecture
```
AIOps Agent ──SSE──> mcp-server-manager:<port>/sse ──> mcp-proxy ──> stdio MCP server ──> K8s API / Prometheus / VictoriaMetrics …
                                                                   └─(multi-cluster) bin/kubectl ──xnet tunnel──> edge runner
```

## Documentation
| Document | Content |
|---|---|
| [Guide](docs/GUIDE.en.md) | Configuration, port sync, namespace changes, AIOps integration, checks, Makefile, FAQ |
| [Adding an MCP server](docs/EXTENDING.md) | Third-party or local tools |
| [Pod observability queries](docs/AIOPS_OBSERVABILITY_QUERY.md) | Query tools and the Fact Ledger contract |

## License
[Apache License 2.0](LICENSE). Third-party code keeps its own licenses.
