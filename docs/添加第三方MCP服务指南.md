# 添加第三方 MCP 服务指南

本文档说明如何在本项目中**完整添加一个第三方 MCP 服务**（如 npm 包、uv 包），需要修改哪些文件、添加哪些配置。

---

## 一、需要修改的文件（3 处必须一致）

| 文件 | 作用 |
|------|------|
| `deploy/configmap.yaml` | 定义服务名、包名、端口、环境变量等 |
| `deploy/deployment.yaml` | 声明容器暴露的端口 |
| `deploy/service.yaml` | 声明 Service 暴露的端口 |

**重要**：三处使用的 **端口号必须一致**。

---

## 二、以「bash 工具」为例（npm 类型）

假设要添加一个名为 `bash` 的 MCP 服务，使用 npm 包 `mcp-server-bash`，端口 `8094`。

### 1. 修改 `deploy/configmap.yaml`

在 `data.mcp_config.yaml` 的 **customermcp** 列表下新增一项：

```yaml
customermcp:
  # ... 已有服务 ...

  - name: bash
    type: npm
    package: "mcp-server-bash"
    port: 8094
    enabled: true
    # 若该包需要环境变量，可加 env
    # env:
    #   SOME_KEY: "value"
```

**字段说明**：

| 字段 | 必填 | 说明 |
|------|------|------|
| name | 是 | 服务显示名，用于日志和 AIOPS 配置 |
| type | 是 | `npm` 或 `uv` |
| package | 是 | npm 包名（可带版本，如 `mcp-server-bash@1.0.0`） |
| port | 是 | 独占端口，不与现有服务冲突 |
| enabled | 否 | 默认 `true`，设为 `false` 可暂时禁用 |
| env | 否 | 传给子进程的环境变量 |

**uv 类型示例**（Python 包）：

```yaml
- name: mysql
  type: uv
  package: "mysql_mcp_server"
  directory: "/opt/mcp/mysql_mcp_server"
  port: 8084
  enabled: true
  env:
    MYSQL_HOST: "mysql"
    MYSQL_PASSWORD: "xxx"
```

### 2. 修改 `deploy/deployment.yaml`

在 `containers[0].ports` 中增加一个端口：

```yaml
ports:
  - name: es-mcp
    containerPort: 8088
  # ... 其他已有端口 ...
  - name: bash-mcp
    containerPort: 8094
```

- `name` 建议用「服务简短名-mcp」，如 `bash-mcp`，便于识别。
- `containerPort` 必须与 ConfigMap 里的 `port` 一致（此处为 8094）。

### 3. 修改 `deploy/service.yaml`

在 `spec.ports` 中增加一个端口：

```yaml
ports:
  - name: es-mcp
    port: 8088
    targetPort: 8088
  # ... 其他已有端口 ...
  - name: bash-mcp
    port: 8094
    targetPort: 8094
```

- `name` 与 deployment 中的端口名一致即可（如 `bash-mcp`）。
- `port` / `targetPort` 都与 ConfigMap 的 `port` 一致（8094）。

---

## 三、可选：本地开发配置

若在本地用 `python start.py` 调试，可同步改 **`mcp_config.yaml`**（项目根目录）：

```yaml
customermcp:
  # ...
  - name: bash
    type: npm
    package: "mcp-server-bash"
    port: 8094
    enabled: true
```

本地与 K8s 使用同一端口，便于行为一致。

---

## 四、部署与验证

1. **仅改 ConfigMap 时**（不增删端口）：
   ```bash
   make reload
   ```

2. **有增删端口时**（如新增 bash 的 8094）：
   ```bash
   make deploy
   ```

3. **看日志确认服务已启动**：
   ```bash
   kubectl logs -f -n mcp deployment/mcp-server-manager
   ```
   期望看到类似：
   ```text
   [bash] starting server on port 8094
   ```

4. **在 AIOPS / HolmesGPT 中配置该 MCP**（示例）：
   ```yaml
   bash:
     description: "Bash MCP 工具"
     config:
       url: "http://mcp-server-manager.mcp:8094/sse"
       mode: "sse"
     enabled: true
   ```
   若 MCP 部署在其他 namespace，将 `mcp` 换成实际 namespace。

---

## 五、检查清单

- [ ] `deploy/configmap.yaml`：在 customermcp 中新增 name、type、package、port（及可选 env）
- [ ] `deploy/deployment.yaml`：在 ports 中新增对应 containerPort
- [ ] `deploy/service.yaml`：在 ports 中新增对应 port/targetPort
- [ ] 端口在三处一致且未被其他服务占用
- [ ] 执行 `make deploy` 或 `make reload` 并查看日志确认启动
- [ ] （可选）在 AIOPS 配置中增加该 MCP 的 url 与 enabled

---

## 六、当前已占用端口参考

| 端口 | 服务名 |
|------|--------|
| 8088 | elasticsearch |
| 8091 | test-tools |
| 8092 | helm-tools |
| 8093 | kubernetes |

新增服务请选用未使用的端口（如 8094、8095…）。
