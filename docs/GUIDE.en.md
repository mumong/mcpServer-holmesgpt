# MCP Server Manager guide

Quick start: [README](../README.en.md). This is the full reference for configuration, integration, ports and troubleshooting.


This project starts many stdio MCP servers in one place and exposes each of them as an HTTP SSE endpoint through `mcp-proxy`, so that HolmesGPT, AIOps and similar services can connect to them via `mcp_servers`.

Request path:

```text
AIOps/HolmesGPT mcp_servers.url
  -> http://mcp-server-manager.<mcp-namespace>:<port>/sse
  -> Kubernetes Service mcp-server-manager
  -> mcp-proxy
  -> stdio MCP Server
  -> optional external backends, e.g. Prometheus / Elasticsearch / runbook directory
```

Keep two kinds of URL apart:

- **MCP SSE URL**: the address AIOps uses to reach this project, for example `http://mcp-server-manager.mcp:8095/sse`.
- **External backend URL**: the address a tool uses internally to reach its backend, for example Prometheus's `PROMETHEUS_URL`.

The two can live in different namespaces; when you change a namespace, change the matching URL too.

---

## Project layout

```text
mcpstander/
├── start.py                      # launcher: reads the config, starts one mcp-proxy child per MCP
├── launch_config.py              # pure functions: child environment and proxy command
├── mcp_client.py                 # SSE test client
├── config/                       # local run configuration (mcp_config.yaml / .example.yaml)
├── servers/                      # local Python MCP servers (stdio)
│   ├── *_server.py               # one entry per MCP (k8s_core, prometheus, bash, helm, aiops_observability_query, ...)
│   ├── cluster_scope.py          # multi-cluster: target cluster of each tool call (cluster argument)
│   ├── aiops_edge/               # multi-cluster: kubectl shim and xnet tunnel client (synced copy of the Agent repo's aiops_edge/)
│   ├── holmes_tools/             # tool contracts, dispatch and the shared text-entry adapter
│   └── aiops_observability/      # collectors, case store, Pod query and the Fact Ledger
├── bin/kubectl                   # kubectl shim in the image: runs locally, or forwards to an edge runner
├── deploy/                       # single-cluster manifests (configmap / deployment / service / rbac / namespace)
│   └── envs/mc/                  # kustomize overlay for the multi-cluster center (make deploy-mc)
├── scripts/                      # local run, offline tests, syntax check, aiops_edge sync
├── docs/                         # extending with new MCPs, Pod query and Fact Ledger contract
├── tests/                        # unittest unit and contract regression tests
├── Dockerfile, Makefile, VERSION
└── requirements.txt / requirements-test.txt
```

---

## Built-in MCP services

`deploy/configmap.yaml` is authoritative; the main services are:

| MCP name | Port | Type | Description |
|---|---:|---|---|
| `elasticsearch` | 8088 | Third-party npm MCP | Elasticsearch queries; disabled in the current deployment |
| `aiops-case` | 8089 | Local Python MCP | Collectors in this repository build, persist and read Kubernetes / Prometheus / logging / DeepFlow / topology case packages |
| `aiops-observability-fine` | 8090 | Local Python MCP | Collects Pod observability evidence per dimension; enabled in the deployment, disabled in the local default |
| `test-tools` | 8091 | Local Python MCP | Test tools |
| `helm-tools` | 8092 | Local Python MCP | Helm list / values / status / history / manifest and more |
| `k8s-core` | 8093 | Local Python MCP | Kubernetes describe / get / events / logs / top / jq / lineage and more |
| `bash_command` | 8094 | Local Python MCP | Restricted bash commands and temporary debug Pods |
| `prometheus` | 8095 | Local Python MCP | General Prometheus rules / metrics / labels / series / PromQL queries; which tools are used is decided by the agent's routing and task scope, not blocked per `/ask` or `/query` entry |
| `internet` | 8096 | Local Python MCP | Fetches web page content |
| `connectivity_check` | 8097 | Local Python MCP | TCP connectivity checks |
| `core-investigation` | 8098 | Local Python MCP | Investigation planning tools |
| `runbook` | 8099 | Local Python MCP | Runbook search and retrieval |
| `aiops-observability-query` | 8100 | Local Python MCP | Pod-scoped Metrics / Logging / Tracing / Topology queries used by `/ask` Pod diagnosis; the metrics tool is `execute_pod_promql`; publishes a canonical Fact Ledger natively |

