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
4. **Do not enable Tailscale Funnel** on this node. Only `tailscale serve` (tailnet-only) answers on the tailnet; nginx and the app listen on loopback only, never on the public IP.
5. **Bind the app to loopback only** (`LISTEN_HOST=127.0.0.1`). Agents connect over HTTPS to nginx, not with SSH tunnels.
6. **Admin access is Tailscale SSH**, governed by Tailscale ACLs. Do not add SSH keys as the way to administer the VPS.
7. **The vault deploy private key never leaves the VPS.** Generate it on the box. Hand only the public key to Josh to add as a read-only deploy key. Do not email, chat, or commit the private key.
8. **Do not copy vault notes into this repo**, into gist, or into a chat transcript. The private vault checkout stays on the box.
9. **Do not put the nginx config in this repository.** It already lives in a separate git on the VPS. Add the sample `location` from this runbook to that config; do not invent a second source of truth here.

## Decisions (19 Nov 2026, approved by Josh)

These replace earlier parts of this runbook and the design. Vault Keeper: please keep them unless Josh says otherwise.

- **Architecture follows the existing VPS proxy.** `tailscale serve` terminates TLS on the tailnet at :443 with the `*.ts.net` certificate and forwards to nginx on `127.0.0.1:8080`, which routes `/vault/mcp` to the app on `127.0.0.1:8787`. There is no `tailscale cert` step and no nginx listener on the tailnet IP.
- **Access control is the tailnet, not the app.** Any tailnet device may call the service. The WhoIs lookup, the denylist, `IDENTITY_SOURCE` and `TAILSCALED_SOCKET` were removed as not needed yet: there is no requirement to restrict callers, and the lookup made every call fail whenever tailscaled was unreachable. If per-device restriction is needed later, put the service on its own tailnet port and restrict it with a Tailscale grant. The app still rejects requests carrying `Tailscale-Funnel-Request`.
- **The audit log records the caller's tailnet IP** (map it to a device with `tailscale status` when needed) and the unverified `X-Vault-Agent-Name`. Result counts and `zero_hits` are unchanged.
- **The proxied Host is allowed explicitly.** The MCP SDK rejects non-localhost `Host` headers on a loopback bind (421). The app now also accepts the host in `PUBLIC_BASE_URL`, so that variable is required in production.
- **No host firewall.** `ufw` is inactive by choice. Isolation comes from nothing listening on the public interface plus the cloud firewall.
- **Deploy a recorded SHA, not "whatever `main` is".** Updates check out a specific reviewed commit. The `mcp` dependency is pinned to an exact version.
- **The vault clone uses `core.sshCommand`** in its own `.git/config` instead of a `GIT_SSH_COMMAND` environment variable, and GitHub's host keys are pinned instead of trusted on first use.
- **Long-running readers follow a rebuilt index file.** After a schema rebuild by the sync timer, the server reopens the new file on its next query; no restart is needed.

## Ownership split

