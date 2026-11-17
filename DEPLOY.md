# Deploying vault-search

Runbook for the agent that deploys this service on the VPS (Josh's Claude). Vault Keeper owns the design and the code in this repository; you pull a release and run it. You do not change product behaviour here.

The MCP HTTP server, the search index, and the pull-only vault sync are not in milestone 1. Steps that need them are marked **available from milestone N**. You can still do the prerequisite checks, create the service user and directories, and clone this public repo now.

The public endpoint is shaped like:

```text
https://<vps>.<tailnet>.ts.net/vault/mcp
```

Those angle-bracket names are placeholders. Never substitute real host, tailnet, or address values into this repository.

**Acceptance.** Every step below ends with checkable completion criteria (exact commands and expected results). Your report is necessary but not sufficient: Vault Keeper independently re-runs the externally visible checks from the shared Grok Bot box (the verification node) with `scripts/verify-deploy`, and the on-box checks over Tailscale SSH, before calling a milestone done.

## Safety rules

Follow these for every step. If a step would violate one, stop and ask Josh.

1. **Never commit host names, tailnet names, MagicDNS names, or IP addresses** to `vault-search` (or any public git remote). Fill those in only in files on the VPS (`/etc/vault-search/env`, and the nginx config in its own on-box git). If Vault Keeper needs inventory, send it privately to Josh, not as a pull request.
2. **Never print secrets.** That includes the vault deploy private key, `.env` values, tokens, and audit-log lines that might contain a secret. When a command would display a secret, show only a redacted confirmation (for example "deploy key written, mode 0600") or the **public** key.
3. **Ask Josh before any firewall or ACL change.** This includes `ufw`/`iptables`/`nftables`, cloud firewall rules, Tailscale ACL / `tagOwners` / auth-key changes, opening a port, and enabling Funnel. Do not make those changes yourself.
4. **Do not enable Tailscale Funnel** on this node. nginx must listen only on the tailnet MagicDNS name, never on the public IP.
5. **Bind the app to loopback only** (`LISTEN_HOST=127.0.0.1`). Agents connect over HTTPS to nginx, not with SSH tunnels.
6. **Admin access is Tailscale SSH**, governed by Tailscale ACLs. Do not add SSH keys as the way to administer the VPS.
7. **The vault deploy private key never leaves the VPS.** Generate it on the box. Hand only the public key to Josh to add as a read-only deploy key. Do not email, chat, or commit the private key.
8. **Do not copy vault notes into this repo**, into gist, or into a chat transcript. The private vault checkout stays on the box.
9. **Do not put the nginx config in this repository.** It already lives in a separate git on the VPS. Add the sample `location` from this runbook to that config; do not invent a second source of truth here.

## Ownership split

| Who | Does |
| --- | --- |
| Vault Keeper | Design, code, tests, and releases in `JoshTeperman/vault-search`. Independently verifies completion criteria. |
| You (Claude on the VPS) | Pull, configure, deploy, verify, update, roll back. Maintain the nginx config in its on-box git. Report status with the [Report back template](#11-report-back-template). |
| Josh | Adds the read-only deploy key, and approves any firewall or ACL change. |

## 1. Prerequisites and checks

Do these first, every time you land on the box. RAM and Python are the first checks: stop and report if either fails.

- **RAM:** `free -m` shows at least 2048 MB total memory (4096 MB is comfortable). Indexing with embeddings peaked at about 1.3 GB RSS. If the box has less than 2 GB, stop and report to Josh; do not add swap or resize the server yourself.
- You reached the VPS with **Tailscale SSH** (not a public SSH port you opened, and not an SSH tunnel for agents).
- `python3 --version` is 3.11 or later; `git`, `systemctl`, `curl`, `nginx`, and `tailscale` are on `PATH`.
- `tailscale status` shows this node is online, with MagicDNS and tailnet HTTPS available. Do not paste the node or tailnet name into git.
- Funnel is off:

  ```bash
  tailscale funnel status
  ```

  If Funnel is on, **do not turn it off or on yourself** if that might be an ACL/policy change you were not asked to make — stop and ask Josh. The expected state is off.
- Confirm the node has a tailnet IPv4 address (`tailscale ip -4`) and a public IPv4 address you can use later for the "not reachable on the public IP" check. Write those down only on the box, never in this repo.
- This public repo is clonable over HTTPS (no token required): `https://github.com/JoshTeperman/vault-search.git`.
- **Host firewall:** port 443 must be allowed only on the `tailscale0` interface (or only from `100.64.0.0/10`), never on the public interface (e.g. `eth0`). Josh makes firewall changes; check the current rules read-only (`sudo ufw status verbose` or `sudo nft list ruleset`) and report what you see.
- nginx is already the reverse proxy on this VPS; its config is maintained in Claude's on-box git, not in `vault-search`.

### Completion criteria

```bash
awk '/MemTotal/ {exit !($2 >= 2000000)}' /proc/meminfo && echo RAM_OK || echo RAM_TOO_SMALL
python3 -c 'import sys; assert sys.version_info >= (3, 11), sys.version'
command -v git systemctl curl nginx tailscale
tailscale status --json | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d.get("BackendState")=="Running", d.get("BackendState")'
tailscale funnel status
```

Expected:

- `RAM_OK` (at least 2 GB total memory). `RAM_TOO_SMALL` means stop and report.
- Python is 3.11 or later; all five commands are on `PATH`.
- Firewall rules (read-only check) allow 443 only on `tailscale0` or from `100.64.0.0/10`, never on the public interface. If not, report to Josh; do not change them.
- Backend state is `Running`. Do not paste the node or tailnet name into git or into this repo.
- `tailscale funnel status` reports Funnel off (no Funnel endpoints advertised). If it is on, stop and ask Josh.

## 2. Service user and directories

Create a dedicated user and the on-box layout. Suggested paths (adjust if the box already has a convention, and do not commit the final host-specific layout):

```text
/var/lib/vault-search/app      # clone of this public repo
/var/lib/vault-search/vault    # pull-only clone of the private vault
/var/lib/vault-search/index    # local search index (gitignored in this repo)
/var/lib/vault-search/models   # local embedding model files
/var/lib/vault-search/ssh      # read-only vault deploy key (mode 0700 / 0600)
/var/log/vault-search          # audit log
/etc/vault-search/env          # environment file, mode 0640, owned by root:vault-search
```

```bash
sudo useradd --system --home /var/lib/vault-search --shell /usr/sbin/nologin vault-search
sudo usermod -aG tailscale vault-search   # so the service can call WhoIs on the local tailscaled socket
sudo mkdir -p /var/lib/vault-search/{app,vault,index,models,ssh} /var/log/vault-search /etc/vault-search
sudo chown -R vault-search:vault-search /var/lib/vault-search /var/log/vault-search
sudo chmod 0700 /var/lib/vault-search/ssh
sudo chmod 0750 /etc/vault-search
```

Adding the user to the `tailscale` group is so the process can read `/var/run/tailscale/tailscaled.sock` (or `/run/tailscale/tailscaled.sock`) for WhoIs. If the socket's group is different on this box, stop and ask Josh rather than loosening permissions.

### Completion criteria

```bash
id vault-search
groups vault-search
stat -c '%A %U:%G %n' /var/lib/vault-search /var/lib/vault-search/ssh /var/log/vault-search /etc/vault-search
```

Expected:

- User `vault-search` exists (`id` succeeds) and is in the `tailscale` group.
- `/var/lib/vault-search` and `/var/log/vault-search` are owned by `vault-search:vault-search`.
- `/var/lib/vault-search/ssh` is mode `0700`.
- `/etc/vault-search` is mode `0750`.

## 3. Clone vault-search

```bash
sudo -u vault-search git clone https://github.com/JoshTeperman/vault-search.git /var/lib/vault-search/app
sudo -u vault-search git -C /var/lib/vault-search/app rev-parse HEAD   # record this SHA in your report
```

Create a virtualenv under the app directory (it is gitignored) and install the package (code for the indexer, hybrid search, and MCP server is in this repo as of milestones 4–6). Use the same Python 3.11+ the box already has (`python3 --version`).

```bash
sudo -u vault-search python3 -m venv /var/lib/vault-search/app/.venv
sudo -u vault-search /var/lib/vault-search/app/.venv/bin/pip install -U pip
sudo -u vault-search /var/lib/vault-search/app/.venv/bin/pip install -e "/var/lib/vault-search/app[semantic]"
```

`[semantic]` pulls `fastembed` (ONNX, CPU) for `BAAI/bge-small-en-v1.5`. Without it the server still starts and search falls back to full-text. The first index with embeddings downloads the model into `EMBEDDING_CACHE_DIR` (or the library default under the service user's home). A local smoke on a 69-page / 331-chunk vault peaked at about 1.3 GB RSS while embedding, with a ~65 MB model cache, a ~2–3 MB SQLite index, and a ~300 MB venv. Plan for **2 GB RAM at a minimum** and **4 GB comfortably** once nginx, tailscaled, and the OS are counted. A 1 GB box is not enough. Disk needs of the app itself are modest (under 500 MB). The official `mcp` SDK is a core install dependency; Python 3.11 or later is required (3.13 is what CI uses).

### Completion criteria

```bash
sudo -u vault-search git -C /var/lib/vault-search/app remote get-url origin
sudo -u vault-search git -C /var/lib/vault-search/app status --porcelain
sudo -u vault-search git -C /var/lib/vault-search/app rev-parse HEAD
sudo -u vault-search git -C /var/lib/vault-search/app rev-parse origin/main
test -x /var/lib/vault-search/app/.venv/bin/python
```

Expected:

- `origin` is `https://github.com/JoshTeperman/vault-search.git` (HTTPS, no token in the URL).
- The working tree is clean (`status --porcelain` is empty).
- `HEAD` equals `origin/main`. Record that SHA in the report.
- After the venv step, `/var/lib/vault-search/app/.venv/bin/python` is executable.

## 4. Pull-only clone of the private vault

**Available from milestone 3.** Until then, skip this section.

The vault stays in the private repo `JoshTeperman/knowledge-vault`. The VPS gets a pull-only clone, authenticated with a **read-only** deploy key generated on this box.

1. Generate the key as the service user. Do not set a passphrase (the timer needs to pull non-interactively). Do not print the private key.

   ```bash
   sudo -u vault-search ssh-keygen -t ed25519 -N "" \
     -C "vault-search-readonly" \
     -f /var/lib/vault-search/ssh/vault_deploy
   sudo chmod 0600 /var/lib/vault-search/ssh/vault_deploy
   sudo chmod 0644 /var/lib/vault-search/ssh/vault_deploy.pub
   ```

2. Show **only** the public key to Josh and ask him to add it as a read-only deploy key on `JoshTeperman/knowledge-vault`. Wait until he confirms.

   ```bash
   sudo -u vault-search cat /var/lib/vault-search/ssh/vault_deploy.pub
   ```

3. Once the key is in place, clone over SSH using a dedicated config that points at this key only:

   ```bash
   sudo -u vault-search tee /var/lib/vault-search/ssh/config >/dev/null <<'EOF'
   Host github-vault
     HostName github.com
     User git
     IdentityFile /var/lib/vault-search/ssh/vault_deploy
     IdentitiesOnly yes
     StrictHostKeyChecking accept-new
   EOF
   sudo chmod 0600 /var/lib/vault-search/ssh/config
   sudo -u vault-search git clone git@github-vault:JoshTeperman/knowledge-vault.git /var/lib/vault-search/vault
   ```

   Put `GIT_SSH_COMMAND="ssh -F /var/lib/vault-search/ssh/config"` in the environment for clone, pull, and the timer.

4. Disable push on that clone:

   ```bash
   sudo -u vault-search git -C /var/lib/vault-search/vault remote set-url --push origin DISABLED
   ```

   Confirm a test `git push` is rejected. Confirm the private key is not in the vault working tree, not in the `vault-search` working tree, and not in the public repo.

A systemd timer that pulls `main` belongs to milestone 3; do not invent a cron job that writes vault content into this repo.

### Completion criteria

**Available from milestone 3.**

```bash
stat -c '%a %n' /var/lib/vault-search/ssh/vault_deploy /var/lib/vault-search/ssh/vault_deploy.pub
sudo -u vault-search git -C /var/lib/vault-search/vault rev-parse HEAD
sudo -u vault-search git -C /var/lib/vault-search/vault remote get-url --push origin
sudo -u vault-search env GIT_SSH_COMMAND='ssh -F /var/lib/vault-search/ssh/config' \
  git -C /var/lib/vault-search/vault push origin HEAD
# The private key must not appear in either working tree:
sudo -u vault-search git -C /var/lib/vault-search/vault ls-files | grep -E 'vault_deploy$|id_ed25519' && echo 'FAIL: key in vault tree' || true
sudo -u vault-search git -C /var/lib/vault-search/app ls-files | grep -E 'vault_deploy$|id_ed25519' && echo 'FAIL: key in app tree' || true
```

Expected:

- Private key mode is `600`; public key is readable.
- `HEAD` is a real commit SHA (hash only in the report; no vault file contents).
- `remote get-url --push origin` is `DISABLED`.
- `git push` is rejected (non-zero exit; no objects sent).
- Neither `grep` prints `FAIL`. Never paste the private key.

## 5. Environment configuration

Copy `.env.example` from the clone and fill it in **on the box** as `/etc/vault-search/env`. Never commit the filled file.

```bash
sudo cp /var/lib/vault-search/app/.env.example /etc/vault-search/env
sudo chown root:vault-search /etc/vault-search/env
sudo chmod 0640 /etc/vault-search/env
sudo -e /etc/vault-search/env
```

Suggested production values (adjust paths if you used a different layout):

| Variable | Production value |
| --- | --- |
| `VAULT_ROOT` | `/var/lib/vault-search/vault` (after milestone 3) |
| `INDEX_PATH` | `/var/lib/vault-search/index/vault.db` (after milestone 4) |
| `EMBEDDING_MODEL` | `BAAI/bge-small-en-v1.5` |
| `EMBEDDING_CACHE_DIR` | `/var/lib/vault-search/models` (gitignored; created on first embed) |
| `EMBEDDING_MODEL_PATH` | leave empty to let fastembed use the cache dir, or point at a pre-fetched model directory |
| `AUDIT_LOG_PATH` | `/var/log/vault-search/audit.jsonl` |
| `DENYLIST` | optional comma-separated device names, tags, or logins |
| `DENYLIST_PATH` | `/etc/vault-search/denylist` (create an empty file if unused) |
| `LISTEN_HOST` | `127.0.0.1` (required) |
| `LISTEN_PORT` | `8787` (or another free loopback port) |
| `MCP_PATH` | `/vault/mcp` |
| `LISTEN_ALLOW_NONLOCAL` | leave unset (the process refuses `0.0.0.0` / `::` without it) |
| `PUBLIC_BASE_URL` | `https://<vps>.<tailnet>.ts.net` — use the real MagicDNS name **only** in this on-box file |
| `IDENTITY_SOURCE` | `whois` (required under nginx; see [Caller identity](#8-caller-identity-audit-log-and-denylist)) |
| `TAILSCALED_SOCKET` | `/var/run/tailscale/tailscaled.sock` (use `/run/tailscale/tailscaled.sock` if that is what the box has) |

`LISTEN_HOST` must stay loopback. Do not set it to `0.0.0.0`, to a public IP, or to the tailnet IP; nginx is what listens on the tailnet. The process refuses a wildcard bind unless `LISTEN_ALLOW_NONLOCAL=1`.

### Completion criteria

```bash
stat -c '%a %U:%G %n' /etc/vault-search/env
sudo grep -E '^(LISTEN_HOST|LISTEN_PORT|MCP_PATH|IDENTITY_SOURCE)=' /etc/vault-search/env
```

Expected:

- File mode is `640`, owner `root:vault-search`.
- `LISTEN_HOST=127.0.0.1`
- `LISTEN_PORT` is a port (default `8787`)
- `MCP_PATH=/vault/mcp`
- `IDENTITY_SOURCE=whois`
- Do not paste other env values (paths, `PUBLIC_BASE_URL`) into git.

## 6. systemd unit (loopback only)

The MCP server lives in this repo (`python -m vault_search.mcp`). You may install the unit file at any time, but do not enable it until `LISTEN_HOST` is `127.0.0.1`, the vault checkout exists, and the index has been built at least once (`python -m vault_search index`). Enabling the unit on the VPS is still blocked on VPS access.

Write `/etc/systemd/system/vault-search.service`:

```ini
[Unit]
Description=vault-search MCP service
After=network-online.target tailscaled.service
Wants=network-online.target
Requires=tailscaled.service

[Service]
Type=exec
User=vault-search
Group=vault-search
SupplementaryGroups=tailscale
EnvironmentFile=/etc/vault-search/env
WorkingDirectory=/var/lib/vault-search/app
ExecStart=/var/lib/vault-search/app/.venv/bin/python -m vault_search.mcp
Restart=on-failure
RestartSec=5
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=/var/lib/vault-search /var/log/vault-search

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now vault-search
sudo systemctl status vault-search
```

### Sync timer (`git pull --ff-only` plus reindex)

Write `/etc/systemd/system/vault-search-sync.service` (a oneshot, not a long-running daemon):

```ini
[Unit]
Description=vault-search pull-only vault sync and reindex
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
Type=oneshot
User=vault-search
Group=vault-search
EnvironmentFile=/etc/vault-search/env
WorkingDirectory=/var/lib/vault-search/app
ExecStart=/var/lib/vault-search/app/.venv/bin/python -m vault_search sync
Nice=10
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=/var/lib/vault-search /var/log/vault-search
```

And `/etc/systemd/system/vault-search-sync.timer`:

```ini
[Unit]
Description=Periodic vault-search sync

[Timer]
OnBootSec=2m
OnUnitActiveSec=10m
AccuracySec=1m
Persistent=true
Unit=vault-search-sync.service

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now vault-search-sync.timer
systemctl list-timers vault-search-sync.timer
```

`python -m vault_search sync` runs `git pull --ff-only` in `VAULT_ROOT` and then reindexes (embeddings are written only for new or changed chunks when the semantic extra is installed). It refuses to run if `VAULT_ROOT` is not itself a git checkout. A failed non-fast-forward pull fails the unit and does not rewrite the index.

### Completion criteria

**Available from milestone 6.**

```bash
systemctl is-enabled vault-search
systemctl is-active vault-search
ss -tlnp | grep -E ':8787\b'
```

Expected:

- `is-enabled` prints `enabled`.
- `is-active` prints `active`.
- `ss` shows `127.0.0.1:8787` (or the configured `LISTEN_PORT`) and does **not** show `0.0.0.0:8787`, `*:8787`, or a public or tailnet address for this process.

## 7. nginx in front of the app

**Available from milestone 6.** This VPS already runs nginx. Its config lives on the VPS, in Claude's own git — not in `vault-search`. You add (and maintain) a site and a `location /vault/mcp` there.

One HTTPS server listens **only** on the node's tailnet MagicDNS name, never on the public IP and never on `0.0.0.0`. Each service on the box gets its own path or subdomain. This service uses `/vault/mcp`. Agents call `https://<vps>.<tailnet>.ts.net/vault/mcp` over HTTPS, not an SSH tunnel.

TLS certificates come from Tailscale, not from a public CA:

```bash
# Run on the VPS. Writes a cert and key for the MagicDNS name.
# Put the files where the on-box nginx config expects them; do not commit them here.
tailscale cert <vps>.<tailnet>.ts.net
```

Sample server and location snippet for the on-box nginx config. Fill in the placeholders on the box only. `proxy_pass` has no trailing slash, so the `/vault/mcp` prefix is forwarded to the app (which uses `MCP_PATH=/vault/mcp`).

```nginx
server {
    listen <tailnet-ipv4>:443 ssl;
    # Optional: listen [<tailnet-ipv6>]:443 ssl;
    # Do not listen on 0.0.0.0, *, or the public IP.
    server_name <vps>.<tailnet>.ts.net;

    ssl_certificate     /path/on/box/<vps>.<tailnet>.ts.net.crt;
    ssl_certificate_key /path/on/box/<vps>.<tailnet>.ts.net.key;

    location /vault/mcp {
        proxy_pass http://127.0.0.1:8787;
        proxy_http_version 1.1;
        proxy_buffering off;
        proxy_request_buffering off;
        proxy_read_timeout 1h;
        proxy_send_timeout 1h;
        # Streamable HTTP is a long-lived HTTP/1.1 response, not WebSocket.
        proxy_set_header Connection "";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        # Set from $remote_addr only. Do not use $proxy_add_x_forwarded_for
        # (that would honour a client-supplied value).
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

After editing the on-box config (in its own git):

```bash
sudo nginx -t && sudo systemctl reload nginx
```

nginx does not inject Tailscale identity headers. The app must resolve the caller with a WhoIs lookup on the client IP taken from `X-Real-IP` or `X-Forwarded-For`, and it must trust those headers **only** when the immediate TCP peer is localhost. See [Caller identity](#8-caller-identity-audit-log-and-denylist).

Fallback only: if nginx were unavailable, Tailscale Serve could path-mount the same loopback app (`tailscale serve --bg --set-path=/vault/mcp http://127.0.0.1:8787/vault/mcp`). This VPS uses nginx; do not enable Serve (or Funnel) on port 443 alongside nginx without Josh's approval. Serve identity headers are blank for tagged nodes anyway, so WhoIs would still be required.

### Completion criteria

**Available from milestone 6.**

```bash
sudo nginx -t
systemctl is-active nginx
ss -tlnp
tailscale funnel status
```

Expected:

- `nginx -t` prints `syntax is ok` and `test is successful`.
- `systemctl is-active nginx` prints `active`.
- `ss -tlnp` shows this stack only as: the app on `127.0.0.1:8787`, and nginx on `<tailnet-ip>:443`. Nothing in this stack listens on the public IP or on `0.0.0.0`/`*`.
- `tailscale funnel status` is still off.
- From another tailnet node, the endpoint in section 9 answers. From off the tailnet, a curl to the public IP fails.

## 8. Caller identity, audit log, and denylist

The service records caller identity on every tool call and rejects callers who are not on the tailnet, or who are on the service denylist. WhoIs identifies the **device**. **nginx does not inject Tailscale identity.** Tagged nodes are the later per-agent path; they are not how the shared Grok Bot box appears today.

**`IDENTITY_SOURCE=whois`.** The app reads `X-Real-IP` or `X-Forwarded-For` (set by nginx from `$remote_addr`) only when the immediate TCP peer is `127.0.0.1`. It then looks up that client address on the local tailscaled socket:

```bash
curl --unix-socket /var/run/tailscale/tailscaled.sock \
  "http://local-tailscaled.sock/localapi/v0/whois?addr=<client-ip>:<client-port>"
```

The response includes node name and tags, and a user profile when the node is not tagged. For tagged nodes, the login field is not a useful user identity (it is the tagged-devices profile); log the **node name and tags** instead. Do not paste WhoIs output that contains real names into git.

**Identity v1 (as of 17 Nov 2026).** The shared Grok Bot box used by all of Josh's agents is on the tailnet through the Tailscale connector. It is a single **untagged** device carrying Josh's identity, meant for **read and verify only**, and it can reach tailnet hosts over HTTP(S)/TCP. WhoIs cannot tell agents on that box apart. Identity v1 is the device, plus a self-reported `X-Vault-Agent-Name` header. The audit log records the header when present and marks it **unverified**. The header is not used for authorisation or revocation. **Revocation is per device** (overlay ACL, device removal, or the service denylist). True per-agent identity comes later, from separate tagged devices or MCP OAuth through the gateway. This same box is Vault Keeper's verification node for the criteria in [End-to-end verification](#9-end-to-end-verification). Do not write real host or tailnet names in this repo.

The service user must be able to read that socket (see [Service user and directories](#2-service-user-and-directories)). If a request has no trusted client IP, or WhoIs does not recognise it as a tailnet node, reject the call.

If a request carries `Tailscale-Funnel-Request`, reject it. Funnel is supposed to be off; this is defence in depth.

The audit log (`AUDIT_LOG_PATH`) is append-only on the box. Each line should include timestamp, caller (node, tags, user if any), tool, arguments, outcome, duration, and whether a secret-like span was masked (rule, path, span — not the secret). Do not log secret values, vault body text, or values you would not want copied off the box. Do not commit the log.

### Completion criteria

**Available from milestone 6** (after a successful call from section 9).

```bash
# On the VPS, after the tailnet curl / verify-deploy run:
sudo tail -n 20 /var/log/vault-search/audit.log
```

Expected:

- A new line exists for the `initialize` or `tools/list` (and, if run, the fixture `read_note`/`search`) call.
- The line includes a caller **device** identity (node name and tags, or a user login if the caller was not tagged). If `X-Vault-Agent-Name` was sent, the line includes it and marks it **unverified**.
- The line does **not** contain secret values or vault body text. In the report, quote only outcome and whether identity was present — not a raw log dump, not WhoIs JSON, not real host names.

## 9. End-to-end verification

**Available from milestone 6**, except Funnel and bind checks, which you can run as soon as nginx and the unit exist.

Run the on-box commands on the VPS. Run the HTTP commands (or `scripts/verify-deploy`) from a **different** node on the tailnet, and the public-IP curl from a host that is **not** on the tailnet. Do not commit commands with real names filled in.

Claude's passing output is not acceptance. Vault Keeper re-runs these checks from the shared Grok Bot box (the verification node; untagged, Josh's identity, read/verify only — see [Caller identity](#8-caller-identity-audit-log-and-denylist)) with `scripts/verify-deploy` (externally visible criteria) and the on-box commands over Tailscale SSH.

1. **systemd is active** (on the VPS):

   ```bash
   systemctl is-active vault-search
   # expected: active
   ```

2. **MCP endpoint answers on the tailnet.** From another tailnet node, `initialize` then `tools/list` must succeed, and the tool list must be `search`, `read_note`, `list_notes`, `list_projects`. The helper script does this for you:

   ```bash
   # On a tailnet-connected machine, with real values only in the shell:
   export PUBLIC_BASE_URL='https://<vps>.<tailnet>.ts.net'
   export MCP_PATH=/vault/mcp
   python3 /path/to/vault-search/scripts/verify-deploy
   ```

   Manual equivalent (Streamable HTTP; capture any `Mcp-Session-Id` from initialize and send it back on `tools/list`):

   ```bash
   curl -fsS https://<vps>.<tailnet>.ts.net/vault/mcp \
     -H 'Content-Type: application/json' \
     -H 'Accept: application/json, text/event-stream' \
     -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2026-07-28","capabilities":{},"clientInfo":{"name":"vault-search-verify","version":"0.1.0"}}}'
   curl -fsS https://<vps>.<tailnet>.ts.net/vault/mcp \
     -H 'Content-Type: application/json' \
     -H 'Accept: application/json, text/event-stream' \
     -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'
   ```

   Expected: a JSON-RPC result (or SSE `data:` payload) whose `tools` names are exactly those four tools. A connection failure or an empty/error result is a fail.

3. **A sample query returns masked output for a synthetic secret fixture.** Create the fixture on the VPS at verify time only: a temporary note the index can see, containing a token **assembled at runtime** (not a committed secret, not a real credential). Do not commit the fixture to the vault or to this repo. Then call `read_note` or `search` on that path (the verify script does this when `VERIFY_FIXTURE_PATH` and `VERIFY_SYNTHETIC_SECRET` are set in the shell). Expected: the response contains `[REDACTED:` and does **not** contain the raw token. Remove the fixture afterwards.

4. **Nothing in this stack listens on the public interface** (on the VPS):

   ```bash
   ss -tlnp
   ```

   Expected: vault-search on `127.0.0.1` only; nginx on the tailnet IP port 443 only; neither on the public IP nor on `0.0.0.0`/`*`.

   From a host off the tailnet (also run by `scripts/verify-deploy` when `PUBLIC_IP` is set):

   ```bash
   curl -fsS --connect-timeout 5 https://<public-ip>/vault/mcp
   ```

   Expected: failure (timeout, connection refused, or no TLS service). An HTTP 200 or an MCP response is a fail. If this succeeds, stop and tell Josh — do not try to "fix" the firewall yourself beyond reverting a change you just made in this session.

5. **Funnel is off** (on the VPS):

   ```bash
   tailscale funnel status
   # expected: off (no Funnel endpoints)
   ```

6. **The audit log records the caller identity** — see [section 8 completion criteria](#completion-criteria-2).

7. **Denylist** (once a test identity is available): adding a caller to `DENYLIST_PATH` and retrying should be rejected and audit-logged. Remove the test entry afterwards.

### Completion criteria

**Available from milestone 6.** This section is done when every item above has the expected result, **and** Vault Keeper has independently obtained the same result with `scripts/verify-deploy` plus the on-box checks. A report from Claude alone does not close the milestone.

## 10. Update and rollback

**Update** (after milestone 6; for a code-only pull you can already update the clone):

```bash
sudo -u vault-search git -C /var/lib/vault-search/app fetch origin
sudo -u vault-search git -C /var/lib/vault-search/app rev-parse HEAD   # previous SHA, for rollback
sudo -u vault-search git -C /var/lib/vault-search/app checkout main
sudo -u vault-search git -C /var/lib/vault-search/app pull --ff-only origin main
sudo -u vault-search /var/lib/vault-search/app/.venv/bin/pip install -e /var/lib/vault-search/app
sudo systemctl restart vault-search    # only once the unit is enabled
```

Re-run [section 9](#9-end-to-end-verification). If the unit is not yet enabled (pre-milestone 6), skip the restart.

Vault content updates are a pull of `knowledge-vault` plus an index rebuild (**milestones 3–5**), not a deploy of this repo.

**Rollback** of this service: check out the previous `vault-search` SHA, reinstall the package if needed, restart the unit, and re-run section 9. Do not `git push --force` to origin. Do not change Tailscale ACLs as part of a rollback. Do not change nginx listen addresses as part of a rollback without Josh if that would touch firewall or interface binding.

If a release needs a new environment variable, add it to the on-box env file first, then update.

### Completion criteria

```bash
sudo -u vault-search git -C /var/lib/vault-search/app rev-parse HEAD
sudo -u vault-search git -C /var/lib/vault-search/app rev-parse origin/main
systemctl is-active vault-search    # after milestone 6
```

Expected:

- For an update: `HEAD` equals `origin/main` and equals the intended release SHA; the previous SHA was recorded first.
- For a rollback: `HEAD` equals the recorded previous SHA.
- After milestone 6, `systemctl is-active vault-search` is `active`, and section 9 still passes (including Vault Keeper's independent `scripts/verify-deploy` run).

## 11. Report back template

Send the filled template privately (not as a commit to this repo). Do not include host names, tailnet names, IP addresses, `.env` values, private keys, tokens, vault note content, or raw audit/WhoIs dumps. If inventory names are needed, send them to Josh privately.

This report is **not** acceptance. Vault Keeper re-runs the completion criteria from the shared Grok Bot box, which is the verification node (`scripts/verify-deploy` for the externally visible checks, plus the on-box commands over Tailscale SSH), before calling the milestone done.

```text
## Deploy report (vault-search)

Date (Australia/Melbourne, UTC+11):
vault-search SHA:
knowledge-vault SHA (milestone 3+; hash only, otherwise "skipped"):
Previous SHA (for an update/rollback, otherwise "n/a"):

systemd is-enabled / is-active (paste the two words only):
nginx -t (ok / FAIL):
nginx is-active:
ss -tlnp summary (app on 127.0.0.1? nginx on tailnet :443 only? nothing on public IP or 0.0.0.0? yes/no, no addresses):
Funnel (expected: off):

MCP initialize / tools/list (pass/fail; list tool names only):
Masked fixture query (pass/fail; say whether [REDACTED:] appeared; do not paste the secret):
Public IP curl (failed as expected / UNEXPECTED SUCCESS):
Audit log: caller identity present (yes/no; node/tags or user; no log dump):

Deploy key public half (first time only; never the private key):
Blockers / questions for Josh (ask before any firewall or ACL change):

I have not treated this report as acceptance. Please re-run scripts/verify-deploy and the on-box checks.
```
