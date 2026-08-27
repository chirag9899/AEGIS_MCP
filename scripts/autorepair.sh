#!/usr/bin/env bash
# Aegis connector auto-repair (macOS).
#
#   Install:    curl -fsSL https://aegis.infrasingularity.com/autorepair.sh | bash
#   Uninstall:  curl -fsSL https://aegis.infrasingularity.com/autorepair.sh | bash -s -- uninstall
#
# Install: a launchd agent re-asserts the mcp-remote (npx) connector shape at every
#   login (and every 5 min while Claude is closed) so Claude's connector sync can't
#   flip it to URL-shape, which silently breaks token refresh. Reads device key/URL
#   from ~/.aegis — no secrets baked in. Requires install.sh to have run once.
#
# Uninstall: removes the launchd agent AND strips the aegis entry from Claude's
#   config in one go (optionally purges ~/.aegis and ~/.mcp-auth too).
set -euo pipefail

AEGIS_DIR="$HOME/.aegis"
LA_DIR="$HOME/Library/LaunchAgents"
PLIST="$LA_DIR/com.infrasingularity.aegis-repair.plist"
SCRIPT="$AEGIS_DIR/repair_connector.py"
CONFIG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
MODE="${1:-install}"

quit_claude() {
    osascript -e 'quit app "Claude"' 2>/dev/null || true
    sleep 2
    if pgrep -x Claude >/dev/null 2>&1; then
        echo "⚠️  Claude is still running — force-quit it (Activity Monitor) and re-run."
        exit 1
    fi
    pkill -f "mcp-remote.*aegis" 2>/dev/null || true
    echo "Claude closed ✓"
}

strip_connector() {
    # Remove every aegis entry from claude_desktop_config.json (Claude must be quit).
    /usr/bin/python3 - "$CONFIG" <<'PYEOF'
import json, os, sys
p = sys.argv[1]
if not os.path.isfile(p):
    print("no config file — nothing to strip"); raise SystemExit
try:
    c = json.load(open(p))
except Exception:
    print("config not valid JSON — leaving untouched"); raise SystemExit
s = c.get("mcpServers", {})
removed = [k for k in list(s) if "aegis" in k.lower()]
for k in removed:
    del s[k]
if removed:
    tmp = p + ".tmp"; json.dump(c, open(tmp, "w"), indent=2); os.replace(tmp, p)
    print("removed connector entries:", removed)
else:
    print("no aegis connector entries in config")
PYEOF
}

# ---------------------------------------------------------------------------
if [ "$MODE" = "uninstall" ] || [ "$MODE" = "remove" ]; then
    echo "== Aegis auto-repair + connector UNINSTALL =="
    quit_claude
    # 1. launchd agent + repair script
    launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST" "$SCRIPT"
    echo "auto-repair agent removed ✓"
    # 2. connector entry from Claude config
    strip_connector
    # 3. optional purge of auth/device dirs
    rm -rf "$HOME/.mcp-auth" "$AEGIS_DIR"
    echo "purged ~/.mcp-auth and ~/.aegis ✓"
    echo
    echo "Done. If an aegis connector still shows in Claude, it's an ACCOUNT-level"
    echo "connector — remove it in Claude → Settings → Connectors (and claude.ai)."
    exit 0
fi

# ---------------------------- INSTALL --------------------------------------
echo "== Aegis auto-repair INSTALL =="
mkdir -p "$AEGIS_DIR" "$LA_DIR"

# Seed the target connector label (fresh namespace, no stale disabled-tool flags).
# Don't clobber a label the user/agent may have already rotated to.
[ -f "$AEGIS_DIR/label" ] || echo "aegis-workspace" > "$AEGIS_DIR/label"

cat > "$SCRIPT" <<'PYEOF'
#!/usr/bin/env python3
"""Re-assert the Aegis MCP connector as an mcp-remote (npx) entry if it drifted.

Runs from a launchd agent; only repairs while Claude Desktop is CLOSED. Rebuilds the
entry from ~/.aegis/device.json + mcp-oauth-client.json; no secrets baked in.
"""
import json, os, subprocess, sys

HOME = os.path.expanduser("~")
CONFIG = os.path.join(HOME, "Library/Application Support/Claude/claude_desktop_config.json")
AEGIS = os.path.join(HOME, ".aegis")
DEVICE = os.path.join(AEGIS, "device.json")
OAUTH = os.path.join(AEGIS, "mcp-oauth-client.json")
MCP_REMOTE = "mcp-remote@0.1.38"


def log(msg):
    print(f"[aegis-repair] {msg}", flush=True)


def claude_running():
    try:
        return subprocess.run(["pgrep", "-x", "Claude"], capture_output=True).returncode == 0
    except Exception:
        return False