| Who | Does |
| --- | --- |
| Vault Keeper | Design, code, tests, and releases in `JoshTeperman/vault-search`. Independently verifies completion criteria. |
| You (Claude on the VPS) | Pull, configure, deploy, verify, update, roll back. Maintain the nginx config in its on-box git. Report status with the [Report back template](#11-report-back-template). |
| Josh | Adds the read-only deploy key, and approves any firewall or ACL change. |

## 1. Prerequisites and checks

Do these first, every time you land on the box. RAM and Python are the first checks: stop and report if either fails.

- **RAM (v1 is keyword-only):** `free -m` should show at least **512 MB available** (`MemAvailable`). A full-vault fulltext-only index built in 0.16 s and peaked at 23 MB RSS. There is no 2 GB minimum for this default. If available memory is below 512 MB, stop and report to Josh; do not add swap or resize the server yourself.
- The **2 GB minimum (4 GB comfortable)** applies only when semantic search is enabled later. Indexing with embeddings peaked at about 1.3 GB RSS. Do not install `.[semantic]` until that RAM is in place; see [Enabling semantic search later](#enabling-semantic-search-later).
- You reached the VPS with **Tailscale SSH** (not a public SSH port you opened, and not an SSH tunnel for agents).
- `python3 --version` is 3.11 or later; `git`, `systemctl`, `curl`, `nginx`, and `tailscale` are on `PATH`. On Debian/Ubuntu, install the matching `python3.X-venv` package so `python3 -m venv` works.
- `tailscale status` shows this node is online, with MagicDNS and tailnet HTTPS available. Do not paste the node or tailnet name into git.
- Funnel is off:

  ```bash
  tailscale funnel status
  ```

  If Funnel is on, **do not turn it off or on yourself** if that might be an ACL/policy change you were not asked to make — stop and ask Josh. The expected state is off.
- Confirm the node has a tailnet IPv4 address (`tailscale ip -4`) and a public IPv4 address you can use later for the "not reachable on the public IP" check. Write those down only on the box, never in this repo.
- This public repo is clonable over HTTPS (no token required): `https://github.com/JoshTeperman/vault-search.git`.
- **Host firewall:** none, by Josh's decision (see [Decisions](#decisions-19-nov-2026-approved-by-josh)). Check read-only that nothing in this stack listens on the public interface. Josh makes any firewall change.
- nginx is already the reverse proxy on this VPS, on loopback behind `tailscale serve`; its config is maintained in Claude's on-box git, not in `vault-search`.

### Completion criteria

```bash
awk '/MemAvailable/ {exit !($2 >= 512000)}' /proc/meminfo && echo RAM_OK || echo RAM_TOO_SMALL
python3 -c 'import sys; assert sys.version_info >= (3, 11), sys.version'
command -v git systemctl curl nginx tailscale
tailscale status --json | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d.get("BackendState")=="Running", d.get("BackendState")'
tailscale funnel status
```

Expected:

- `RAM_OK` (at least 512 MB available memory). `RAM_TOO_SMALL` means stop and report. A box with less than 2 GB total is fine for keyword-only v1; do not treat that as a failure.
- Python is 3.11 or later; all five commands are on `PATH`.
- Backend state is `Running`. Do not paste the node or tailnet name into git or into this repo.
- `tailscale funnel status` reports Funnel off (no Funnel endpoints advertised). If it is on, stop and ask Josh.

## 2. Service user and directories

Create a dedicated user and the on-box layout. Suggested paths (adjust if the box already has a convention, and do not commit the final host-specific layout):

```text
/var/lib/vault-search/app      # clone of this public repo
/var/lib/vault-search/vault    # pull-only clone of the private vault
/var/lib/vault-search/index    # local search index (gitignored in this repo)
/var/lib/vault-search/ssh      # read-only vault deploy key (mode 0700 / 0600)
/var/log/vault-search          # audit log
/etc/vault-search/env          # environment file, mode 0640, owned by root:vault-search
```

`/var/lib/vault-search/models` is only needed once semantic search is enabled.

```bash
sudo useradd --system --home /var/lib/vault-search --shell /usr/sbin/nologin vault-search
sudo mkdir -p /var/lib/vault-search/{app,vault,index,ssh} /var/log/vault-search /etc/vault-search
sudo chown -R vault-search:vault-search /var/lib/vault-search /var/log/vault-search
sudo chmod 0750 /var/lib/vault-search   # holds the private vault
sudo chmod 0700 /var/lib/vault-search/ssh
sudo chmod 0750 /etc/vault-search
```

The service does not talk to tailscaled, so it needs no extra group.

### Completion criteria

```bash
id vault-search
stat -c '%A %U:%G %n' /var/lib/vault-search /var/lib/vault-search/ssh /var/log/vault-search /etc/vault-search
```

Expected:

- User `vault-search` exists (`id` succeeds).
- `/var/lib/vault-search` and `/var/log/vault-search` are owned by `vault-search:vault-search`; `/var/lib/vault-search` is mode `0750`.
- `/var/lib/vault-search/ssh` is mode `0700`.
- `/etc/vault-search` is mode `0750`.

## 3. Clone vault-search

```bash
sudo -u vault-search git clone https://github.com/JoshTeperman/vault-search.git /var/lib/vault-search/app
sudo -u vault-search git -C /var/lib/vault-search/app checkout --detach <release-sha>   # the reviewed commit to deploy
sudo -u vault-search git -C /var/lib/vault-search/app rev-parse HEAD   # record this SHA in your report
```

Create a virtualenv under the app directory (it is gitignored) and install the package **without** the `[semantic]` extra. v1 is keyword-only (full-text) search. The hybrid/semantic code is in the tree but stays off until you install that extra and reindex; see [Enabling semantic search later](#enabling-semantic-search-later). Use the same Python 3.11+ the box already has (`python3 --version`).

```bash
sudo -u vault-search python3 -m venv /var/lib/vault-search/app/.venv
sudo -u vault-search /var/lib/vault-search/app/.venv/bin/pip install -U pip
sudo -u vault-search /var/lib/vault-search/app/.venv/bin/pip install -e /var/lib/vault-search/app
```

Do not pass `[semantic]` for this deploy. The official `mcp` SDK is a core dependency. A full-vault keyword-only index built in 0.16 s and peaked at 23 MB RSS; the SQLite index is a few megabytes. Disk needs of the app itself are modest. No model is downloaded, and no model cache is expected.

### Completion criteria

```bash
sudo -u vault-search git -C /var/lib/vault-search/app remote get-url origin
sudo -u vault-search git -C /var/lib/vault-search/app status --porcelain
sudo -u vault-search git -C /var/lib/vault-search/app rev-parse HEAD
sudo -u vault-search git -C /var/lib/vault-search/app rev-parse origin/main
test -x /var/lib/vault-search/app/.venv/bin/python
/var/lib/vault-search/app/.venv/bin/python -c 'import importlib.util, sys; sys.exit(0 if importlib.util.find_spec("fastembed") is None else 1)' && echo KEYWORD_ONLY || echo SEMANTIC_INSTALLED
if [ -d /var/lib/vault-search/models ]; then find /var/lib/vault-search/models -type f | wc -l; else echo 0; fi
```

Expected:

- `origin` is `https://github.com/JoshTeperman/vault-search.git` (HTTPS, no token in the URL).
- The working tree is clean (`status --porcelain` is empty).
- `HEAD` is the intended release SHA (normally a commit on `origin/main`). Record that SHA in the report.
- After the venv step, `/var/lib/vault-search/app/.venv/bin/python` is executable.
- The venv prints `KEYWORD_ONLY` (`fastembed` is not installed).
- The model directory file count is `0`. No model cache is expected for v1.

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

3. Ask Josh to leave **Allow write access** unticked when adding the key; that is what makes it read-only.

4. Pin GitHub's SSH host keys instead of trusting on first use. Copy them from GitHub's published list (`https://api.github.com/meta`, `ssh_keys`, or the "GitHub's SSH key fingerprints" docs page) and check the fingerprints against that page:

   ```bash
   curl -fsS https://api.github.com/meta \
     | python3 -c 'import json,sys; [print("github.com", k) for k in json.load(sys.stdin)["ssh_keys"]]' \
     | sudo -u vault-search tee /var/lib/vault-search/ssh/known_hosts >/dev/null
   sudo -u vault-search ssh-keygen -lf /var/lib/vault-search/ssh/known_hosts
   ```

5. Once the key is in place, clone over SSH using a dedicated config that points at this key only:

   ```bash
   sudo -u vault-search tee /var/lib/vault-search/ssh/config >/dev/null <<'EOF'
   Host github-vault
     HostName github.com
     User git
     IdentityFile /var/lib/vault-search/ssh/vault_deploy
     IdentitiesOnly yes
     UserKnownHostsFile /var/lib/vault-search/ssh/known_hosts
     StrictHostKeyChecking yes
   EOF
   sudo chmod 0600 /var/lib/vault-search/ssh/config
   sudo -u vault-search git clone \
     -c core.sshCommand="ssh -F /var/lib/vault-search/ssh/config" \
     git@github-vault:JoshTeperman/knowledge-vault.git /var/lib/vault-search/vault
   ```

   `-c core.sshCommand=…` is saved in the vault clone's own `.git/config`, so every later `git pull` in that checkout (the sync timer, or by hand from any directory) uses the deploy key. Do **not** also set `GIT_SSH_COMMAND`; it would override this.

6. Disable push on that clone:

   ```bash
   sudo -u vault-search git -C /var/lib/vault-search/vault remote set-url --push origin DISABLED
   ```

   Confirm a test `git push` is rejected. Confirm the private key is not in the vault working tree, not in the `vault-search` working tree, and not in the public repo.

### Completion criteria

**Available from milestone 3.**

```bash
stat -c '%a %n' /var/lib/vault-search/ssh/vault_deploy /var/lib/vault-search/ssh/vault_deploy.pub
sudo -u vault-search git -C /var/lib/vault-search/vault rev-parse HEAD
sudo -u vault-search git -C /var/lib/vault-search/vault remote get-url --push origin
sudo -u vault-search git -C /var/lib/vault-search/vault config core.sshCommand
sudo -u vault-search git -C /var/lib/vault-search/vault push origin HEAD
# The private key must not appear in either working tree:
sudo -u vault-search git -C /var/lib/vault-search/vault ls-files | grep -E 'vault_deploy$|id_ed25519' && echo 'FAIL: key in vault tree' || true
sudo -u vault-search git -C /var/lib/vault-search/app ls-files | grep -E 'vault_deploy$|id_ed25519' && echo 'FAIL: key in app tree' || true
```

Expected:

- Private key mode is `600`; public key is readable.
- `HEAD` is a real commit SHA (hash only in the report; no vault file contents).
- `remote get-url --push origin` is `DISABLED`.
- `core.sshCommand` is `ssh -F /var/lib/vault-search/ssh/config`.
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
| `EMBEDDING_MODEL` | leave unset for v1 (keyword-only). Default `BAAI/bge-small-en-v1.5` only when enabling semantic search later |
| `EMBEDDING_CACHE_DIR` | leave unset for v1. Use `/var/lib/vault-search/models` only when enabling semantic search later |
| `EMBEDDING_MODEL_PATH` | leave unset for v1 |
| `AUDIT_LOG_PATH` | `/var/log/vault-search/audit.jsonl` |
| `LISTEN_HOST` | `127.0.0.1` (required) |
| `LISTEN_PORT` | `8787` (or another free loopback port) |
| `MCP_PATH` | `/vault/mcp` |
| `LISTEN_ALLOW_NONLOCAL` | leave unset (the process refuses `0.0.0.0` / `::` without it) |
| `PUBLIC_BASE_URL` | `https://<vps>.<tailnet>.ts.net` — required: the app accepts this `Host` from the proxy. Use the real MagicDNS name **only** in this on-box file |

Leave the `EMBEDDING_*` variables unset for this deploy. Setting a cache directory does not enable semantic search; that requires `pip install '.[semantic]'` and a reindex, and must not be done until the RAM check in [Enabling semantic search later](#enabling-semantic-search-later) passes.

`LISTEN_HOST` must stay loopback. Do not set it to `0.0.0.0`, to a public IP, or to the tailnet IP; `tailscale serve` is what answers on the tailnet. The process refuses a wildcard bind unless `LISTEN_ALLOW_NONLOCAL=1`.

### Completion criteria

```bash
stat -c '%a %U:%G %n' /etc/vault-search/env
sudo grep -E '^(LISTEN_HOST|LISTEN_PORT|MCP_PATH)=' /etc/vault-search/env
sudo grep -cE '^PUBLIC_BASE_URL=https://' /etc/vault-search/env   # 1; do not print the value
sudo grep -E '^(EMBEDDING_CACHE_DIR|EMBEDDING_MODEL_PATH)=' /etc/vault-search/env || true
if [ -d /var/lib/vault-search/models ]; then find /var/lib/vault-search/models -type f | wc -l; else echo 0; fi
```

Expected:

- File mode is `640`, owner `root:vault-search`.
- `LISTEN_HOST=127.0.0.1`
- `LISTEN_PORT` is a port (default `8787`)
- `MCP_PATH=/vault/mcp`
- `PUBLIC_BASE_URL` is set (count `1`).
- `EMBEDDING_CACHE_DIR` and `EMBEDDING_MODEL_PATH` are unset (or empty). No model cache is expected for v1.
- Do not paste other env values (paths, `PUBLIC_BASE_URL`) into git.

## 6. systemd unit (loopback only)

The MCP server lives in this repo (`python -m vault_search.mcp`). You may install the unit file at any time, but do not enable it until `LISTEN_HOST` is `127.0.0.1`, the vault checkout exists, and the index has been built at least once (`python -m vault_search index`). Enabling the unit on the VPS is still blocked on VPS access.

Write `/etc/systemd/system/vault-search.service`:

```ini
[Unit]
Description=vault-search MCP service
After=network.target

[Service]
Type=exec
User=vault-search
Group=vault-search
EnvironmentFile=/etc/vault-search/env
WorkingDirectory=/var/lib/vault-search/app
ExecStart=/var/lib/vault-search/app/.venv/bin/python -m vault_search.mcp
Restart=on-failure
RestartSec=5
UMask=0027
# Loopback only, in and out: the server never makes network calls.
IPAddressDeny=any
IPAddressAllow=localhost
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
ReadWritePaths=/var/lib/vault-search/index /var/log/vault-search
NoNewPrivileges=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectSystem=strict
ProtectHome=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectClock=yes
ProtectHostname=yes
RestrictNamespaces=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
LockPersonality=yes
CapabilityBoundingSet=
SystemCallArchitectures=native

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
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=vault-search
Group=vault-search
EnvironmentFile=/etc/vault-search/env
WorkingDirectory=/var/lib/vault-search/app
ExecStart=/var/lib/vault-search/app/.venv/bin/python -m vault_search sync
Nice=10
UMask=0027
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
ReadWritePaths=/var/lib/vault-search/vault /var/lib/vault-search/index /var/log/vault-search
NoNewPrivileges=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectSystem=strict
ProtectHome=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectClock=yes
ProtectHostname=yes
RestrictNamespaces=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
LockPersonality=yes
CapabilityBoundingSet=
SystemCallArchitectures=native
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

`python -m vault_search sync` runs `git pull --ff-only` in `VAULT_ROOT` and then reindexes. On the v1 keyword-only install this is a full-text rebuild only (no embeddings, no model cache). Embeddings are written only for new or changed chunks after the semantic extra is installed; see [Enabling semantic search later](#enabling-semantic-search-later). It refuses to run if `VAULT_ROOT` is not itself a git checkout. A failed non-fast-forward pull fails the unit and does not rewrite the index. The pull authenticates through the clone's `core.sshCommand` (section 4); the env file needs no SSH setting. If a release changes the index schema, the sync deletes and rebuilds the file; the running server reopens it on its next query.

### Completion criteria

**Available from milestone 6.**

```bash
systemctl is-enabled vault-search
systemctl is-active vault-search
ss -tlnp | grep -E ':8787\b'
/var/lib/vault-search/app/.venv/bin/python -c 'import importlib.util, sys; sys.exit(0 if importlib.util.find_spec("fastembed") is None else 1)' && echo KEYWORD_ONLY || echo SEMANTIC_INSTALLED
if [ -d /var/lib/vault-search/models ]; then find /var/lib/vault-search/models -type f | wc -l; else echo 0; fi
```

Expected:

- `is-enabled` prints `enabled`.
- `is-active` prints `active`.
- `ss` shows `127.0.0.1:8787` (or the configured `LISTEN_PORT`) and does **not** show `0.0.0.0:8787`, `*:8787`, or a public or tailnet address for this process.
- The venv still prints `KEYWORD_ONLY`, and the model directory file count is `0`. No model cache is expected for v1.

### Enabling semantic search later

Leave this section until Vault Keeper asks for it, typically after reviewing audit-log `result_count` values (including `zero_hits` on search). The search code is already in the tree; enabling it is an install plus a reindex, not a code change.

1. **RAM first.** `free -m` should show at least 2048 MB total memory (4096 MB is comfortable). Indexing with embeddings peaked at about 1.3 GB RSS. If the box has less than 2 GB, stop and report to Josh; do not add swap or resize the server yourself, and do not install the extra.
2. Install the optional extra into the existing venv:

   ```bash
   sudo -u vault-search /var/lib/vault-search/app/.venv/bin/pip install -e "/var/lib/vault-search/app[semantic]"
   ```

3. In `/etc/vault-search/env` (on the box only), set `EMBEDDING_MODEL=BAAI/bge-small-en-v1.5` and `EMBEDDING_CACHE_DIR=/var/lib/vault-search/models`. Leave `EMBEDDING_MODEL_PATH` empty unless you are pointing at a pre-fetched model directory. Do not commit this file.
4. Rebuild the index so vectors are written for current chunks:

   ```bash
   sudo -u vault-search /var/lib/vault-search/app/.venv/bin/python -m vault_search index
   ```

5. Restart the service: `sudo systemctl restart vault-search`.

The first run downloads the ONNX model into `EMBEDDING_CACHE_DIR` (about 65 MB). RSS will rise toward the 1.3 GB peak observed while embedding. `mode=hybrid` then fuses full-text with semantic search; until this section is done, it continues to fall back to full-text.

#### Completion criteria

```bash
awk '/MemTotal/ {exit !($2 >= 2000000)}' /proc/meminfo && echo RAM_OK || echo RAM_TOO_SMALL
/var/lib/vault-search/app/.venv/bin/python -c 'import fastembed; print("SEMANTIC_OK")'
sudo grep -E '^(EMBEDDING_MODEL|EMBEDDING_CACHE_DIR)=' /etc/vault-search/env
if [ -d /var/lib/vault-search/models ]; then find /var/lib/vault-search/models -type f | wc -l; else echo 0; fi
systemctl is-active vault-search
ss -tlnp | grep -E ':8787\b'
```

Expected:

- `RAM_OK` (at least 2 GB total memory). `RAM_TOO_SMALL` means stop and report; do not install the extra.
- The venv prints `SEMANTIC_OK`.
- `EMBEDDING_MODEL` and `EMBEDDING_CACHE_DIR` are set in the on-box env file. Do not paste those values into git.
- The model directory file count is greater than `0`.
- The index command reported embeddings written (a positive `embeddings` count in its summary). Do not paste vault content.
- `is-active` is `active`, and `ss` still shows only `127.0.0.1:8787` (or the configured loopback port).

## 7. nginx in front of the app

**Available from milestone 6.** This VPS already runs `tailscale serve` → nginx. Its config lives on the VPS, in Claude's own git — not in `vault-search`.

```text
tailnet ──HTTPS──▶ tailscaled :443 ──HTTP──▶ nginx 127.0.0.1:8080 ──▶ vault-search 127.0.0.1:8787
                  (tailscale serve,           (location /vault/mcp)
                   TLS + *.ts.net cert)
```

`tailscale serve` is tailnet-only and handles TLS and certificate renewal, so there is no `tailscale cert` step. It sets `X-Forwarded-For` to the caller's tailnet IP; the on-box nginx config turns that into `$remote_addr` with `real_ip_header` trusted only from loopback.

Add this `location` to the existing loopback `server` in the on-box nginx config. `proxy_pass` has no trailing slash, so the `/vault/mcp` prefix is forwarded to the app (which uses `MCP_PATH=/vault/mcp`).

```nginx
location /vault/mcp {
    proxy_pass http://127.0.0.1:8787;
    proxy_http_version 1.1;
    proxy_buffering off;
    proxy_request_buffering off;
    proxy_read_timeout 1h;
    proxy_send_timeout 1h;
    # Streamable HTTP is a long-lived HTTP/1.1 response, not WebSocket.
    proxy_set_header Connection "";
    proxy_set_header Host $host;   # must match PUBLIC_BASE_URL's host
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Forwarded-Proto https;
}
```

After editing the on-box config (in its own git):

```bash
sudo nginx -t && sudo systemctl reload nginx
```

Do not change the `tailscale serve` mapping or enable Funnel without Josh's approval.

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
- `ss -tlnp` shows this stack only as: the app on `127.0.0.1:8787`, nginx on `127.0.0.1:8080`, and tailscaled on `<tailnet-ip>:443`. Nothing in this stack listens on the public IP or on `0.0.0.0`/`*`.
- `tailscale funnel status` is still off.
- From another tailnet node, the endpoint in section 9 answers. From off the tailnet, a curl to the public IP fails.

## 8. Caller details and audit log

Access control is the tailnet: only tailnet devices can reach `tailscale serve`, and the app does not authorise callers itself (see [Decisions](#decisions-19-nov-2026-approved-by-josh)). It rejects only requests that carry `Tailscale-Funnel-Request`, as defence in depth.

The app takes the caller's IP from `X-Real-IP` / `X-Forwarded-For` **only** when the immediate TCP peer is loopback (nginx); otherwise it uses the peer address. That IP is the caller's tailnet address, which `tailscale status` maps to a device. A self-reported `X-Vault-Agent-Name` header is recorded when present and marked **unverified**; it is not used for anything else.

The audit log (`AUDIT_LOG_PATH`) is append-only JSON lines on the box. Each line includes timestamp, `client_ip`, `agent_name_unverified`, tool, masked arguments, `result_count`, `masked_hits`, and `zero_hits` for searches with no results. Vault Keeper uses those counts to decide whether to enable semantic search. Do not log secret values or vault body text. Do not commit the log.

### Completion criteria

**Available from milestone 6** (after a successful call from section 9).

```bash
# On the VPS, after the tailnet curl / verify-deploy run:
sudo tail -n 20 /var/log/vault-search/audit.jsonl
```

Expected:

- A new line exists for the fixture `read_note` / `search` call.
- The line's `client_ip` is the calling device's tailnet IP (not `127.0.0.1`). If `X-Vault-Agent-Name` was sent, it appears as `agent_name_unverified`.
- The line does **not** contain secret values or vault body text. In the report, quote only the outcome — not a raw log dump, not IPs or host names.

## 9. End-to-end verification

**Available from milestone 6**, except Funnel and bind checks, which you can run as soon as nginx and the unit exist.

Run the on-box commands on the VPS. Run the HTTP commands (or `scripts/verify-deploy`) from a **different** node on the tailnet, and the public-IP curl from a host that is **not** on the tailnet. Do not commit commands with real names filled in.

Claude's passing output is not acceptance. Vault Keeper re-runs these checks from the shared Grok Bot box (the verification node; untagged, Josh's identity, read/verify only) with `scripts/verify-deploy` (externally visible criteria) and the on-box commands over Tailscale SSH.

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

   Expected: vault-search and nginx on `127.0.0.1` only; tailscaled on the tailnet IP port 443; nothing in this stack on the public IP or on `0.0.0.0`/`*`.

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

6. **The audit log records the caller's tailnet IP** — see [section 8 completion criteria](#8-caller-details-and-audit-log).

7. **The built index contains no unmasked secrets** (on the VPS). This is an on-box check; `scripts/verify-deploy` does not run it, because that script only covers externally visible HTTP criteria.

   ```bash
   sudo -u vault-search /var/lib/vault-search/app/.venv/bin/python -m vault_search index-audit /var/lib/vault-search/index/vault.db
   ```

   Expected: exit code `0` and no output. Exit `1` means the detector found a secret-like span in a table, an FTS shadow table, or the raw database bytes (including WAL/journal sidecars). The command never prints the matched value. In the report, say only pass/fail and any rule names — do not paste findings or vault text.

### Completion criteria

**Available from milestone 6.** This section is done when every item above has the expected result, **and** Vault Keeper has independently obtained the same result with `scripts/verify-deploy` plus the on-box checks. A report from Claude alone does not close the milestone.

## 10. Update and rollback

**Update** (after milestone 6; for a code-only pull you can already update the clone):

```bash
sudo -u vault-search git -C /var/lib/vault-search/app fetch origin
sudo -u vault-search git -C /var/lib/vault-search/app rev-parse HEAD   # previous SHA, for rollback
sudo -u vault-search git -C /var/lib/vault-search/app checkout --detach <release-sha>
sudo -u vault-search /var/lib/vault-search/app/.venv/bin/pip install -e /var/lib/vault-search/app
sudo systemctl restart vault-search    # only once the unit is enabled
```

Keep the v1 install keyword-only: do not add `[semantic]` on a routine update. If semantic search was already enabled, reinstall with `pip install -e "/var/lib/vault-search/app[semantic]"` instead so the extra is not dropped, then reindex if the model or schema changed.

A schema-version change deletes the existing index file and its WAL/journal sidecars and rebuilds from scratch, so older unmasked rows cannot remain in free pages. After pulling a release that bumps the schema, rebuild the index **before** relying on the service, then audit the file:

```bash
sudo -u vault-search /var/lib/vault-search/app/.venv/bin/python -m vault_search index
sudo -u vault-search /var/lib/vault-search/app/.venv/bin/python -m vault_search index-audit /var/lib/vault-search/index/vault.db
sudo systemctl restart vault-search    # only once the unit is enabled
```

If the unit is restarted before that reindex, it will open an empty rebuilt file and searches will return nothing until `python -m vault_search index` or the sync timer runs. The server reopens the file when the reindex replaces it.

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

- For an update: `HEAD` equals the intended release SHA; the previous SHA was recorded first.
- For a rollback: `HEAD` equals the recorded previous SHA.
- After milestone 6, `systemctl is-active vault-search` is `active`, and section 9 still passes (including Vault Keeper's independent `scripts/verify-deploy` run).

## 11. Report back template

Send the filled template privately (not as a commit to this repo). Do not include host names, tailnet names, IP addresses, `.env` values, private keys, tokens, vault note content, or raw audit-log dumps. If inventory names are needed, send them to Josh privately.

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
ss -tlnp summary (app and nginx on 127.0.0.1? tailscaled on tailnet :443? nothing on public IP or 0.0.0.0? yes/no, no addresses):
Funnel (expected: off):

MCP initialize / tools/list (pass/fail; list tool names only):
Masked fixture query (pass/fail; say whether [REDACTED:] appeared; do not paste the secret):
Index audit (`python -m vault_search index-audit`; pass/fail; rule names only, no findings dump):
Public IP curl (failed as expected / UNEXPECTED SUCCESS):
Audit log: caller tailnet IP present (yes/no; no IPs, no log dump):

Deploy key public half (first time only; never the private key):
Blockers / questions for Josh (ask before any firewall or ACL change):

I have not treated this report as acceptance. Please re-run scripts/verify-deploy and the on-box checks.
```
