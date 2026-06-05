"""Event type constants for the ReticulumPi event bus."""

from __future__ import annotations

# Plugin lifecycle
PLUGIN_STARTED = "plugin.started"
PLUGIN_STOPPED = "plugin.stopped"
PLUGIN_CRASHED = "plugin.crashed"
PLUGIN_STOPPING = "plugin.stopping"

# Mesh network
NODE_DISCOVERED = "node.discovered"
NODE_METRICS_RECEIVED = "node.metrics_received"

# Alerts
ALERT_TRIGGERED = "alert.triggered"

# File transfer
FILE_RECEIVED = "file.received"

# Sensors
SENSOR_READING = "sensor.reading"

# Emergency
EMERGENCY_RECEIVED = "emergency.received"

# Transport monitoring
HUB_ONLINE = "hub.online"
HUB_OFFLINE = "hub.offline"
FALLBACK_ACTIVATED = "transport.fallback_activated"
FALLBACK_DEACTIVATED = "transport.fallback_deactivated"

# Internet connectivity
INTERNET_ONLINE = "internet.online"
INTERNET_OFFLINE = "internet.offline"
OFFGRID_MODE_CHANGED = "offgrid.mode_changed"
TCP_INTERFACES_AUTO_DISABLED = "internet.tcp_auto_disabled"
TCP_INTERFACES_AUTO_ENABLED = "internet.tcp_auto_enabled"

# Connectivity monitoring
RNSD_RECOVERED = "connectivity.rnsd_recovered"
RNSD_RESTARTING = "connectivity.rnsd_restarting"

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

# MeshCore gateway
MESHCORE_CONNECTED = "meshcore.connected"
MESHCORE_DISCONNECTED = "meshcore.disconnected"
MESHCORE_CONNECT_FAILED = "meshcore.connect_failed"
MESHCORE_MESSAGE_RECEIVED = "meshcore.message_received"
MESHCORE_MESSAGE_SENT = "meshcore.message_sent"

# GPS telemetry
GPS_DEVICE_CONNECTED = "gps.device_connected"
GPS_DEVICE_DISCONNECTED = "gps.device_disconnected"
GPS_FIX_RECEIVED = "gps.fix_received"
GPS_FIX_UPDATED = "gps.fix_updated"
GPS_FIX_LOST = "gps.fix_lost"

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
MESSAGE_STATUS_CHANGED = "messaging.message_status_changed"
CONVERSATION_DELETED = "messaging.conversation_deleted"

# Meshtastic firmware watchdog
MESHTASTIC_FIRMWARE_HANG = "meshtastic.firmware_hang"
MESHTASTIC_FIRMWARE_RECOVERED = "meshtastic.firmware_recovered"

# Meshtastic emoji reactions
MESHTASTIC_REACTION_RECEIVED = "meshtastic.reaction_received"

# Meshtastic DM read receipts (PRIVATE_APP portnum)
MESHTASTIC_READ_RECEIPT_RECEIVED = "meshtastic.read_receipt_received"

# Messaging hub — reactions
MESSAGE_REACTION_RECEIVED = "messaging.reaction_received"

# MeshCore message ack
MESHCORE_MESSAGE_ACKED = "meshcore.message_acked"

# MeshCore observer
MESHCORE_OBSERVER_DEVICE_CONNECTED = "meshcore_observer.device_connected"
MESHCORE_OBSERVER_DEVICE_DISCONNECTED = "meshcore_observer.device_disconnected"
MESHCORE_OBSERVER_CONNECT_FAILED = "meshcore_observer.connect_failed"
MESHCORE_OBSERVER_MQTT_CONNECTED = "meshcore_observer.mqtt_connected"
MESHCORE_OBSERVER_MQTT_DISCONNECTED = "meshcore_observer.mqtt_disconnected"

# Space tracker — satellites, launches, space weather
SPACE_TLE_UPDATED = "space.tle.updated"
SPACE_POSITIONS_SNAPSHOT = "space.positions.snapshot"
SPACE_PASS_UPCOMING = "space.pass.upcoming"
SPACE_LAUNCH_UPCOMING = "space.launch.upcoming"
SPACE_WEATHER_UPDATED = "space.weather.updated"

