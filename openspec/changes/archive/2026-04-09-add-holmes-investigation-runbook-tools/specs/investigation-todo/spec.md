## ADDED Requirements

### Requirement: TodoWrite tool registration
The system SHALL register a `TodoWrite` tool in the holmes-tools MCP server that accepts a complete task list and returns formatted investigation plan.

#### Scenario: Successful task plan creation
- **WHEN** Agent calls `TodoWrite` with a `todos` array containing task objects (each with `id`, `content`, `status`)
- **THEN** system returns formatted task list with status icons ([ ] pending, [~] in_progress, [✓] completed, [✗] failed) and summary counts

#### Scenario: Empty todos array
- **WHEN** Agent calls `TodoWrite` with an empty `todos` array
- **THEN** system returns success message with "No tasks in the investigation plan."

#### Scenario: Missing todos parameter
- **WHEN** Agent calls `TodoWrite` without `todos` parameter
- **THEN** system returns JSON error: `{"error": "missing parameter: todos"}`

#### Scenario: String-encoded JSON input
- **WHEN** Agent calls `TodoWrite` with `todos` as a JSON string instead of array
- **THEN** system parses the string as JSON and processes normally

### Requirement: TodoWrite tool discovery
The system SHALL expose `TodoWrite` in the MCP `list_tools` response with accurate inputSchema.

#### Scenario: Tool listed in MCP tools
- **WHEN** client calls `list_tools` on the holmes-tools MCP server
- **THEN** response includes a tool named `TodoWrite` with description containing "investigation tasks" and inputSchema defining `todos` as required array parameter
