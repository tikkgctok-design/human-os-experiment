# Windows production runtime

Human OS uses three separate loopback processes:

| Process | Bind | Responsibility |
|---|---:|---|
| Local API | `127.0.0.1:8765` | Read-only retrieval transport |
| Bridge | `127.0.0.1:8787` | Internal auth, limits and Local API isolation |
| Tool API | `127.0.0.1:8899` | Exact ChatGPT/OpenAPI contract and external auth |

Create `private/tool.env` with a token different from the bridge token. Generate the
token without printing it:

```powershell
.venv\Scripts\python.exe -c "import pathlib,secrets; p=pathlib.Path('private/tool.env'); p.write_text('HUMAN_OS_TOOL_TOKEN='+secrets.token_hex(32)+'\nHUMAN_OS_TOOL_HOST=127.0.0.1\nHUMAN_OS_TOOL_PORT=8899\nHUMAN_OS_TOOL_BRIDGE_URL=http://127.0.0.1:8787\nHUMAN_OS_TOOL_TIMEOUT=10\nHUMAN_OS_TOOL_BODY_LIMIT=65536\nHUMAN_OS_TOOL_RATE_LIMIT=60\nHUMAN_OS_TOOL_RATE_WINDOW=60\nHUMAN_OS_TOOL_AUDIT_LOG=private/tool-audit.jsonl\nHUMAN_OS_TOOL_REQUIRE_HTTPS=true\n',encoding='utf-8')"
```

Start the foreground supervisor:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/Start-HumanOSTool.ps1
```

The supervisor starts hidden child processes, waits for all three loopback ports,
writes only logs/PID state below ignored `private/runtime/`, stops all children if one
fails, and performs a clean coordinated shutdown on `Ctrl+C` or task termination.

For unattended startup, create a Windows Task Scheduler entry manually with:

- Program: `powershell.exe`
- Arguments: `-NoProfile -ExecutionPolicy Bypass -File "YOUR_REPOSITORY_PATH\scripts\Start-HumanOSTool.ps1"`
- Start in: `YOUR_REPOSITORY_PATH`
- Run whether the user is logged on or not;
- Restart on failure;
- Trigger at system startup or user logon, according to the storage availability.

The task account must have read access to the repository, private DB and RAW archive,
but no public file share is required. Do not put tokens in Task Scheduler arguments.
The stable TLS ingress is a separate process/service and must satisfy
[`deploy/INGRESS.md`](INGRESS.md).
