"""Event type constants for the ReticulumPi event bus."""

from __future__ import annotations

# Plugin lifecycle
PLUGIN_STARTED = "plugin.started"
PLUGIN_STOPPED = "plugin.stopped"
PLUGIN_CRASHED = "plugin.crashed"

# System metrics
METRICS_UPDATED = "metrics.updated"

# Mesh network
NODE_DISCOVERED = "node.discovered"
NODE_METRICS_RECEIVED = "node.metrics_received"

# Alerts
ALERT_TRIGGERED = "alert.triggered"

# File transfer
FILE_RECEIVED = "file.received"

# Links
LINK_ESTABLISHED = "link.established"
LINK_CLOSED = "link.closed"

# Sensors
SENSOR_READING = "sensor.reading"

# Emergency
EMERGENCY_RECEIVED = "emergency.received"
