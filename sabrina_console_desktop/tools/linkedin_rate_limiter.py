# -*- coding: utf-8 -*-
"""
tools/linkedin_rate_limiter.py
==============================
Anti-flagging rate limiter and quota management for Sabrina's LinkedIn operations.

Enforces:
1. Hard daily action quotas persisted across restarts in data/linkedin_rate_limit.json.
2. Randomized Gaussian/Poisson jitter delays between actions to avoid bot-like cadence.
3. Natural operating hour constraints (active during day, sleeping at night).
4. Circuit breaker: auto-trips if a CAPTCHA or checkpoint is detected, freezing operations.
"""

import os
import json
import time
import random
import datetime
import threading
from typing import Dict, Any, Tuple, Optional

DEFAULT_RATE_LIMIT_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "linkedin_rate_limit.json")

# Default daily caps (conservative to stay well under LinkedIn heuristic thresholds)
DEFAULT_DAILY_LIMITS = {
    "profile_view": 35,
    "connection_request": 12,
    "direct_message": 40,
    "feed_post": 2,
    "feed_like": 15,
    "feed_comment": 10,
    "inbox_check": 80,
    "post_delete": 15,
    "job_search": 999999,
    "job_apply": 999999,
}

# Base delay ranges in seconds (min, max) between operations of that type
ACTION_DELAYS = {
    "profile_view": (6.0, 14.0),
    "connection_request": (20.0, 45.0),
    "direct_message": (6.0, 14.0),
    "feed_post": (30.0, 60.0),
    "feed_like": (8.0, 18.0),
    "feed_comment": (25.0, 50.0),
    "inbox_check": (10.0, 22.0),
    "post_delete": (15.0, 35.0),
    "job_search": (10.0, 22.0),
    "job_apply": (35.0, 75.0),
    "default": (8.0, 18.0)
}