def main():
    if not os.path.isfile(DEVICE):
        log("no ~/.aegis/device.json — run install.sh once; nothing to repair")
        return
    dev = json.load(open(DEVICE))
    key, url = dev.get("device_key"), dev.get("mcp_url")
    if not (key and url):
        log("device.json missing key/url; skipping")
        return
    # Target connector label: a fresh, un-poisoned namespace so Claude's stale
    # per-tool "disabled" flags (keyed to the old 'aegis-google' label) never apply.
    # Override by writing a different label to ~/.aegis/label — it self-migrates.
    label_file = os.path.join(AEGIS, "label")
    default_label = "aegis-workspace"
    try:
        name = open(label_file).read().strip() or default_label
    except Exception:
        name = default_label
    # Keep device.json in sync so mcp_preauth and this agent agree on the label.
    if dev.get("server_name") != name:
        dev["server_name"] = name
        try:
            td = DEVICE + ".tmp"; json.dump(dev, open(td, "w"), indent=2); os.replace(td, DEVICE)
        except Exception as e:
            log(f"could not update device.json label (non-fatal): {e}")
    has_static = os.path.isfile(OAUTH)
    if not has_static:
        log("WARNING: ~/.aegis/mcp-oauth-client.json missing — run mcp_preauth.sh to "
            "restore the static client (auto-repair can't regenerate it)")
    else:
        # Ensure the static client tells mcp-remote to send client_id in the token
        # BODY (client_secret_post). Without this, mcp-remote defaults to
        # client_secret_basic (client_id only in the Authorization header), and the
        # MCP SDK rejects it as "Missing client_id" → perpetual re-auth loop.
        try:
            oc = json.load(open(OAUTH))
            if oc.get("token_endpoint_auth_method") != "client_secret_post":
                oc["token_endpoint_auth_method"] = "client_secret_post"
                tf = OAUTH + ".tmp"
                json.dump(oc, open(tf, "w")); os.replace(tf, OAUTH)
                log("patched static client: token_endpoint_auth_method=client_secret_post")
        except Exception as e:
            log(f"could not patch static client auth method (non-fatal): {e}")

    if claude_running():
        log("Claude is running; will repair next time it's closed")
        return

    # Claude is closed → no mcp-remote should be alive. Kill orphans so the next
    # launch starts ONE clean instance. Duplicate instances race the ~/.mcp-auth
    # cache and produce the 'Missing client_id' refresh loop (seen on Jitin's Mac).
    try:
        r = subprocess.run(["pgrep", "-f", "mcp-remote.*aegis"], capture_output=True)
        if r.returncode == 0 and r.stdout.strip():
            subprocess.run(["pkill", "-9", "-f", "mcp-remote.*aegis"], capture_output=True)
            log("killed orphan mcp-remote process(es) (Claude is closed)")
    except Exception:
        pass

    desired_args = ["-y", MCP_REMOTE, url, "--header", f"X-Device-Key: {key}",
                    "--transport", "http-first"]
    if has_static:
        desired_args += ["--static-oauth-client-info", "@" + OAUTH]
    desired = {"command": "npx", "args": desired_args}

    cfg = {}
    if os.path.isfile(CONFIG):
        try:
            cfg = json.load(open(CONFIG))
        except Exception:
            log("claude_desktop_config.json is not valid JSON; skipping to avoid clobber")
            return
    servers = cfg.setdefault("mcpServers", {})

    changed = False
    for k in [k for k in list(servers) if "aegis" in k.lower() and k != name]:
        del servers[k]; changed = True; log(f"removed stray aegis entry '{k}'")

    cur = servers.get(name)

    def needs_repair(e):
        if not isinstance(e, dict):
            return True
        if "url" in e:
            return True
        if e.get("command") != "npx":
            return True
        args = [str(x) for x in e.get("args", [])]
        if not any(MCP_REMOTE in x for x in args):
            return True
        if url not in args:
            return True
        if not any("X-Device-Key" in x for x in args):
            return True
        if has_static and "--static-oauth-client-info" not in args:
            return True
        return False

    if needs_repair(cur):
        servers[name] = desired
        changed = True
        log(f"repaired '{name}' to mcp-remote/npx shape")

    if changed:
        tmp = CONFIG + ".tmp"
        os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
        json.dump(cfg, open(tmp, "w"), indent=2)
        os.replace(tmp, CONFIG)
        log("config updated")
    else:
        log("config already correct; no change")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"error: {e}")
        sys.exit(0)
PYEOF
chmod +x "$SCRIPT"

cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.infrasingularity.aegis-repair</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>$SCRIPT</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>StartInterval</key><integer>300</integer>
  <key>StandardOutPath</key><string>$AEGIS_DIR/repair.log</string>
  <key>StandardErrorPath</key><string>$AEGIS_DIR/repair.log</string>
</dict>
</plist>
PLISTEOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
/usr/bin/python3 "$SCRIPT"

echo
echo "Installed ✓  Auto-repair runs at login and every 5 min while Claude is closed."
echo "It keeps the connector on a clean label (\"$(cat "$AEGIS_DIR/label")\") so tools"
echo "never show 'disabled in connector settings', and pins the mcp-remote shape so"
echo "auth never loops. Reopen Claude — you don't have to touch anything."
echo "Log:            $AEGIS_DIR/repair.log"
echo "Rotate label:   echo aegis-workspace2 > $AEGIS_DIR/label   (if a label ever gets poisoned)"
echo "Uninstall:      curl -fsSL https://aegis.infrasingularity.com/autorepair.sh | bash -s -- uninstall"
