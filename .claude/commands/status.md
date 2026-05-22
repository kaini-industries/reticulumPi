Report ReticulumPi node health. Run these checks in parallel where possible:

1. **Services:** `sudo systemctl is-active reticulumpi rnsd`
2. **Reticulum network:** `sudo -u reticulumpi /opt/reticulumpi/.venv/bin/rnstatus` -- summarize interface count and known paths
3. **Dashboard:** `curl -s -o /dev/null -w "%{http_code}" --max-time 3 http://127.0.0.1:8080/login.html`
4. **Recent errors:** `sudo journalctl -u reticulumpi --since "10 minutes ago" -p err --no-pager -q` -- count errors, show last 3 if any
5. **System resources:**
   - CPU temp: `cat /sys/class/thermal/thermal_zone0/temp` (divide by 1000 for Celsius)
   - Memory: `free -m | grep Mem`
   - Disk: `df -h /`

Present as a compact summary table. Flag anything abnormal (service not active, dashboard unreachable, temp >70C, disk >85%).