The local configuration `config/mcp_config.yaml` may use different ports from the Kubernetes deployment; at runtime the configuration file in use is authoritative:

```bash
python3 start.py --list
```

### AIOps autonomous queries and the Fact Ledger

`aiops-observability-query` exposes four general tools on `8100/sse`:

```text
execute_pod_promql
query_pod_logs
query_pod_tracing
query_pod_topology
```

Tools are not routed by fault type. Every result is bound to an exact namespace, Pod and optional Pod UID, and an `aiops.fact-ledger.v1` is built on the final 6144-byte bounded projection. The canonical ledger uses `source=mcp_canonical` and `legacy_contract=false`, and publishes only data-source facts, coverage and evidence refs; it contains no diagnosis, root cause, remediation or evaluator labels.

Contract details, boundaries and testing are in [`docs/AIOPS_OBSERVABILITY_QUERY.md`](AIOPS_OBSERVABILITY_QUERY.md).

---

## Running locally

The code uses Python 3.10+ syntax and the image is based on Python 3.11. Runtime dependencies are pinned in `requirements.txt`. Only the test code in this repository uses `jsonschema` directly, and its test constraint is in `requirements-test.txt`. The runtime image does not install that test file, but the MCP SDK itself pulls in `jsonschema`, so it is not excluded from the runtime image.

Offline verification (does not install anything when dependencies already exist):

```bash
make check       # syntax check + offline unittest contract tests
make test-unit   # offline tests only
make lint        # ruff E9/F/I/B; make format applies formatting
```

`make test` keeps the original SSE client check; it needs a running service and is not a unit test. The tests mock backends and command execution, and the offline entry additionally blocks unexpected network and subprocess access; this does not replace testing against real SSE, Kubernetes backends or the image. See the [contribution guide](../CONTRIBUTING.md).

Runtime boundaries: `start.py` owns process lifecycles (logs go to stderr; a child that exits is restarted with backoff starting at 1 s and doubling up to 60 s, reset after 60 s of stable running), and `launch_config.py` builds environments and commands. Each MCP entry keeps its tool contract and dispatches through `holmes_tools` to the collection/query implementation. Eight text entries share `holmes_tools/server_adapter.py` but keep their own argument sanitising and error policies; blocking command/HTTP calls run in worker threads and never block the MCP event loop. The query entry keeps its own structured errors and Fact Ledger contract. Command tools cap output at `MCP_TOOL_MAX_OUTPUT_BYTES` (default 1000000) and caller-supplied timeouts at `MCP_TOOL_MAX_TIMEOUT_SECONDS` (default 600).

Install dependencies and start with the default configuration:

```bash
python3 -m pip install -r requirements.txt
python3 start.py
```

Or use the Makefile:

```bash
make run
```

If your machine has `python3` but no `python` command, use `python3` when running directly; the Makefile targets keep the repository's existing convention and need a `python` command or an activated `.venv`.

For local debugging, prefer an override file instead of editing the default configuration:

```bash
cp config/mcp_config.example.yaml config/mcp_config.local.yaml
# edit config/mcp_config.local.yaml as needed
make run-local
```

`make run-local` reads `config/mcp_config.local.yaml` first. For local Prometheus debugging set `PROMETHEUS_URL` to `http://localhost:9090`, or port-forward the cluster's Prometheus first.

---

## Kubernetes deployment

Copy-paste commands for all three services (single cluster, multi-cluster, edge runners) are in the Agent README, "Quick start". To deploy only this service:

```bash
make deploy                       # existing image: apply + rolling restart + wait; a failed rollout is rolled back
echo <new version> > VERSION && make build push deploy   # after a code change
make deploy-mc                    # multi-cluster center (run make mc-init in the Agent repo first)
make status                       # Pods / Service / ConfigMap
```

