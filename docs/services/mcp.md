---
title: MCP connections
description: Connect external tool servers and manage their tools in Astra Demo.
---

# MCP connections

Astra can use tools from a Model Context Protocol (MCP) server during a conversation.
It supports local stdio subprocesses and Streamable HTTP endpoints. Install the
pinned client in the demo environment.

```bash
.venv/bin/python -m pip install -r requirements-integrations.txt
```

## Connect and choose tools

1. Open **MCP** in the demo and select **Enable MCP**.
2. Select **Add server**. For stdio, enter the executable, argument list, and
   working directory for the installed server. For Streamable HTTP, enter its
   final MCP endpoint URL. If it needs credentials, map child environment
   variables or HTTP header names to environment variables available to Astra.
3. Enable and save the server, then select **Connect** and **Refresh tools**.
   On a fresh workspace, refresh the catalog to load any tool choices from the launch YAML.
4. Inspect each **Tool definition** you want to enable. Select the tools, set
   **Require execution approval** as appropriate, and select **Save tool choices**.
   Test a read-only tool in a new agent turn.

New catalog tools start disabled when discovered through the UI. The demo saves
enabled names, reviewed contract fingerprints, and approval settings in its
workspace copy of `integrations.mcp`. Restore or restart reloads the launch YAML
and starts with disconnected servers. A changed tool contract requires
review before it can be selected again. A validation issue includes the affected
part of the schema when available. Reload before saving again if a form is stale.

## Execution and result limits

`MCPManagement` handles configuration and catalog actions through `/api/mcp`.
The integration publishes reviewed definitions through the capability registry.
Each admitted turn captures a catalog snapshot and uses it for both schema
exposure and dispatch. The registry still checks capability authority and
arguments at dispatch, and the executor applies the configured approval.

Each call preflights the reviewed contract against the server. Connections,
catalogs, arguments, and results have bounded deadlines and sizes. Astra supports
text, structured JSON, and resource-link metadata. Binary and embedded-resource
results produce an unsupported-content error. If a transport failure leaves
completion uncertain, Astra suppresses the tool for the rest of the turn and
avoids replaying the remote call. When the prompt guard is enabled, it can
apply additional [request-local result compaction](../architecture/context-building.md#per-request-prompt-budget).

The management and transport implementations live in `app/integrations/` and
`app/http/mcp.py`. The launch settings are validated by `MCPConfig` and `MCPServerConfig` in
`app/config.py`.
