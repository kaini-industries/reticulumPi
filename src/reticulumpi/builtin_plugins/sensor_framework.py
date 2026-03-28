"""Sensor Data Framework plugin — config-driven sensor reading with logging and mesh broadcast."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from typing import Any

import RNS
import RNS.vendor.umsgpack as umsgpack

from reticulumpi import events
from reticulumpi.plugin_base import PluginBase


class SensorDriver:
    """Base class for sensor drivers.

    Subclasses must implement ``read()`` which returns a dict of named readings,
    e.g. ``{"temperature": 22.5, "humidity": 65.0}``.
    """

    driver_name: str = "base"

    def __init__(self, sensor_config: dict[str, Any]) -> None:
        self.config = sensor_config

    def read(self) -> dict[str, Any]:
        raise NotImplementedError

    def close(self) -> None:
        pass


class DS18B20Driver(SensorDriver):
    """Dallas 1-Wire temperature sensor (DS18B20).

    Reads from ``/sys/bus/w1/devices/<address>/temperature`` (kernel w1-therm).
    """

    driver_name = "ds18b20"

    def __init__(self, sensor_config: dict[str, Any]) -> None:
        super().__init__(sensor_config)
        self._address = sensor_config.get("address", "")
        self._path = f"/sys/bus/w1/devices/{self._address}/temperature"

    def read(self) -> dict[str, Any]:
        try:
            with open(self._path, "r") as f:
                raw = f.read().strip()
            temp_c = int(raw) / 1000.0
            return {"temperature": round(temp_c, 2)}
        except FileNotFoundError:
            return {"error": f"sensor not found: {self._address}"}
        except (ValueError, OSError) as e:
            return {"error": str(e)}


class BME280Driver(SensorDriver):
    """Bosch BME280 I2C temperature/humidity/pressure sensor.

    Requires ``smbus2`` package (install with ``pip install smbus2``).
    """

    driver_name = "bme280"

    def __init__(self, sensor_config: dict[str, Any]) -> None:
        super().__init__(sensor_config)
        self._bus_num = sensor_config.get("bus", 1)
        self._address = sensor_config.get("i2c_address", 0x76)
        self._bus = None

    def read(self) -> dict[str, Any]:
        try:
            if self._bus is None:
                import smbus2
                self._bus = smbus2.SMBus(self._bus_num)

            # Read raw compensation data and measurements
            # Simplified BME280 read — full driver would parse compensation registers
            # For production use, consider the bme280 pip package
            calib = self._bus.read_i2c_block_data(self._address, 0x88, 26)
            self._bus.write_byte_data(self._address, 0xF2, 0x01)  # humidity oversampling x1
            self._bus.write_byte_data(self._address, 0xF4, 0x27)  # temp+pressure oversampling, normal mode
            self._bus.write_byte_data(self._address, 0xF5, 0xA0)  # config
            time.sleep(0.05)
            data = self._bus.read_i2c_block_data(self._address, 0xF7, 8)

            # Parse temperature (simplified, 20-bit)
            raw_temp = ((data[3] << 16) | (data[4] << 8) | data[5]) >> 4
            dig_t1 = calib[0] | (calib[1] << 8)
            dig_t2 = self._to_signed(calib[2] | (calib[3] << 8))
            dig_t3 = self._to_signed(calib[4] | (calib[5] << 8))

            var1 = ((raw_temp / 16384.0) - (dig_t1 / 1024.0)) * dig_t2
            var2 = (((raw_temp / 131072.0) - (dig_t1 / 8192.0)) ** 2) * dig_t3
            t_fine = var1 + var2
            temperature = round(t_fine / 5120.0, 2)

            # Parse pressure (simplified, 20-bit)
            raw_press = ((data[0] << 16) | (data[1] << 8) | data[2]) >> 4
            dig_p1 = calib[6] | (calib[7] << 8)
            dig_p2 = self._to_signed(calib[8] | (calib[9] << 8))
            v1 = (t_fine / 2.0) - 64000.0
            v2 = v1 * v1 * dig_p2 / 32768.0
            v2 = v2 + v1 * (self._to_signed(calib[10] | (calib[11] << 8))) * 2.0
            v2 = (v2 / 4.0) + (self._to_signed(calib[12] | (calib[13] << 8)) * 65536.0)
            v1 = ((self._to_signed(calib[14] | (calib[15] << 8))) * v1 * v1 / 524288.0
                   + (self._to_signed(calib[16] | (calib[17] << 8))) * v1) / 524288.0
            v1 = (1.0 + v1 / 32768.0) * dig_p1
            pressure = 0.0
            if v1 != 0:
                pressure = 1048576.0 - raw_press
                pressure = (pressure - (v2 / 4096.0)) * 6250.0 / v1
            pressure = round(pressure / 100.0, 2)  # hPa

            # Parse humidity (simplified)
            raw_hum = (data[6] << 8) | data[7]
            calib_h = self._bus.read_i2c_block_data(self._address, 0xE1, 7)
            dig_h2 = self._to_signed(calib_h[1] | (calib_h[2] << 8))
            dig_h4 = self._to_signed((calib_h[3] << 4) | (calib_h[4] & 0x0F))
            dig_h5 = self._to_signed((calib_h[5] << 4) | ((calib_h[4] >> 4) & 0x0F))
            h = t_fine - 76800.0
            if h != 0:
                h = (raw_hum - (dig_h4 * 64.0 + (dig_h5 / 16384.0) * h)) * (
                    dig_h2 / 65536.0 * (1.0 + (self._to_signed(calib_h[6]) / 67108864.0) * h * (
                        1.0 + (self._to_signed((calib_h[3] << 4) | 0) / 67108864.0) * h)))
            humidity = max(0.0, min(100.0, round(h, 2))) if h else 0.0

            return {
                "temperature": temperature,
                "pressure": pressure,
                "humidity": humidity,
            }
        except ImportError:
            return {"error": "smbus2 not installed"}
        except Exception as e:
            return {"error": str(e)}

    @staticmethod
    def _to_signed(val: int) -> int:
        return val - 65536 if val >= 32768 else val

    def close(self) -> None:
        if self._bus is not None:
            try:
                self._bus.close()
            except Exception:
                pass
            self._bus = None


class ADCDriver(SensorDriver):
    """Generic ADC reading via sysfs (e.g. IIO subsystem).

    Reads from a configurable sysfs path. Applies optional scale and offset.
    """

    driver_name = "adc"

    def __init__(self, sensor_config: dict[str, Any]) -> None:
        super().__init__(sensor_config)
        self._path = sensor_config.get("sysfs_path", "")
        self._scale = sensor_config.get("scale", 1.0)
        self._offset = sensor_config.get("offset", 0.0)
        self._name = sensor_config.get("reading_name", "value")

    def read(self) -> dict[str, Any]:
        try:
            with open(self._path, "r") as f:
                raw = float(f.read().strip())
            return {self._name: round(raw * self._scale + self._offset, 4)}
        except FileNotFoundError:
            return {"error": f"sysfs path not found: {self._path}"}
        except (ValueError, OSError) as e:
            return {"error": str(e)}


class CommandDriver(SensorDriver):
    """Reads sensor data by executing a shell command.

    The command should print a single numeric value to stdout.
    """

    driver_name = "command"

    def __init__(self, sensor_config: dict[str, Any]) -> None:
        super().__init__(sensor_config)
        self._command = sensor_config.get("command", "")
        self._name = sensor_config.get("reading_name", "value")

    def read(self) -> dict[str, Any]:
        if not self._command:
            return {"error": "no command configured"}
        try:
            import subprocess
            result = subprocess.run(
                self._command, shell=True, capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0:
                return {"error": f"command failed: {result.stderr.strip()[:100]}"}
            return {self._name: float(result.stdout.strip())}
        except (ValueError, subprocess.TimeoutExpired, OSError) as e:
            return {"error": str(e)}


# Driver registry
DRIVERS: dict[str, type[SensorDriver]] = {
    "ds18b20": DS18B20Driver,
    "bme280": BME280Driver,
    "adc": ADCDriver,
    "command": CommandDriver,
}


class SensorFrameworkPlugin(PluginBase):
    """Config-driven sensor reading with SQLite logging and mesh broadcast.

    Supports multiple sensor types (DS18B20, BME280, ADC, command) via
    a driver abstraction. Readings are stored in SQLite, published on the
    event bus, and optionally broadcast over Reticulum announces.
    """

    plugin_name = "sensor_framework"
    plugin_version = "1.0.0"
    plugin_description = "Config-driven sensor reading, logging, and mesh broadcast"

    def validate_config(self) -> None:
        sensors = self.config.get("sensors", [])
        if not isinstance(sensors, list):
            raise ValueError("sensors must be a list")
        for i, sensor in enumerate(sensors):
            if not isinstance(sensor, dict):
                raise ValueError(f"sensor #{i} must be a dict")
            if "name" not in sensor:
                raise ValueError(f"sensor #{i} missing 'name'")
            if "driver" not in sensor:
                raise ValueError(f"sensor #{i} missing 'driver'")
            driver_name = sensor["driver"]
            if driver_name not in DRIVERS:
                raise ValueError(
                    f"sensor #{i}: unknown driver '{driver_name}' "
                    f"(available: {', '.join(sorted(DRIVERS))})"
                )

        interval = self.config.get("read_interval", 60)
        if not isinstance(interval, (int, float)) or interval < 1:
            raise ValueError("read_interval must be >= 1 second")

        storage = self.config.get("storage", {})
        if not isinstance(storage, dict):
            raise ValueError("storage must be a dict")
        storage_type = storage.get("type", "sqlite")
        if storage_type not in ("sqlite", "csv", "none"):
            raise ValueError(f"storage.type must be sqlite, csv, or none (got '{storage_type}')")

    def start(self) -> None:
        self._active = True
        self._readings_count = 0
        self._last_readings: dict[str, dict[str, Any]] = {}
        self._drivers: list[tuple[dict[str, Any], SensorDriver]] = []
        self._db_lock = threading.Lock()

        # Initialize drivers
        for sensor_cfg in self.config.get("sensors", []):
            driver_cls = DRIVERS[sensor_cfg["driver"]]
            driver = driver_cls(sensor_cfg)
            self._drivers.append((sensor_cfg, driver))

        # Initialize storage
        self._db: sqlite3.Connection | None = None
        storage = self.config.get("storage", {})
        storage_type = storage.get("type", "sqlite")
        if storage_type == "sqlite":
            db_path = os.path.expanduser(
                storage.get("path", "~/.local/share/reticulumpi/sensor_data.db")
            )
            os.makedirs(os.path.dirname(db_path), exist_ok=True)
            self._db = sqlite3.connect(db_path, check_same_thread=False)
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS sensor_readings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sensor_name TEXT NOT NULL,
                    reading_name TEXT NOT NULL,
                    value REAL NOT NULL,
                    timestamp REAL NOT NULL
                )
            """)
            self._db.execute("""
                CREATE INDEX IF NOT EXISTS idx_readings_sensor_time
                ON sensor_readings(sensor_name, timestamp)
            """)
            self._db.commit()
        elif storage_type == "csv":
            self._csv_path = os.path.expanduser(
                storage.get("path", "~/.local/share/reticulumpi/sensor_data.csv")
            )
            os.makedirs(os.path.dirname(self._csv_path), exist_ok=True)

        # Initialize broadcast destination if enabled
        self._broadcast_dest = None
        broadcast = self.config.get("broadcast", {})
        if broadcast.get("enabled", False):
            self._broadcast_dest = RNS.Destination(
                self.identity,
                RNS.Destination.IN,
                RNS.Destination.SINGLE,
                "reticulumpi",
                "node",
                "sensors",
            )

        # Start read loop
        self._start_thread(self._read_loop, "sensor-reader")

        # Start pruning loop if retention is configured
        retention = self.config.get("storage", {}).get("retention_days", 30)
        if retention and self._db:
            self._start_thread(self._prune_loop, "sensor-pruner")

        self.log.info(
            "Sensor framework active (%d sensors, storage=%s)",
            len(self._drivers),
            storage_type,
        )

    def stop(self) -> None:
        self._active = False
        # Wait for threads to finish before closing resources
        self._join_threads()
        # Close drivers
        for _, driver in self._drivers:
            try:
                driver.close()
            except Exception:
                pass
        self._drivers.clear()
        # Close DB
        if self._db:
            try:
                self._db.close()
            except Exception:
                pass
            self._db = None

    def get_status(self) -> dict[str, Any]:
        with self._db_lock:
            return {
                "active": self._active,
                "sensor_count": len(self._drivers),
                "readings_total": self._readings_count,
                "last_readings": dict(self._last_readings),
            }

    def get_latest_readings(self) -> dict[str, dict[str, Any]]:
        """Return the most recent reading from each sensor."""
        with self._db_lock:
            return dict(self._last_readings)

    def get_sensor_history(
        self, sensor_name: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Return recent readings for a sensor from SQLite."""
        if not self._db:
            return []
        try:
            with self._db_lock:
                cursor = self._db.execute(
                    "SELECT reading_name, value, timestamp FROM sensor_readings "
                    "WHERE sensor_name = ? ORDER BY timestamp DESC LIMIT ?",
                    (sensor_name, limit),
                )
                return [
                    {"reading": row[0], "value": row[1], "timestamp": row[2]}
                    for row in cursor.fetchall()
                ]
        except Exception:
            self.log.debug("Error reading sensor history", exc_info=True)
            return []

    def _read_loop(self) -> None:
        interval = self.config.get("read_interval", 60)
        broadcast_cfg = self.config.get("broadcast", {})
        broadcast_interval = broadcast_cfg.get("interval", 300)
        last_broadcast = 0.0

        while self._active:
            now = time.time()
            all_readings: dict[str, dict[str, Any]] = {}

            for sensor_cfg, driver in self._drivers:
                sensor_name = sensor_cfg["name"]
                try:
                    reading = driver.read()
                    reading["timestamp"] = now
                    all_readings[sensor_name] = reading
                    with self._db_lock:
                        self._last_readings[sensor_name] = reading

                    # Store reading
                    if "error" not in reading:
                        self._store_reading(sensor_name, reading, now)
                        with self._db_lock:
                            self._readings_count += 1

                    # Publish event
                    self.event_bus.publish(events.SENSOR_READING, {
                        "sensor": sensor_name,
                        "driver": sensor_cfg["driver"],
                        "reading": reading,
                    })
                except Exception:
                    self.log.exception("Error reading sensor '%s'", sensor_name)
                    with self._db_lock:
                        self._last_readings[sensor_name] = {"error": "read failed", "timestamp": now}

            # Broadcast if interval elapsed
            if (
                self._broadcast_dest
                and broadcast_cfg.get("enabled", False)
                and now - last_broadcast >= broadcast_interval
                and all_readings
            ):
                self._broadcast_readings(all_readings)
                last_broadcast = now

            self._sleep_while_active(interval)

    def _store_reading(
        self, sensor_name: str, reading: dict[str, Any], timestamp: float
    ) -> None:
        storage_type = self.config.get("storage", {}).get("type", "sqlite")

        if storage_type == "sqlite" and self._db:
            try:
                rows = [
                    (sensor_name, key, float(val), timestamp)
                    for key, val in reading.items()
                    if key != "timestamp" and isinstance(val, (int, float))
                ]
                if rows:
                    with self._db_lock:
                        self._db.executemany(
                            "INSERT INTO sensor_readings (sensor_name, reading_name, value, timestamp) "
                            "VALUES (?, ?, ?, ?)",
                            rows,
                        )
                        self._db.commit()
            except Exception:
                self.log.debug("Error storing sensor reading", exc_info=True)

        elif storage_type == "csv" and hasattr(self, "_csv_path"):
            try:
                import csv
                with self._db_lock:
                    file_exists = os.path.isfile(self._csv_path)
                    with open(self._csv_path, "a", newline="") as f:
                        writer = csv.writer(f)
                        if not file_exists:
                            writer.writerow(["timestamp", "sensor", "reading", "value"])
                        for key, val in reading.items():
                            if key != "timestamp" and isinstance(val, (int, float)):
                                writer.writerow([timestamp, sensor_name, key, val])
            except Exception:
                self.log.debug("Error writing CSV", exc_info=True)

    def _broadcast_readings(self, readings: dict[str, dict[str, Any]]) -> None:
        """Announce sensor readings over Reticulum."""
        try:
            # Build compact payload
            payload: dict[str, Any] = {
                "name": self.app.node_name,
                "t": time.time(),
                "sensors": {},
            }
            for sensor_name, reading in readings.items():
                if "error" not in reading:
                    clean = {
                        k: v for k, v in reading.items()
                        if k != "timestamp" and isinstance(v, (int, float))
                    }
                    if clean:
                        payload["sensors"][sensor_name] = clean

            if payload["sensors"]:
                self._broadcast_dest.announce(app_data=umsgpack.packb(payload))
                self.log.debug("Sensor readings broadcast")
        except Exception:
            self.log.debug("Error broadcasting sensor readings", exc_info=True)

    def _prune_loop(self) -> None:
        """Periodically remove old sensor readings from SQLite."""
        retention_days = self.config.get("storage", {}).get("retention_days", 30)
        while self._active:
            self._sleep_while_active(3600)  # Check hourly
            if not self._active or not self._db:
                break
            try:
                cutoff = time.time() - (retention_days * 86400)
                with self._db_lock:
                    self._db.execute(
                        "DELETE FROM sensor_readings WHERE timestamp < ?", (cutoff,)
                    )
                    self._db.commit()
                self.log.debug("Pruned sensor readings older than %d days", retention_days)
            except Exception:
                self.log.debug("Error pruning sensor data", exc_info=True)
