# MCP client setup

Autonomous Intelligence is host-neutral. Any client that can start a local MCP server over standard input/output can use the same executable and tool surface.

The host starts the MCP adapter. The separately trusted Broker must already be running for the same workspace. Both processes derive the same authenticated local IPC address and state directory from the canonical workspace path.

## Before connecting a client

Install the package into a stable environment and identify the full executable paths:

```powershell
python -m venv "$env:LOCALAPPDATA\AutonomousIntelligence\runtime"
& "$env:LOCALAPPDATA\AutonomousIntelligence\runtime\Scripts\python.exe" -m pip install autonomous-intelligence

$AiMcp = "$env:LOCALAPPDATA\AutonomousIntelligence\runtime\Scripts\autonomous-intelligence-mcp.exe"
$AiCli = "$env:LOCALAPPDATA\AutonomousIntelligence\runtime\Scripts\autonomous-intelligence.exe"
$Workspace = "C:\path\to\allowed-workspace"
```

Until a package is published to PyPI, replace the final install argument with the repository path or a downloaded wheel.

Start the approval Broker in a visible terminal:

```powershell
& $AiCli --workspace $Workspace broker
```

Keep `--approval-mode prompt`, the default, outside disposable tests.

## Generate an exact configuration

The CLI can render host-specific configuration with an absolute executable and workspace path:

```powershell
autonomous-intelligence --workspace $Workspace client-config codex --mcp-command $AiMcp
autonomous-intelligence --workspace $Workspace client-config claude --mcp-command $AiMcp
autonomous-intelligence --workspace $Workspace client-config kimi --mcp-command $AiMcp
autonomous-intelligence --workspace $Workspace client-config antigravity --mcp-command $AiMcp
autonomous-intelligence --workspace $Workspace client-config gemini --mcp-command $AiMcp
autonomous-intelligence --workspace $Workspace client-config cursor --mcp-command $AiMcp
autonomous-intelligence --workspace $Workspace client-config vscode --mcp-command $AiMcp
autonomous-intelligence --workspace $Workspace client-config generic --mcp-command $AiMcp
```

The command only prints configuration. It never replaces an existing client file.

## Verified host formats

| Host | User configuration | Project configuration | Setup or verification |
|---|---|---|---|
| OpenAI Codex, ChatGPT desktop, Codex IDE | `~/.codex/config.toml` | `.codex/config.toml` | `codex mcp add`, `codex mcp get`, `codex mcp list` |
| Claude Code | `~/.claude.json` | `.mcp.json` | `claude mcp add`, `claude mcp get`, `claude mcp list`, `/mcp` |
| Kimi Code CLI | `~/.kimi/mcp.json` or `~/.kimi-code/mcp.json` | `.kimi-code/mcp.json` | `kimi mcp add` on releases that expose it; otherwise `/mcp-config` or JSON |
| Google Antigravity IDE/CLI | `~/.gemini/config/mcp_config.json` | `.agents/mcp_config.json` | `/mcp` and the MCP Manager |
| Gemini CLI | `~/.gemini/settings.json` | `.gemini/settings.json` | `gemini mcp add`, `gemini mcp list`, `/mcp` |
| Cursor | `~/.cursor/mcp.json` | `.cursor/mcp.json` | Cursor Settings → Tools & Integrations |
| VS Code / GitHub Copilot | MCP user configuration | `.vscode/mcp.json` | `MCP: List Servers` or `code --add-mcp` |
| Other MCP clients | Client-specific | Client-specific | Use `mcp-config.example.json` or `client-config generic` |

The repository includes project-level configurations for every host in this table. They intentionally use the command on `PATH`; replace it with a full executable path when the client does not inherit the installation environment.

## Codex

```powershell
codex mcp add autonomous-intelligence -- $AiMcp --workspace $Workspace
codex mcp get autonomous-intelligence
codex mcp list
```

Codex reads `~/.codex/config.toml`; its CLI, IDE extension, and ChatGPT desktop app share that local configuration. Keep `autonomous_write_file` in prompt mode. See the [official Codex MCP documentation](https://developers.openai.com/codex/mcp/).

## Claude Code

```powershell
claude mcp add --scope user --transport stdio autonomous-intelligence -- `
  $AiMcp --workspace $Workspace
claude mcp get autonomous-intelligence
claude mcp list
```

For a team-scoped configuration, commit `.mcp.json` and let Claude Code request workspace trust. See the [official Claude Code MCP documentation](https://code.claude.com/docs/en/mcp).

## Kimi Code CLI

Current Kimi releases support a JSON `mcpServers` object. Some releases also provide the non-interactive command:

```powershell
kimi mcp add --transport stdio autonomous-intelligence -- `
  $AiMcp --workspace $Workspace
kimi mcp test autonomous-intelligence
```

If the installed release does not expose `kimi mcp`, merge the generated Kimi entry into `~/.kimi-code/mcp.json` or the project `.kimi-code/mcp.json`, then open `/mcp`. See the [official Kimi Code MCP documentation](https://moonshotai.github.io/kimi-code/en/customization/mcp.html).

## Antigravity

Merge the generated Antigravity entry into either:

- `~/.gemini/config/mcp_config.json` for global use.
- `.agents/mcp_config.json` for one workspace.

Open `/mcp` in Antigravity CLI or use the IDE's MCP Manager to reload and inspect the connection. See the [official Antigravity MCP documentation](https://antigravity.google/docs/mcp).

## Gemini CLI

```powershell
gemini mcp add --scope user autonomous-intelligence $AiMcp -- `
  --workspace $Workspace
gemini mcp list
```

Alternatively merge the generated entry into `~/.gemini/settings.json`. Keep `trust` set to `false` so the host continues to request approval. See the [official Gemini CLI MCP documentation](https://geminicli.com/docs/tools/mcp-server/).

## Cursor

Merge the generated entry into `~/.cursor/mcp.json`, or use the included `.cursor/mcp.json` in a project. Cursor asks for approval before MCP tool calls by default. See the [official Cursor MCP documentation](https://docs.cursor.com/context/model-context-protocol).

## VS Code and GitHub Copilot

Use the included `.vscode/mcp.json`, run `MCP: Add Server`, or register the server in the current VS Code profile:

```powershell
$Definition = @{ name = "autonomous-intelligence"; command = $AiMcp; args = @("--workspace", $Workspace) } | ConvertTo-Json -Compress
code --add-mcp $Definition
```

Use `MCP: List Servers` to start, stop, and inspect it. See the [official VS Code MCP documentation](https://code.visualstudio.com/docs/agent-customization/mcp-servers).

## Generic stdio contract

All other clients need the same three fields:

```json
{
  "mcpServers": {
    "autonomous-intelligence": {
      "command": "C:\\absolute\\path\\to\\autonomous-intelligence-mcp.exe",
      "args": ["--workspace", "C:\\absolute\\path\\to\\workspace"]
    }
  }
}
```

The adapter writes only MCP protocol frames to standard output. Diagnostics go through logging, and tool execution is delegated to the separate Broker.

## Host-independent verification

1. Start the Broker for the exact canonical workspace path.
2. Restart or reload the client after changing its configuration.
3. Confirm that the server exposes five tools and the `autonomous-intelligence://capabilities` resource.
4. Call `autonomous_read_file` for a small file inside the workspace.
5. Call `autonomous_write_file` for a disposable new file and confirm both host and Broker approval behavior.
6. Stop the Broker and confirm tools fail with `broker_unavailable` rather than accessing the host directly.

Do not enable YOLO, auto-run, or trusted-server modes merely to make setup easier. Host approval is defense in depth; the Broker remains the authoritative approval and policy boundary.
