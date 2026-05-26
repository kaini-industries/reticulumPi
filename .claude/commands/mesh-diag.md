Deep mesh network diagnostics. Synthesizes data from multiple plugins and system tools.

Run these checks and compile a health grade (healthy / degraded / critical):

1. **Interface inventory:** `sudo -u reticulumpi /opt/reticulumpi/.venv/bin/rnstatus`
   - Count active interfaces, note any that show 0 TX/RX bytes.

2. **Mesh summary:** `curl -s http://127.0.0.1:8080/api/mesh/summary`
   - Report: total nodes, app breakdown, hop distribution, recent growth.
   - Flag: 0 nodes = critical, declining count = degraded.

3. **Transport health:** `curl -s http://127.0.0.1:8080/api/transport`
   - Report: primary hub count, which are online/offline, fallback status.
   - Flag: all hubs offline = critical, any hub offline = degraded.

4. **Connectivity issues:** `curl -s http://127.0.0.1:8080/api/connectivity`
   - List any active issues with severity.

5. **Routing table:** `curl -s 'http://127.0.0.1:8080/api/routing?per_page=0'`
   - Report: total paths, paths by interface, blackholed destinations.
   - Flag: 0 paths = critical, blackholes present = degraded.

6. **Reachability:** `curl -s 'http://127.0.0.1:8080/api/reachability?per_page=0'`
   - Report: score distribution (high/good/fair/low/unlikely), average score.

7. **Internet connectivity:** `curl -s http://127.0.0.1:8080/api/status`
   - Extract internet_online, wan_ip, lan_ip from the status response.

8. **rnsd socket:** `ss -xa | grep @rns/default`
   - Flag: socket missing = critical (rnsd is down).

Compile results into an overall health grade:
- **Critical:** rnsd socket missing, 0 interfaces, 0 nodes, or all hubs offline
- **Degraded:** any hub offline, blackholes present, declining node count, high error rate
- **Healthy:** all checks pass, nodes growing or stable

Present as a structured report with the grade at the top and details below.
