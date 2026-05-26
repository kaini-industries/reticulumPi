Systematic triage of the web dashboard. Stop at the first definitive failure.

Steps:
1. **Service binding:** `ss -tlnp | grep 8080`
   - If nothing listening, check `sudo systemctl status reticulumpi --no-pager` for errors.
   - Common cause: plugin disabled in config or port conflict.

2. **Plugin loaded:** `curl -s --max-time 3 http://127.0.0.1:8080/api/version`
   - Should return JSON with `api_version` and `app_version`.
   - If connection refused, the service is up but dashboard plugin failed to start.

3. **Key endpoints (test in parallel):**
   ```
   curl -s --max-time 5 http://127.0.0.1:8080/api/status
   curl -s --max-time 5 http://127.0.0.1:8080/api/plugins
   curl -s --max-time 5 http://127.0.0.1:8080/api/mesh/summary
   curl -s --max-time 5 http://127.0.0.1:8080/api/interfaces
   curl -s --max-time 5 http://127.0.0.1:8080/api/metrics
   ```
   - Check each for HTTP 200 and valid JSON. Report any failures with status code and body.

4. **WebSocket connectivity:**
   ```
   .venv/bin/python -c "
   import asyncio, aiohttp
   async def test():
       async with aiohttp.ClientSession() as s:
           async with s.ws_connect('http://127.0.0.1:8080/ws/metrics') as ws:
               msg = await asyncio.wait_for(ws.receive(), timeout=10)
               print(f'OK: received {len(msg.data)} bytes')
   asyncio.run(test())
   "
   ```
   - If this fails, check `sudo journalctl -u reticulumpi --since "2 min ago" | grep -i websocket`.

5. **Auth database:** Check that the dashboard secret file exists:
   `ls -la /home/reticulumpi/.local/share/reticulumpi/dashboard_secret`

6. **Static files:** Verify key frontend files exist:
   ```
   ls src/reticulumpi/builtin_plugins/web_dashboard/static/index.html
   ls src/reticulumpi/builtin_plugins/web_dashboard/static/app.js
   ls src/reticulumpi/builtin_plugins/web_dashboard/static/style.css
   ```

7. Report a summary: which checks passed, which failed, and suggested fix for the first failure.