# Spectrum scanner — RTL-SDR sweep-based waterfall / spectrum
SPECTRUM_SWEEP = "spectrum.sweep"  # one complete sweep ready
SPECTRUM_STATUS = "spectrum.status"  # status change (running/error/unavailable)
SPECTRUM_PRESET_SWITCHING = "spectrum.preset_switching"
SPECTRUM_PRESET_ACTIVE = "spectrum.preset_active"

# LoRa scanner — dedicated RTL-SDR LoRa band sweep
LORA_SCANNER_SWEEP = "lora_scanner.sweep"
LORA_SCANNER_STATUS = "lora_scanner.status"

# LoRa scanner — sweep-triggered I/Q capture request
LORA_CAPTURE_TRIGGER = "lora.capture_trigger"

# ADS-B radar — aircraft tracking via dump1090
ADSB_AIRCRAFT_DETECTED = "adsb.aircraft_detected"
ADSB_AIRCRAFT_LOST = "adsb.aircraft_lost"
ADSB_EMERGENCY_SQUAWK = "adsb.emergency_squawk"
ADSB_STATUS = "adsb.status"
ADSB_WEDGE_DETECTED = "adsb.wedge_detected"
ADSB_EXHAUSTED = "adsb.exhausted"
ADSB_RECOVERED = "adsb.recovered"
ADSB_DEGRADED = "adsb.degraded"
ADSB_HEALTHY = "adsb.healthy"

# LoRa link tester — dedicated-radio probe/ACK measurements
LINK_TEST_STARTED = "link_test.started"
LINK_TEST_STOPPED = "link_test.stopped"
LINK_TEST_PROBE_RESULT = "link_test.probe_result"
LINK_TEST_CONNECTION_CHANGED = "link_test.connection_changed"

# NTP synchronization
NTP_SYNC_ACQUIRED = "ntp.sync_acquired"
NTP_SYNC_LOST = "ntp.sync_lost"
NTP_STATUS_UPDATED = "ntp.status_updated"
NTP_GPS_REFCLOCK_ACTIVE = "ntp.gps_refclock_active"

# FM/AM radio receiver
FM_RECEIVER_TUNED = "fm_receiver.tuned"
FM_RECEIVER_STATUS = "fm_receiver.status"
FM_RECEIVER_RECORDING_STARTED = "fm_receiver.recording_started"
FM_RECEIVER_RECORDING_STOPPED = "fm_receiver.recording_stopped"

# SDR scheduler — dongle time-sharing
SDR_DONGLE_GRANTED = "sdr.dongle.granted"
SDR_DONGLE_YIELDED = "sdr.dongle.yielded"
SDR_SCHEDULE_UPDATED = "sdr.schedule.updated"

# NOAA APT weather satellite decoder
NOAA_APT_CAPTURE_START = "noaa_apt.capture_start"
NOAA_APT_CAPTURE_DONE = "noaa_apt.capture_done"
NOAA_APT_DECODE_COMPLETE = "noaa_apt.decode_complete"
NOAA_APT_STATUS = "noaa_apt.status"

# Radiosonde tracker
RADIOSONDE_DETECTED = "radiosonde.detected"
RADIOSONDE_BURST = "radiosonde.burst"
RADIOSONDE_LOST = "radiosonde.lost"
RADIOSONDE_STATUS = "radiosonde.status"

# AIS marine vessel receiver
AIS_VESSEL_DETECTED = "ais.vessel_detected"
AIS_VESSEL_LOST = "ais.vessel_lost"
AIS_STATUS = "ais.status"

# ACARS aircraft message decoder
ACARS_MESSAGE_DECODED = "acars.message_decoded"
ACARS_STATUS = "acars.status"

# NOAA Weather Radio SAME alert monitor
WEATHER_ALERT_RECEIVED = "weather_alert.received"
WEATHER_ALERT_SEVERE = "weather_alert.severe"
WEATHER_ALERT_EXPIRED = "weather_alert.expired"
WEATHER_ALERT_STATUS = "weather_alert.status"

# ISM band decoder (rtl_433)
ISM_DEVICE_DETECTED = "ism.device_detected"
ISM_DEVICE_LOST = "ism.device_lost"
ISM_STATUS = "ism.status"

# Captive portal
CAPTIVE_PORTAL_ACTIVATED = "captive_portal.activated"
CAPTIVE_PORTAL_DEACTIVATED = "captive_portal.deactivated"

# NomadNet subprocess health
NOMADNET_CPU_RUNAWAY = "nomadnet.cpu_runaway"
