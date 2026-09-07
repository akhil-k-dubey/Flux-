"""
flux/nervous_system/perception_loop.py

Flux's perception loop — continuously samples the environment and writes
signals to the WorldModel. Runs as a daemon thread, starts automatically.

Sources (today, on laptop):
  - cpu_temp:          every 1s   (psutil.sensors_temperatures)
  - ram_used:          every 2s   (psutil.virtual_memory)
  - battery:           every 5s   (psutil.sensors_battery)
  - uptime:            every 10s  (psutil.boot_time)
  - rsm_queue_size:    every 30s  (reads L3 queue file)
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

try:
    import psutil
    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False
    log.warning("PerceptionLoop: psutil not available — hardware signals disabled")


class PerceptionLoop:
    """
    Background daemon thread that polls signal sources and writes to WorldModel.
    Does NOT start automatically — call .start() explicitly.
    """

    def __init__(self, world_model) -> None:
        self._wm       = world_model
        self._running  = False
        self._thread:  threading.Thread | None = None
        self._last:    dict[str, float] = {}   # source → last poll time

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread  = threading.Thread(
            target=self._loop, daemon=True, name="PerceptionLoop"
        )
        self._thread.start()
        log.info("PerceptionLoop: started")

    def stop(self) -> None:
        self._running = False

    # ── Main loop ─────────────────────────────────────────────────────────────

    def _loop(self) -> None:
        while self._running:
            now = time.time()
            try:
                self._tick_cpu_temp(now)
                self._tick_ram(now)
                self._tick_battery(now)
                self._tick_uptime(now)
                self._tick_rsm_queue(now)
                self._wm.update("physical.laptop.last_seen", now)
            except Exception as exc:
                log.debug("PerceptionLoop tick error: %s", exc)
            time.sleep(1.0)   # base tick: 1 second

    # ── Signal samplers ───────────────────────────────────────────────────────

    def _due(self, key: str, interval: float, now: float) -> bool:
        """True if it's time to sample this source again."""
        if now - self._last.get(key, 0) >= interval:
            self._last[key] = now
            return True
        return False

    def _tick_cpu_temp(self, now: float) -> None:
        if not _HAS_PSUTIL or not self._due("cpu_temp", 1.0, now):
            return
        try:
            temps = psutil.sensors_temperatures()
            if not temps:
                return
            # Try common keys: coretemp, k10temp, acpitz
            for key in ("coretemp", "k10temp", "acpitz", "cpu_thermal"):
                if key in temps:
                    t = temps[key][0].current
                    self._wm.update("physical.laptop.cpu_temp_c", round(t, 1))
                    return
            # Fallback: first available sensor
            first_key = next(iter(temps))
            t = temps[first_key][0].current
            self._wm.update("physical.laptop.cpu_temp_c", round(t, 1))
        except Exception:
            pass

    def _tick_ram(self, now: float) -> None:
        if not _HAS_PSUTIL or not self._due("ram", 2.0, now):
            return
        try:
            mem = psutil.virtual_memory()
            self._wm.update("physical.laptop.ram_used_mb",  round(mem.used / 1024**2, 1))
            self._wm.update("physical.laptop.ram_total_mb", round(mem.total / 1024**2, 1))
        except Exception:
            pass

    def _tick_battery(self, now: float) -> None:
        if not _HAS_PSUTIL or not self._due("battery", 5.0, now):
            return
        try:
            bat = psutil.sensors_battery()
            if bat:
                self._wm.update("physical.laptop.battery_pct",      round(bat.percent, 1))
                self._wm.update("physical.laptop.battery_charging",  bat.power_plugged)
        except Exception:
            pass

    def _tick_uptime(self, now: float) -> None:
        if not _HAS_PSUTIL or not self._due("uptime", 10.0, now):
            return
        try:
            boot_time = psutil.boot_time()
            uptime_h  = (now - boot_time) / 3600
            self._wm.update("physical.laptop.uptime_hours", round(uptime_h, 2))
        except Exception:
            pass

    def _tick_rsm_queue(self, now: float) -> None:
        if not self._due("rsm_queue", 30.0, now):
            return
        try:
            queue_path = Path("logs/layer3_queue.jsonl")
            if queue_path.exists():
                import json
                proposals = []
                for line in queue_path.read_text().splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    if rec.get("event") == "CREATED":
                        proposals.append({
                            "id":          rec.get("proposal_id", ""),
                            "change_type": rec.get("change_type", ""),
                            "hypothesis":  rec.get("hypothesis", "")[:80],
                        })
                self._wm.update("processes.active_rsm_proposals", proposals)
        except Exception:
            pass
