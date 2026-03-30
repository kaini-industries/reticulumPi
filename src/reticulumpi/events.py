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

# Transport monitoring
HUB_ONLINE = "hub.online"
HUB_OFFLINE = "hub.offline"
FALLBACK_ACTIVATED = "transport.fallback_activated"
FALLBACK_DEACTIVATED = "transport.fallback_deactivated"

# Connectivity monitoring
RNSD_DOWN = "connectivity.rnsd_down"
RNSD_RECOVERED = "connectivity.rnsd_recovered"
INTERFACE_OFFLINE = "connectivity.interface_offline"
I2P_NO_PEERS = "connectivity.i2p_no_peers"

# Routing diagnostics
PATH_TABLE_EMPTY = "routing.path_table_empty"
PATHS_STALE = "routing.paths_stale"
SINGLE_INTERFACE_SPOF = "routing.single_interface_spof"
