"""
flux/sensors/system_monitor.py

Hardware sensor stream — CPU, RAM, disk, network.
Grounds system-state concepts (thermal_throttling, memory_pressure, etc.)
in real physical measurements.
"""
from __future__ import annotations

try:
    import psutil
    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False


def snapshot() -> dict:
    if not _HAS_PSUTIL:
        return {"available": False}

    cpu_temps: dict = {}
    try:
        temps = psutil.sensors_temperatures()
        if temps:
            for name, entries in temps.items():
                cpu_temps[name] = round(entries[0].current, 1) if entries else None
    except (AttributeError, NotImplementedError):
        pass  # Windows / some Linux configs

    mem  = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    net  = psutil.net_io_counters()

    return {
        "available":       True,
        "cpu_percent":     psutil.cpu_percent(interval=None),
        "cpu_temps":       cpu_temps,
        "ram_used_mb":     round(mem.used / 1e6, 1),
        "ram_percent":     mem.percent,
        "disk_used_gb":    round(disk.used / 1e9, 2),
        "disk_percent":    disk.percent,
        "net_bytes_sent":  net.bytes_sent,
        "net_bytes_recv":  net.bytes_recv,
    }