`make deploy` always restarts (rolling), so a change to `deploy/configmap.yaml` alone takes effect with it (`make reload` does the same). After changing `start.py`, `launch_config.py`, `servers/` or the `Dockerfile`, bump `VERSION` and rebuild the image.

The default namespace is `mcp`, set by `NAMESPACE := mcp` in the `Makefile` and by the namespaces in `deploy/*.yaml` (including the ClusterRoleBinding subject in `rbac.yaml`); change them together.

### aiops-case data directory

`aiops-case` is not a stateless query tool. `servers/aiops_observability/case_builder.py` in this repository orchestrates the collectors and writes case packages; it does not depend on the data project or on `AIOPS_DATASET_ROOT` / `AIOPS_COLLECT_SCRIPT`.

The current deployment sets `AIOPS_CASES_DIR=/app/aiops-cases`, mounts `/root/huhu/agent/combine-aiops-mcp/aiops-cases` from the master node there via `hostPath`, and pins scheduling to `master` with a `nodeSelector`; the local default directory is `./aiops-cases`. Moving the storage needs its own plan for data retention and mounting; do not assume other nodes have this directory.

---

## Configuring MCP servers

There are two kinds of configuration file:

- **Local run**: `config/mcp_config.yaml` or `config/mcp_config.local.yaml`
- **Kubernetes**: the `data.mcp_config.yaml` content in `deploy/configmap.yaml`, mounted in the container as `/app/config/mcp_config.yaml`

The top level has two groups:

```yaml
customermcp: []  # third-party npm / uv MCP servers
basicmcp: []     # Python MCP servers under servers/ in this repository
```

### Local Python MCP

```yaml
basicmcp:
  - name: prometheus
    path: "servers/prometheus_server.py"
    port: 8095
    enabled: true
    env:
      PROMETHEUS_URL: "http://observability-prometheus.xnet.svc:9090"
      PROMETHEUS_TIMEOUT: "30"
```

Fields:

| Field | Description |
|---|---|
| `name` | MCP service name, used in logs and configuration |
| `path` | Python server file, relative to the project root |
| `port` | SSE port exposed by this MCP |
| `enabled` | Whether to start it |
| `env` | Environment variables passed to this MCP's child process |

The launcher effectively runs:

```bash
mcp-proxy --port 8095 --server sse -- python servers/prometheus_server.py
```

### Third-party npm MCP

```yaml
customermcp:
  - name: elasticsearch
    type: npm
    package: "@elastic/mcp-server-elasticsearch@0.3.1"
    port: 8088
    enabled: true
    env:
      ES_URL: "https://elasticsearch:9200"
      ES_USERNAME: "elastic"
      ES_PASSWORD: "changeme"
      ES_SSL_SKIP_VERIFY: "true"
```

`type: npm` starts the server with `npx -y <package>`. `type: uv` also needs `directory`, and starts it with `uv --directory <directory> run <package>`.

### Keeping Kubernetes ports in sync

When you add an MCP or change a port, update all four places:

1. the MCP's `port` in `deploy/configmap.yaml`
2. `containers[0].ports[].containerPort` in `deploy/deployment.yaml`
3. `spec.ports[].port` and `targetPort` in `deploy/service.yaml`
4. `mcp_servers.<name>.config.url` in the AIOps/HolmesGPT configuration

When only `env`, `enabled` or a third-party backend address changes, updating the ConfigMap and running `make deploy` or `make reload` is enough.

---

## Connecting AIOps / HolmesGPT

AIOps is configured with the **MCP SSE URL**:

```text
http://mcp-server-manager.<mcp-namespace>:<port>/sse
```

If AIOps runs in the same namespace as this project, the short form also works:

```text
http://mcp-server-manager:<port>/sse
```

Example, with this project in the `mcp` namespace:

```yaml
mcp_servers:
  prometheus:
    description: "Prometheus MCP - metrics, labels, rules and PromQL"
    config:
      url: "http://mcp-server-manager.mcp:8095/sse"
      mode: "sse"
    enabled: true

  k8s-core:
    description: "Kubernetes MCP - resources, events, logs and live metrics"
    config:
      url: "http://mcp-server-manager.mcp:8093/sse"
      mode: "sse"
    enabled: true

  aiops-observability-query:
    description: "Pod-scoped observability queries with canonical Fact Ledger"
    config:
      url: "http://mcp-server-manager.mcp:8100/sse"
      mode: "sse"
    enabled: true
```

