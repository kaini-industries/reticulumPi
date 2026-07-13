Systematic triage of the web dashboard. Stop at the first definitive failure.

Steps:
1. **Service binding:** `ss -tlnp | grep 8080`
   - If nothing listening, check `sudo systemctl status reticulumpi --no-pager` for errors.
   - Common cause: plugin disabled in config or port conflict.

2. **Plugin loaded** (TLS with self-signed cert since v0.2.3 — `-k`/`ssl=False` required): `curl -sk --max-time 3 https://127.0.0.1:8080/api/version`
   - Should return JSON with `api_version` and `app_version`.
   - If connection refused, the service is up but dashboard plugin failed to start.

3. **Key endpoints (test in parallel):**
   ```
   curl -sk --max-time 5 https://127.0.0.1:8080/api/status
   curl -sk --max-time 5 https://127.0.0.1:8080/api/plugins
   curl -sk --max-time 5 https://127.0.0.1:8080/api/mesh/summary
   curl -sk --max-time 5 https://127.0.0.1:8080/api/interfaces
   curl -sk --max-time 5 https://127.0.0.1:8080/api/metrics
   ```
   - Check each for HTTP 200 and valid JSON. Report any failures with status code and body.

4. **WebSocket connectivity:** `/ws/metrics` requires auth (401 without a token) — log in first:
   ```
   PW=$(sudo cat /var/lib/reticulumpi/.config/reticulumpi/dashboard_password.txt 2>/dev/null)
   .venv/bin/python - "$PW" <<'EOF'
   import asyncio, aiohttp, sys
   async def test():
       async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as s:
           r = await s.post('https://127.0.0.1:8080/api/auth/login', json={'password': sys.argv[1]})
           token = (await r.json())['data']['token']
           async with s.ws_connect('https://127.0.0.1:8080/ws/metrics',
                                   headers={'Authorization': f'Bearer {token}'}) as ws:
               msg = await asyncio.wait_for(ws.receive(), timeout=10)
               print(f'OK: received {len(msg.data)} bytes')
   asyncio.run(test())
   EOF
   ```
   - First message should be a large JSON `update` payload.
   - `dashboard_password.txt` only exists for auto-generated passwords; if the operator set one
     in config, ask them for it.
   - If login returns 401, the password is wrong; 429 means rate-limited (wait per Retry-After).
   - If the ws_connect fails, check `sudo journalctl -u reticulumpi --since "2 min ago" | grep -i websocket`.

5. **Auth secret:** Check that the dashboard secret file exists (dir is owned by the
   reticulumpi user, so use sudo):
   `sudo ls -la /var/lib/reticulumpi/.config/reticulumpi/dashboard_secret`
   - Default location is the `secret_dir` config option (`~/.config/reticulumpi`).

6. **Static files:** Verify key frontend files exist:
   ```
   ls src/reticulumpi/builtin_plugins/web_dashboard/static/index.html
   ls src/reticulumpi/builtin_plugins/web_dashboard/static/app.js
   ls src/reticulumpi/builtin_plugins/web_dashboard/static/style.css
   ```

7. Report a summary: which checks passed, which failed, and suggested fix for the first failure.

Note: `journalctl -p err` / `-p warning` does NOT catch the app's Python-level WARNING/ERROR
lines — stdout logs land in journald at INFO priority. Grep message text instead, e.g.
`sudo journalctl -u reticulumpi --since "10 min ago" | grep -E "ERROR:|Slow broadcast plugin"`.
