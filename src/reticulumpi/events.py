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

# Hub pool auto-discovery
HUB_POOL_CONNECTED = "hub_pool.connected"
HUB_POOL_DISCONNECTED = "hub_pool.disconnected"
HUB_POOL_EXHAUSTED = "hub_pool.exhausted"
HUB_POOL_DISCOVERED = "hub_pool.discovered"

# Routing diagnostics
PATH_TABLE_EMPTY = "routing.path_table_empty"
PATHS_STALE = "routing.paths_stale"
SINGLE_INTERFACE_SPOF = "routing.single_interface_spof"

# Path warming
PATH_WARMED = "path.warmed"
PATH_WARM_FAILED = "path.warm_failed"
PATH_WARMING_CYCLE = "path.warming_cycle"

# Transport node health
TRANSPORT_NODE_DOWN = "transport_node.down"
TRANSPORT_NODE_RECOVERED = "transport_node.recovered"
TRANSPORT_NODE_DEGRADED = "transport_node.degraded"
TRANSPORT_NODE_DISCOVERED = "transport_node.discovered"

# NomadNet page auth
NOMADNET_AUTH_IDENTITY_ADDED = "nomadnet.auth.identity_added"
NOMADNET_AUTH_IDENTITY_REMOVED = "nomadnet.auth.identity_removed"

# Meshtastic gateway
MESHTASTIC_CONNECTED = "meshtastic.connected"
MESHTASTIC_DISCONNECTED = "meshtastic.disconnected"
MESHTASTIC_CONNECT_FAILED = "meshtastic.connect_failed"
MESHTASTIC_MESSAGE_RECEIVED = "meshtastic.message_received"
MESHTASTIC_MESSAGE_SENT = "meshtastic.message_sent"
MESHTASTIC_NODEINFO_SENT = "meshtastic.nodeinfo_sent"

# MeshCore gateway
MESHCORE_CONNECTED = "meshcore.connected"
MESHCORE_DISCONNECTED = "meshcore.disconnected"
MESHCORE_CONNECT_FAILED = "meshcore.connect_failed"
MESHCORE_MESSAGE_RECEIVED = "meshcore.message_received"
MESHCORE_MESSAGE_SENT = "meshcore.message_sent"

# Yggdrasil transport
YGGDRASIL_ONLINE = "yggdrasil.online"
YGGDRASIL_OFFLINE = "yggdrasil.offline"
YGGDRASIL_PEERS_CHANGED = "yggdrasil.peers_changed"
YGGDRASIL_RNS_CONFIGURED = "yggdrasil.rns_configured"

# LoRa diagnostics
LORA_PEER_ANNOUNCE_RECEIVED = "lora.peer_announce_received"
LORA_PEER_PATH_LOST = "lora.peer_path_lost"
LORA_STATS_UPDATED = "lora.stats_updated"

# Shutdown
SHUTDOWN_STARTING = "shutdown.starting"

# Messaging hub
MESSAGE_RECEIVED = "messaging.message_received"
MESSAGE_SENT = "messaging.message_sent"
MESSAGE_FAILED = "messaging.message_failed"
MESSAGE_STATUS_CHANGED = "messaging.message_status_changed"

# Meshtastic message ack/nak
MESHTASTIC_MESSAGE_ACKED = "meshtastic.message_acked"

# MeshCore message ack
MESHCORE_MESSAGE_ACKED = "meshcore.message_acked"

# Space tracker — satellites, launches, space weather
SPACE_TLE_UPDATED = "space.tle.updated"
SPACE_POSITIONS_SNAPSHOT = "space.positions.snapshot"
SPACE_PASS_UPCOMING = "space.pass.upcoming"
SPACE_LAUNCH_UPCOMING = "space.launch.upcoming"
SPACE_WEATHER_UPDATED = "space.weather.updated"