If this project moves to the `monitor` namespace, change the AIOps URL too:

```yaml
config:
  url: "http://mcp-server-manager.monitor:8095/sse"
  mode: "sse"
```

After changing the AIOps ConfigMap, reload or restart the AIOps agent so it reloads the MCP tools.

---

## Multi-cluster mode

Enabled by mounting the cluster configuration (ConfigMap `aiops-clusters`, env `AIOPS_CLUSTERS_CONFIG`); without it this is a single cluster and the tools behave as before.

Every tool gets a `cluster` argument (`servers/cluster_scope.py`); a missing one returns `cluster_required`. `list_clusters` returns the clusters and whether they are online. Tools run in different places:

| Tools | Run where | Notes |
|---|---|---|
| Metrics, logs, traces (Prometheus, `aiops-observability-query`) | center | query the center's VictoriaMetrics / VictoriaLogs filtered by the `cluster` label; no trace data returns `capability_missing` |
| kubectl tools (`k8s-core`, Pod lifecycle, ...) | edge | the child process carries the target cluster; the `bin/kubectl` shim sends the command through the xnet tunnel to the edge's `aiops-edge-runner`, allowed by the runner's permission level |
| `bash_command` | edge clusters accept only `kubectl ... \| jq/grep` pipelines | kubectl runs on the edge, filters on the center; anything else is refused |
| `helm-tools` | local cluster only | remote clusters return `capability_missing` |

- Deploy: run `make mc-init` in the Agent repo first (cluster config, xnet certificate and signing key into the `mcp` namespace; no manual certificate handling), then `make deploy-mc` here; it stops with a hint if those objects are missing.
- `servers/aiops_edge/` is a synced copy of the Agent repo's `aiops_edge/`; do not edit it here. Sync with `make sync-aiops-edge` (copies from `../robusta/aiops_edge` by default; with the README's clone name use `./scripts/sync_aiops_edge.sh ../holmegpt-agent/aiops_edge`); `tests/test_aiops_edge_sync.py` checks both sides match.
- Full design and implementation: `docs/multi-cluster-design.md` in the Agent repo (single source).

## External backend URLs and namespaces

Some MCP tools call external services whose addresses are set in that MCP's `env`. Prometheus is the one most often confused:

```yaml
- name: prometheus
  path: "servers/prometheus_server.py"
  port: 8095
  enabled: true
  env:
    PROMETHEUS_URL: "http://observability-prometheus.xnet.svc:9090"
```

`PROMETHEUS_URL` here is the Prometheus service address, not the SSE address AIOps uses to reach MCP.

The usual Kubernetes Service DNS form:

```text
http://<service-name>.<service-namespace>.svc:<service-port>
```

So if the Prometheus service is still called `observability-prometheus` but moves from `xnet` to `monitor`, change it to:

```yaml
env:
  PROMETHEUS_URL: "http://observability-prometheus.monitor.svc:9090"
```

If the service name changes as well, change both, for example:

```yaml
env:
  PROMETHEUS_URL: "http://prometheus-k8s.monitor.svc:9090"
```

### What to change when a namespace moves

| Change | What to update |
|---|---|
| Only Prometheus moves from `xnet` to `monitor` | Only `PROMETHEUS_URL` in `deploy/configmap.yaml`, to `http://observability-prometheus.monitor.svc:9090`, then `make deploy` |
| Only MCP Server Manager moves from `mcp` to `monitor` | This project's namespace in `deploy/*.yaml` and the `Makefile`; AIOps `mcp_servers.*.config.url` becomes `http://mcp-server-manager.monitor:<port>/sse` |
| Both move to `monitor` | Both sides: `PROMETHEUS_URL` uses `observability-prometheus.monitor.svc`, and the AIOps SSE URL uses `mcp-server-manager.monitor` |

