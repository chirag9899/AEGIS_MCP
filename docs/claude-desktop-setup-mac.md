# Fix: `aegis-google` disconnected — npm EACCES on Mac

Claude starts `mcp-remote` but npm crashes first:

```
npm ERR! code EACCES
npm ERR! path ~/.npm/_cacache/tmp/...
npm ERR! Your cache folder contains root-owned files
```

**Cause:** `~/.npm` has root-owned files (usually from past `sudo npm` / `sudo npx`).  
**Not a server issue** — config, device key, and allowlist are fine.

**Remove the old connector?** No — not for this error. Your `claude_desktop_config.json` entry is already correct; npm dies before `mcp-remote` runs. Only remove/re-add if you are redoing setup from scratch, or you added `aegis-google` twice (local config **and** Settings → Connectors).

---

## Step 1 — Fix permissions

```bash
sudo chown -R $(whoami):staff ~/.npm
```

No output = success. Enter your Mac password when prompted.

If still failing:

```bash
sudo chown -R $(whoami):staff ~/.npm/_cacache ~/.npm/_npx
```

Do not run `sudo npm` or `sudo npx` again.

---

## Step 2 — Verify npm

```bash
npx --yes cowsay "npx works"
```

Should print an ASCII cow. If `EACCES`, repeat Step 1.

---

## Step 3 — Test mcp-remote

Copy `X-Device-Key` from `~/Library/Application Support/Claude/claude_desktop_config.json` (not the registration secret from `install.sh`):

```bash
npx --yes mcp-remote \
  https://aegis.infrasingularity.com/google/mcp \
  --header "X-Device-Key: YOUR_KEY"
```

Should stay running with no `EACCES`. Press **Ctrl+C** to stop.

---

## Step 4 — Restart Claude

1. Quit Claude fully (not just close the window)
2. Reopen and wait ~30s for `aegis-google` to connect

---

## Checklist

- [ ] `chown ~/.npm` — no error
- [ ] `cowsay` test works
- [ ] `mcp-remote` runs without `EACCES`
- [ ] Claude reconnects

---

## Still broken?

| Symptom | Fix |
|---------|-----|
| `EACCES` on `_cacache` | `ls -la ~/.npm` — owner should be you, not `root` |
| `Server transport closed unexpectedly` | Run Step 3 in Terminal for the real error |
| `Invalid URL` + `--help` | First arg must be the MCP URL, not `--help` |

No need to re-run `install.sh` unless Step 3 shows a different error.
