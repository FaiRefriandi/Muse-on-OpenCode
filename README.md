# Muse on OpenCode

**English** | [Bahasa Indonesia](README.id.md)

Run your [Muse](https://muse.ai) personal agent as a model inside [OpenCode](https://opencode.ai) on your laptop. Muse reads and edits your local workspace through OpenCode's own tools, shows reasoning blocks per step, searches the web from its VM, and understands images.

No API key from any model provider is needed. Everything runs on your own Muse VM plus a free tunnel.

## How it works

```
OpenCode (your laptop)
  -> HTTPS tunnel (localhost.run, stable subdomain)
    -> 9Router (Muse VM, 127.0.0.1:20128)
      -> bridge.py (Muse VM, 127.0.0.1:8765)
        -> Muse worker agent (Muse VM, pulls jobs and answers)
```

- `bridge.py` exposes an OpenAI-compatible `/v1/chat/completions` endpoint and queues each request as a JSON file.
- A Muse worker (a scheduled task or hook inside Muse) polls the queue, answers with text or `tool_calls`, and posts progress and reasoning notes while it works.
- OpenCode executes the tool calls locally on your laptop and sends the results back. Your files never leave your laptop; only the conversation passes through the tunnel.
- 9Router sits in front of the bridge as the public API gateway (API keys, model routing).

## Features

- **Workspace agent**: read, edit, write files and run shell commands through OpenCode tools, batched up to 8 calls per step.
- **Reasoning blocks**: the worker's plan and findings appear as thinking blocks in OpenCode, per step.
- **Web search**: current-information questions are searched by the worker from the VM, with sources. No third-party search API key on your laptop.
- **Images**: attach screenshots or images in OpenCode; the worker views them.
- **1M context**: the example config declares a 1,000,000-token context window.
- **Stable URL**: key-based localhost.run subdomain that survives reconnects and VM restarts, so your `opencode.json` never changes.

## Requirements

- A Muse account with its VM (this is where Muse runs).
- A laptop with Node.js (for OpenCode) and SSH.
- A free [localhost.run](https://localhost.run) account (for the stable tunnel subdomain).

## Setup

Everything in steps 1 to 4 runs **inside the Muse VM**. Ask Muse to do it for you, or run the commands yourself in the VM shell.

### 1. Install the bridge

```bash
mkdir -p ~/muse-bridge/queue
cp bridge.py ~/muse-bridge/
cd ~/muse-bridge

# Create the two API keys. Each key is printed ONCE. Save them to files with 600 permissions.
python3 bridge.py keygen --role worker --label muse-vm
python3 bridge.py keygen --role user   --label 9router
```

Run it as a systemd service (`/etc/systemd/system/muse-bridge.service`):

```ini
[Unit]
Description=Muse bridge
After=network.target

[Service]
ExecStart=/usr/bin/python3 /home/<user>/muse-bridge/bridge.py
Environment=BRIDGE_QUEUE=/home/<user>/muse-bridge/queue
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
```

```bash
systemctl daemon-reload && systemctl enable --now muse-bridge
curl http://127.0.0.1:8765/health   # -> {"ok": true}
```

### 2. Install 9Router

```bash
npm config set prefix ~/.npm-global
npm install -g 9router
```

Run it as a systemd service (`/etc/systemd/system/9router.service`). Set a strong dashboard password via `INITIAL_PASSWORD` in an env file (600 permissions):

```ini
[Unit]
Description=9Router
After=network.target

[Service]
ExecStart=/home/<user>/.npm-global/bin/9router --port 20128 --host 127.0.0.1 --no-browser --skip-update
EnvironmentFile=/home/<user>/muse-bridge/9router.env
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
```

`9router.env` contains one line: `INITIAL_PASSWORD=<a strong password you generate>`.

```bash
systemctl daemon-reload && systemctl enable --now 9router
```

### 3. Register the bridge in 9Router

Open the 9Router dashboard (forward port 20128 or use the tunnel from step 4 first), then:

1. **Provider node**: prefix `musevm`, API type `chat`, base URL `http://127.0.0.1:8765/v1`.
   - The prefix must not be `muse`; that name collides with 9Router's built-in Muse provider and breaks credential lookup.
2. **Connection**: API key = the bridge **user** key from step 1, default model `muse-spark`.
   - The model name matters: 9Router decides vision support by matching the model name against patterns. `muse-spark` matches a vision-capable pattern (1M context). Plain `muse` does not, and images get stripped with "[image omitted: model has no vision support]".
3. **Combo**: name `muse`, models `["musevm/muse-spark"]`. This is the public model name OpenCode will use.
4. **API key**: create one for your laptop (for example named `opencode-laptop`). Keep dashboard login and API key requirements enabled.

### 4. Add the Muse worker

The worker is a Muse scheduled task or hook that wakes when jobs are pending, pulls them from the bridge, and answers. Use the prompt in [`worker-prompt.md`](worker-prompt.md) as-is, adjusting only the key file path.

Two rules that make or break the setup:

- The worker must answer workspace questions with `tool_calls` (OpenCode executes them on your laptop), never by reading files inside the VM.
- The worker reads its API key from a 600-permission file inside its shell commands only. The key value must never appear in chat, logs, or the repo.

A 5-second queue poll (hook) plus the worker prompt is enough. A 1-minute scheduled fallback is optional; it burns tokens while idle.

### 5. Expose 9Router with a stable tunnel

Free localhost.run domains rotate. A registered SSH key binds a stable subdomain:

1. Create a free account at [admin.localhost.run](https://admin.localhost.run) and upload the VM's SSH public key (`~/.ssh/id_ed25519.pub`).
2. Run the tunnel with key auth (no `nokey` user):

```bash
ssh -i ~/.ssh/id_ed25519 -o IdentitiesOnly=yes -o ServerAliveInterval=30 \
  -R 80:localhost:20128 localhost.run
```

Run it as a systemd service (`muse-tunnel`) with `Restart=always`. The printed URL (for example `https://<subdomain>.app.lhr.life`) stays the same across reconnects.

> If your VM can only reach the internet through an HTTP CONNECT proxy, add a `ProxyCommand` that tunnels SSH through the proxy. Direct egress works in most setups.

### 6. Configure OpenCode on your laptop

Install OpenCode:

```bash
npm install -g opencode-ai
```

Copy [`examples/opencode.json`](examples/opencode.json) to `~/.config/opencode/opencode.json` (Windows: `C:\Users\<username>\.config\opencode\opencode.json`) and replace the two placeholders:

- `<YOUR-TUNNEL-URL>`: your stable localhost.run URL from step 5.
- `<YOUR-9ROUTER-API-KEY>`: the API key from step 3.

Test the endpoint before opening OpenCode:

```bash
curl <YOUR-TUNNEL-URL>/v1/chat/completions \
  -H "Authorization: Bearer <YOUR-9ROUTER-API-KEY>" \
  -H "Content-Type: application/json" \
  -d '{"model":"muse","stream":false,"messages":[{"role":"user","content":"Say hello in one word."}]}'
```

Then run `opencode`, pick model `muse-vm/muse`, and ask it to read a file in your workspace.

## Usage notes

- **Tool calls run on your laptop.** OpenCode's permission system is the gate. The example config auto-allows read-only tools (`read`, `glob`, `grep`, `webfetch`); edits and shell commands still ask.
- **Web questions** are searched by the worker from the VM. OpenCode's own `websearch` tool needs a third-party (Exa) key and is intentionally not used.
- **Images** work through the `muse-spark` model name. Attach a screenshot and ask about it.
- **Subagents and MCP tools** pass through as normal tool calls. A subagent runs on your laptop and calls back through the same bridge, so it works but adds queue latency.

## Limitations

Honest limits of this design:

- **Latency**: every step costs one worker wake, roughly 15 to 40 seconds. Batching (up to 8 tool calls per step) keeps multi-file tasks practical.
- **No token streaming**: reasoning and answers arrive per block, not token by token. Progress notes keep the UI informed while you wait.
- **One worker**: parallel OpenCode sessions queue behind each other.
- **Token cost**: each job is a Muse agent run in your VM. Long sessions cost accordingly.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `401` from the tunnel URL | Wrong or missing 9Router API key in `opencode.json`. |
| Connection error after VM restart | Tunnel down or URL rotated. Use key auth (step 5) for a stable URL; check `systemctl status muse-tunnel`. |
| "Missing API key" in OpenCode | The key must be embedded in `opencode.json` (as in the example) or exported in the same shell that launches OpenCode. |
| Model only replies with text, never reads files | The worker is not returning `tool_calls`. Check the worker prompt (step 4) and that requests include `tools`. |
| "[image omitted: model has no vision support]" | Upstream model name does not match a vision pattern in 9Router. Use `muse-spark` (step 3). |
| `429 Muse bridge busy` | More than 10 jobs pending. Wait for the worker to drain the queue. |
| Answers time out on long tasks | The bridge waits up to 600s per request. Split very long tasks into smaller asks. |

## Security

- Never commit keys. `keys.json`, env files, and `opencode.json` with a real key stay out of git (600 permissions).
- The tunnel exposes the 9Router login page. Use a strong dashboard password and keep API key requirements on.
- The bridge itself only listens on `127.0.0.1`; only 9Router is tunneled.
- Worker keys and user keys are separate roles. Rotate with `bridge.py keydel` + `keygen` if one leaks.

## Credits

- Based on [Kutuyyy/Muse-on-9Router-Hermes-Agent-ai](https://github.com/Kutuyyy/Muse-on-9Router-Hermes-Agent-ai) (`bridge.py` v5.1), extended here with OpenAI tool calling, progress and reasoning streaming, token usage estimates, and higher limits.
- [9Router](https://github.com/decolua/9router) as the API gateway.
- [OpenCode](https://opencode.ai) as the terminal agent.
- [localhost.run](https://localhost.run) for the stable free tunnel.

## License

MIT. See [LICENSE](LICENSE).