Likewise, Elasticsearch, the runbook directory and the bash safety policy are configured independently through each MCP's `env`:

| MCP | Common environment variables |
|---|---|
| `prometheus` | `PROMETHEUS_URL`, `PROMETHEUS_TIMEOUT` |
| `elasticsearch` | `ES_URL`, `ES_USERNAME`, `ES_PASSWORD`, `ES_SSL_SKIP_VERIFY` |
| `runbook` | `RUNBOOK_SEARCH_PATH` |
| `bash_command` | `BASH_BLOCKED_COMMANDS`, `BASH_BLOCKED_PATTERNS`, `BASH_TOOL_UNSAFE_ALLOW_ALL` |

---

## Verification

List the current configuration:

```bash
python3 start.py --list
```

After starting locally, connect the test client to an SSE endpoint:

```bash
python3 mcp_client.py http://localhost:8095/sse
```

After deploying to Kubernetes, test the Service from inside the cluster:

```bash
kubectl run curl-test --rm -it --image=curlimages/curl -n mcp -- \
  curl -s -o /dev/null -w "%{http_code}" http://mcp-server-manager:8095/sse
```

If this project runs in the `monitor` namespace, replace `-n mcp` with `-n monitor`.

For the Prometheus MCP, also confirm that `PROMETHEUS_URL` is reachable from the Pod:

```bash
kubectl exec -n mcp deployment/mcp-server-manager -- \
  curl -s http://observability-prometheus.xnet.svc:9090/-/ready
```

If Prometheus has moved to `monitor`:

```bash
kubectl exec -n mcp deployment/mcp-server-manager -- \
  curl -s http://observability-prometheus.monitor.svc:9090/-/ready
```

---

## Makefile targets

| Target | Description |
|---|---|
| `make build` / `make push` / `make build-push` | build, push the image |
| `make deploy` | deploy to Kubernetes: apply, rolling restart, wait; a failed rollout is rolled back |
| `make deploy-mc` | deploy to the multi-cluster center (run `make mc-init` in the Agent repo first) |
| `make reload` / `make restart` | apply the ConfigMap and restart / restart only |
| `make delete` | delete the resources, keep the namespace |
| `make status` / `make logs` | resources / logs |
| `make run` / `make run-local` / `make list` | run the default config / prefer `config/mcp_config.local.yaml` / list configured MCPs |
| `make check` / `make test-unit` / `make lint` / `make format` | syntax + offline tests / offline tests only / ruff check / format |
| `make test` | test a running SSE service with `mcp_client.py` |
| `make sync-aiops-edge` | sync `servers/aiops_edge/` from the Agent repo |

---

## Troubleshooting

| Symptom | Common cause | Fix |
|---|---|---|
| AIOps shows 0 tools | Wrong namespace/port in `mcp_servers.*.config.url`, or AIOps was not reloaded | Check `mcp-server-manager.<namespace>:<port>/sse`; restart or reload AIOps |
| `/sse` unreachable inside the cluster | ConfigMap port, Deployment containerPort and Service port disagree | Check the four places in "Keeping Kubernetes ports in sync" |
| Prometheus queries fail | `PROMETHEUS_URL` points to the wrong namespace/service/port, or NetworkPolicy/RBAC blocks it | curl the Prometheus ready endpoint from the Pod and confirm `http://<service>.<namespace>.svc:<port>` is reachable |
| Local start fails | Missing Python/Node dependencies, port in use, wrong server `path` | Install `requirements.txt`, check `node`/`mcp-proxy`, run `python3 start.py --list` |
| A config change has no effect | The ConfigMap changed but the Pod was not restarted | Run `make deploy` (it restarts) or `make reload` |

Steps for adding a new MCP are in [docs/EXTENDING.md](EXTENDING.md) (Chinese).

## License

The project's own code is licensed under the [Apache License 2.0](../LICENSE), which permits commercial use. Third-party code and dependencies keep their own licenses and attribution and are not relicensed by this project. Modules marked as ported from Holmes still need their upstream version and license material verified; this repository's LICENSE alone does not establish that their third-party distribution is compliant.
