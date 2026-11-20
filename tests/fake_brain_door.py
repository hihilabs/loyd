#!/usr/bin/env python3
"""A stand-in for hihilabs' brain door: newline JSON-RPC on stdio, three tools (one not allowed)."""
import json
import os
import sys

TOOLS = [{"name": "project_brief", "description": "One project's memory.",
          "inputSchema": {"type": "object", "properties": {"project_id": {"type": "integer"}}, "required": ["project_id"]}},
         {"name": "create_project_memory", "description": "Save a memory.", "inputSchema": {"type": "object", "properties": {}}},
         {"name": "log_handoff", "description": "Leave a handoff.", "inputSchema": {"type": "object", "properties": {}}},
         {"name": "arm_paid_api", "description": "Not for the door.", "inputSchema": {"type": "object", "properties": {}}}]
if os.environ.get("SSH_ORIGINAL_COMMAND") != "mcp":
    print(json.dumps({"error": "unknown verb"})); sys.exit(2)
for line in sys.stdin:
    m = json.loads(line)
    if "id" not in m:
        continue
    if m["method"] == "initialize":
        r = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "0"}}
    elif m["method"] == "tools/list":
        r = {"tools": TOOLS}
    elif m["method"] == "tools/call":
        p = m["params"]
        import time
        time.sleep(0.4)                       # a real call takes a moment; the client must wait for it
        if p["name"] == "log_handoff" and p["arguments"].get("boom"):
            r = {"content": [{"type": "text", "text": "project_id is required"}], "isError": True}
        else:
            r = {"content": [{"type": "text", "text": json.dumps({"tool": p["name"], "args": p["arguments"]})}]}
    else:
        print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32601, "message": "no such method"}}), flush=True)
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": r}), flush=True)
