# GitHub Container package

The native wheel remains the recommended installation for Windows desktop clients. The OCI image exists for container-oriented MCP hosts and Linux deployments and is published to:

```text
ghcr.io/coldrazer/autonomous-intelligence
```

The image is multi-platform (`linux/amd64` and `linux/arm64`), runs as an unprivileged user, includes SBOM and provenance metadata, and is built from a digest-pinned Python base image.

## Why two containers are required

Autonomous Intelligence deliberately keeps the MCP adapter separate from the trusted Broker. A container deployment preserves that boundary. The Broker owns approval and effects; the MCP container accepts protocol messages from the LLM host.

Both containers must receive:

- The same workspace mount at `/workspace`.
- The same named state volume at `/state`.
- The same Unix socket path, `/state/broker.sock`.

## Start the Broker

Create the shared state volume once:

```powershell
docker volume create autonomous-intelligence-state
```

Start the Broker in a visible terminal. Replace `C:\path\to\workspace` with the workspace to expose:

```powershell
docker run --rm -it `
  --mount type=bind,source="C:\path\to\workspace",target=/workspace `
  --mount source=autonomous-intelligence-state,target=/state `
  --entrypoint autonomous-intelligence `
  ghcr.io/coldrazer/autonomous-intelligence:0.3.1 `
  --workspace /workspace `
  --state-dir /state `
  --pipe /state/broker.sock `
  broker
```

Do not use `--approval-mode allow` outside disposable automated tests.

## Configure an MCP client

Use Docker as the stdio command:

```json
{
  "mcpServers": {
    "autonomous-intelligence": {
      "command": "docker",
      "args": [
        "run",
        "--rm",
        "-i",
        "--mount",
        "type=bind,source=C:\\path\\to\\workspace,target=/workspace",
        "--mount",
        "source=autonomous-intelligence-state,target=/state",
        "ghcr.io/coldrazer/autonomous-intelligence:0.3.1",
        "--workspace",
        "/workspace",
        "--state-dir",
        "/state",
        "--pipe",
        "/state/broker.sock"
      ]
    }
  }
}
```

The container entrypoint is `autonomous-intelligence-mcp`, so the remaining arguments go directly to the stdio server.

## Pull and inspect

```powershell
docker pull ghcr.io/coldrazer/autonomous-intelligence:0.3.1
docker buildx imagetools inspect ghcr.io/coldrazer/autonomous-intelligence:0.3.1
```

For production automation, pin the image digest shown by the inspect command instead of using `latest`.

## Platform boundary

The image is a Linux deployment option. Native Windows installation remains necessary for future Windows UI Automation capabilities and is the path covered by the full Windows named-pipe integration test. Do not expect a Linux container to control the Windows desktop directly.