class LinkedInRateLimiter:
    _instance: Optional["LinkedInRateLimiter"] = None

    def __init__(self, data_file: str = DEFAULT_RATE_LIMIT_FILE):
        self.data_file = data_file
        self._lock = threading.RLock()
        self._ensure_dir()
        self.state = self._load_state()

    @classmethod
    def get_instance(cls, data_file: str = DEFAULT_RATE_LIMIT_FILE) -> "LinkedInRateLimiter":
        if cls._instance is None:
            cls._instance = LinkedInRateLimiter(data_file)
        return cls._instance

    def _ensure_dir(self):
        directory = os.path.dirname(self.data_file)
        if directory and not os.path.exists(directory):
            try:
                os.makedirs(directory, exist_ok=True)
            except Exception:
                pass

    def _get_today_str(self) -> str:
        return datetime.date.today().isoformat()

    def _load_state(self) -> Dict[str, Any]:
        with self._lock:
            today = self._get_today_str()
            default_state = {
                "date": today,
                "counts": {k: 0 for k in DEFAULT_DAILY_LIMITS},
                "limits": dict(DEFAULT_DAILY_LIMITS),
                "circuit_breaker": {
                    "tripped": False,
                    "reason": "",
                    "tripped_at": 0.0,
                    "cooldown_until": 0.0
                },
                "last_action_ts": 0.0,
                "history": []
            }

            if not os.path.isfile(self.data_file):
                return default_state

            try:
                with open(self.data_file, "r", encoding="utf-8") as f:
                    data = json.load(f)

                # Reset daily counters if date changed
                if data.get("date") != today:
                    data["date"] = today
                    data["counts"] = {k: 0 for k in DEFAULT_DAILY_LIMITS}
                    if "history" not in data:
                        data["history"] = []
                
                # Ensure all limit keys exist
                counts = data.get("counts", {})
                for k in DEFAULT_DAILY_LIMITS:
                    if k not in counts:
                        counts[k] = 0
                data["counts"] = counts

                if "circuit_breaker" not in data:
                    data["circuit_breaker"] = default_state["circuit_breaker"]

                return data
            except Exception:
                return default_state

    def _save_state(self):
        with self._lock:
            self._ensure_dir()
            tmp_file = f"{self.data_file}.tmp.{os.getpid()}"
            try:
                with open(tmp_file, "w", encoding="utf-8") as f:
                    json.dump(self.state, f, indent=2)
                if os.path.exists(self.data_file):
                    os.replace(tmp_file, self.data_file)
                else:
                    os.rename(tmp_file, self.data_file)
            except Exception:
                if os.path.exists(tmp_file):
                    try:
                        os.remove(tmp_file)
                    except Exception:
                        pass

    def trip_circuit_breaker(self, reason: str, cooldown_hours: float = 24.0):
        """Immediately locks down all automated actions when a checkpoint or ban alert occurs."""
        with self._lock:
            now = time.time()
            self.state["circuit_breaker"] = {
                "tripped": True,
                "reason": reason,
                "tripped_at": now,
                "cooldown_until": now + (cooldown_hours * 3600.0)
            }
            self._save_state()

    def reset_circuit_breaker(self):
        """Manual reset of circuit breaker."""
        with self._lock:
            self.state["circuit_breaker"] = {
                "tripped": False,
                "reason": "",
                "tripped_at": 0.0,
                "cooldown_until": 0.0
            }
            self._save_state()

    def is_circuit_breaker_active(self) -> Tuple[bool, str]:
        """Check if circuit breaker is engaged."""
        with self._lock:
            cb = self.state.get("circuit_breaker", {})
            if not cb.get("tripped"):
                return False, ""
            
            until = cb.get("cooldown_until", 0.0)
            now = time.time()
            if now < until:
                rem_hours = round((until - now) / 3600.0, 1)
                return True, f"Circuit breaker ACTIVE ({cb.get('reason')}). Cooldown remaining: {rem_hours} hours."
            
            # Cooldown expired, reset
            self.reset_circuit_breaker()
            return False, ""

    def is_within_operating_hours(self, start_hour: int = 8, end_hour: int = 21) -> Tuple[bool, str]:
        """Returns True if current local time is within normal human activity hours (or 24/7 boost mode)."""
        if os.environ.get("SABRINA_LINKEDIN_24_7", "").strip().lower() in ("1", "true", "yes"):
            return True, "24/7 boost mode enabled"
        try:
            start_hour = int(os.environ.get("SABRINA_LINKEDIN_START_HOUR", start_hour))
            end_hour = int(os.environ.get("SABRINA_LINKEDIN_END_HOUR", end_hour))
        except (TypeError, ValueError):
            pass
        now = datetime.datetime.now()
        current_hour = now.hour
        if start_hour <= current_hour < end_hour:
            return True, ""
        return False, f"Outside operating hours ({start_hour}:00 - {end_hour}:00). Current time is {now.strftime('%H:%M')}."

    def check_quota(self, action_type: str) -> Tuple[bool, str]:
        """Check if action is permitted under daily quota and circuit breaker rules."""
        with self._lock:
            # 1. Circuit breaker
            tripped, reason = self.is_circuit_breaker_active()
            if tripped:
                return False, f"BLOCKED: {reason}"

            # 2. Daily date turnover
            today = self._get_today_str()
            if self.state.get("date") != today:
                self.state["date"] = today
                self.state["counts"] = {k: 0 for k in DEFAULT_DAILY_LIMITS}

            # 3. Quota check (job_apply and job_search are uncapped)
            limits = self.state.get("limits", DEFAULT_DAILY_LIMITS)
            counts = self.state.get("counts", {})
            count = counts.get(action_type, 0)
            if action_type in ("job_apply", "job_search"):
                return True, f"OK ({count}/unlimited used today)"

            limit = limits.get(action_type, DEFAULT_DAILY_LIMITS.get(action_type, 10))

            if count >= limit:
                return False, f"BLOCKED: Daily limit for '{action_type}' reached ({count}/{limit}). Resets tomorrow."

            return True, f"OK ({count}/{limit} used today)"

    def wait_jitter(self, action_type: str):
        """Sleeps for a randomized duration to enforce human-like pacing."""
        min_sec, max_sec = ACTION_DELAYS.get(action_type, ACTION_DELAYS["default"])
        # Use triangular distribution centered towards lower-middle to mimic human variation
        delay = random.triangular(min_sec, max_sec, min_sec + (max_sec - min_sec) * 0.35)
        time.sleep(delay)

    def record_action(self, action_type: str, details: Optional[Dict[str, Any]] = None):
        """Records a successful action and increments daily counter."""
        with self._lock:
            today = self._get_today_str()
            if self.state.get("date") != today:
                self.state["date"] = today
                self.state["counts"] = {k: 0 for k in DEFAULT_DAILY_LIMITS}

            counts = self.state.setdefault("counts", {})
            counts[action_type] = counts.get(action_type, 0) + 1
            self.state["last_action_ts"] = time.time()

            entry = {
                "timestamp": datetime.datetime.now().isoformat(),
                "action": action_type,
                "details": details or {}
            }
            history = self.state.setdefault("history", [])
            history.append(entry)
            if len(history) > 100:
                self.state["history"] = history[-100:]

            self._save_state()

    def record_observation(self, post_key: str, decision: Dict[str, Any]):
        """Remember evaluated feed cards without consuming an engagement quota."""
        with self._lock:
            observations = self.state.setdefault("observations", [])
            observations.append({"timestamp_epoch": time.time(), "post_key": post_key,
                                 "decision": decision})
            cutoff = time.time() - (7 * 86400)
            self.state["observations"] = [item for item in observations
                                          if item.get("timestamp_epoch", 0) >= cutoff][-500:]
            self._save_state()

    def was_observed(self, post_key: str, within_hours: float = 12.0) -> bool:
        cutoff = time.time() - max(0.1, float(within_hours)) * 3600
        with self._lock:
            return any(item.get("post_key") == post_key and item.get("timestamp_epoch", 0) >= cutoff
                       for item in self.state.get("observations", []))

    def get_status_summary(self) -> Dict[str, Any]:
        """Returns structured summary of quotas, circuit breaker status, and today's usage."""
        with self._lock:
            cb_tripped, cb_reason = self.is_circuit_breaker_active()
            in_hours, hours_msg = self.is_within_operating_hours()
            return {
                "date": self.state.get("date", self._get_today_str()),
                "circuit_breaker_active": cb_tripped,
                "circuit_breaker_details": cb_reason,
                "within_operating_hours": in_hours,
                "operating_hours_message": hours_msg,
                "daily_counts": dict(self.state.get("counts", {})),
                "daily_limits": dict(self.state.get("limits", DEFAULT_DAILY_LIMITS)),
                "last_action_epoch": self.state.get("last_action_ts", 0.0)
            }
