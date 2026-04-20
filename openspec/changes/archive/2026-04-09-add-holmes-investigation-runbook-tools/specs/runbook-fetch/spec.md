## ADDED Requirements

### Requirement: fetch_runbook tool registration
The system SHALL register a `fetch_runbook` tool in the holmes-tools MCP server that retrieves Runbook content by ID from configured search paths.

#### Scenario: Successful runbook fetch
- **WHEN** Agent calls `fetch_runbook` with a valid `runbook_id` (e.g., `dns_troubleshooting.md`) and the file exists in `RUNBOOK_SEARCH_PATH`
- **THEN** system returns the runbook content wrapped in `<runbook>` tags with execution instructions

#### Scenario: Runbook not found
- **WHEN** Agent calls `fetch_runbook` with a `runbook_id` that does not exist in any search path
- **THEN** system returns error message listing the searched paths

#### Scenario: Empty runbook_id
- **WHEN** Agent calls `fetch_runbook` with empty or missing `runbook_id`
- **THEN** system returns error: "runbook_id cannot be empty."

#### Scenario: Path traversal prevention
- **WHEN** Agent calls `fetch_runbook` with a `runbook_id` containing `../` that resolves outside the search path
- **THEN** system rejects the request and returns "not found" (does not expose files outside search paths)

### Requirement: Runbook search path configuration
The system SHALL read `RUNBOOK_SEARCH_PATH` environment variable to determine where to search for runbook files.

#### Scenario: RUNBOOK_SEARCH_PATH configured
- **WHEN** `RUNBOOK_SEARCH_PATH` is set to `/data/runbooks:/opt/runbooks`
- **THEN** system searches both directories in order for the requested runbook

#### Scenario: RUNBOOK_SEARCH_PATH not set
- **WHEN** `RUNBOOK_SEARCH_PATH` is not set or empty
- **THEN** system falls back to current working directory as the only search path

### Requirement: fetch_runbook tool discovery
The system SHALL expose `fetch_runbook` in the MCP `list_tools` response with dynamic description listing available runbook files.

#### Scenario: Tool listed with available runbooks
- **WHEN** client calls `list_tools` and runbook files exist in search paths
- **THEN** response includes `fetch_runbook` tool with description listing available `.md` filenames
