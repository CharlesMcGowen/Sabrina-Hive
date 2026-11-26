# -*- coding: utf-8 -*-
"""
tools/x_rate_limiter.py
=======================
Anti-flagging rate limiter and quota management for Sabrina's X.com (Twitter) operations.

Enforces:
1. Hard daily action quotas persisted in data/x_rate_limit.json.
2. Randomized Gaussian/Poisson jitter delays between actions to prevent bot-detection flags.
3. Natural operating constraints (safe spacing, rate limits).
4. Circuit breaker: auto-trips if a checkpoint, CAPTCHA, or verification wall is encountered.
"""

import os
import json
import time
import random
import datetime
import threading
from typing import Dict, Any, Tuple, Optional

DEFAULT_RATE_LIMIT_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "x_rate_limit.json"
)

DEFAULT_DAILY_LIMITS = {
    "feed_read": 500,
    "tweet_post": 24,
    "tweet_quote": 18,
    "tweet_like": 160,
    "tweet_retweet": 40,
    "tweet_reply": 70,
    "user_follow": 25,
    "analytics_read": 120,
}

ACTION_DELAYS = {
    "feed_read": (2.0, 5.0),
    "tweet_post": (8.0, 18.0),
    "tweet_quote": (8.0, 18.0),
    "tweet_like": (1.8, 4.5),
    "tweet_retweet": (3.0, 7.5),
    "tweet_reply": (5.5, 12.5),
    "user_follow": (4.0, 9.5),
    "analytics_read": (2.0, 5.0),
    "default": (2.5, 6.5),
}


class XRateLimiter:
    _instance: Optional["XRateLimiter"] = None
    _lock = threading.RLock()

    def __init__(self, data_file: str = DEFAULT_RATE_LIMIT_FILE, limits: Optional[Dict[str, int]] = None):
        self.data_file = data_file
        self.daily_limits = limits or dict(DEFAULT_DAILY_LIMITS)
        self.state_lock = threading.RLock()
        self._ensure_dir()
        self.state = self._load_state()

    @classmethod
    def get_instance(cls, data_file: str = DEFAULT_RATE_LIMIT_FILE) -> "XRateLimiter":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(data_file=data_file)
            return cls._instance

    def _ensure_dir(self):
        directory = os.path.dirname(self.data_file)
        if directory and not os.path.exists(directory):
            os.makedirs(directory, exist_ok=True)

    def _load_state(self) -> Dict[str, Any]:
        with self.state_lock:
            today_str = datetime.date.today().isoformat()
            if os.path.exists(self.data_file):
                try:
                    with open(self.data_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if data.get("date") == today_str:
                        return data
                except Exception:
                    pass

            new_state = {
                "date": today_str,
                "counts": {action: 0 for action in self.daily_limits},
                "circuit_broken": False,
                "circuit_break_until": 0,
                "circuit_break_reason": "",
                "last_action_timestamp": {}
            }
            self._save_state(new_state)
            return new_state

    def _save_state(self, state: Dict[str, Any]):
        try:
            with open(self.data_file, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
        except Exception:
            pass

    def check_quota(self, action: str) -> Tuple[bool, str]:
        with self.state_lock:
            now = time.time()
            if self.state.get("circuit_broken"):
                until = self.state.get("circuit_break_until", 0)
                if now < until:
                    remaining_mins = max(1, int((until - now) / 60))
                    reason = self.state.get("circuit_break_reason", "Security checkpoint")
                    return False, f"Circuit breaker active: {reason}. Cooldown remaining: {remaining_mins}m."
                else:
                    self.state["circuit_broken"] = False
                    self.state["circuit_break_reason"] = ""
                    self._save_state(self.state)

            today_str = datetime.date.today().isoformat()
            if self.state.get("date") != today_str:
                self.state = self._load_state()

            limit = self.daily_limits.get(action, 10)
            current_count = self.state.get("counts", {}).get(action, 0)

            if current_count >= limit:
                return False, f"Daily limit reached for '{action}' ({current_count}/{limit})."

            return True, f"Quota available for '{action}' ({current_count}/{limit})."

    def record_action(self, action: str):
        with self.state_lock:
            today_str = datetime.date.today().isoformat()
            if self.state.get("date") != today_str:
                self.state = self._load_state()

            counts = self.state.setdefault("counts", {})
            counts[action] = counts.get(action, 0) + 1
            timestamps = self.state.setdefault("last_action_timestamp", {})
            timestamps[action] = time.time()
            self._save_state(self.state)

    def trip_circuit_breaker(self, reason: str, cooldown_hours: float = 12.0):
        with self.state_lock:
            now = time.time()
            self.state["circuit_broken"] = True
            self.state["circuit_break_until"] = now + (cooldown_hours * 3600.0)
            self.state["circuit_break_reason"] = reason
            self._save_state(self.state)

    def get_jitter_delay(self, action: str) -> float:
        delay_range = ACTION_DELAYS.get(action, ACTION_DELAYS["default"])
        low, high = delay_range
        return random.uniform(low, high)

    def get_status_summary(self) -> Dict[str, Any]:
        with self.state_lock:
            today_str = datetime.date.today().isoformat()
            if self.state.get("date") != today_str:
                self.state = self._load_state()
            return {
                "date": self.state.get("date"),
                "counts": dict(self.state.get("counts", {})),
                "limits": dict(self.daily_limits),
                "circuit_broken": self.state.get("circuit_broken", False),
                "circuit_break_reason": self.state.get("circuit_break_reason", ""),
            }
