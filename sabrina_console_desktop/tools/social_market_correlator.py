# -*- coding: utf-8 -*-
"""
tools/social_market_correlator.py
=================================
Correlates timestamped social media posts (X.com, TikTok) with DEX price
and volume candles, computing lead-lag relationships and overlay markers.
"""

import time
import math
import logging
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger("sabrina.crypto.correlator")


class SocialMarketCorrelator:
    """Synchronizes social events with candlestick price action and evaluates trading alpha."""

    def __init__(self, lead_threshold_min: float = 3.0):
        self.lead_threshold_min = lead_threshold_min

    def correlate(
        self,
        candles: List[Dict[str, Any]],
        social_events: List[Dict[str, Any]],
        token_meta: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Correlates price candles with social events:
        - Maps each social event to the closest price candle
        - Analyzes lead-lag relationship (did social precede price pump?)
        - Classifies actionable trading signal
        """
        if not candles:
            return {
                "status": "insufficient_data",
                "reason": "No price candles provided",
                "timeline": [],
            }

        # Sort candles by timestamp ascending
        sorted_candles = sorted(candles, key=lambda c: c["timestamp"])
        t_start = sorted_candles[0]["timestamp"]
        t_end = sorted_candles[-1]["timestamp"]

        # Filter social events to window (or slightly before)
        window_events = [
            e for e in social_events
            if (t_start - 3600) <= e.get("timestamp", 0) <= (t_end + 300)
        ]
        window_events.sort(key=lambda e: e.get("timestamp", 0))

        # Bucket events into candles
        candle_timeline = []
        for i, candle in enumerate(sorted_candles):
            c_time = candle["timestamp"]
            # Next candle time or 5m (300s) default
            next_time = sorted_candles[i + 1]["timestamp"] if i + 1 < len(sorted_candles) else c_time + 300

            # Find social events falling in this candle interval
            matching_events = [
                e for e in window_events
                if c_time <= e.get("timestamp", 0) < next_time
            ]

            posts_count = len(matching_events)
            total_likes = sum(e.get("likes", 0) for e in matching_events)
            total_reposts = sum(e.get("reposts", 0) for e in matching_events)

            # Markers on graph
            markers = []
            for ev in matching_events:
                is_influencer = (ev.get("likes", 0) >= 50 or ev.get("reposts", 0) >= 15)
                is_dog_pile = ev.get("is_dog_pile", False)

                if is_influencer:
                    marker_type = "INFLUENCER_CALL"
                    color = "#ef4444"  # Red
                elif is_dog_pile:
                    marker_type = "DOG_PILE_SPIKE"
                    color = "#eab308"  # Yellow
                else:
                    marker_type = "SOCIAL_MENTION"
                    color = "#22c55e"  # Green

                markers.append({
                    "type": marker_type,
                    "author": ev.get("author", "@unknown"),
                    "text": ev.get("text", "")[:80],
                    "color": color,
                    "likes": ev.get("likes", 0),
                    "reposts": ev.get("reposts", 0),
                    "time": ev.get("timestamp"),
                })

            candle_timeline.append({
                "timestamp": c_time,
                "price": candle.get("close") or candle.get("price") or 0.0,
                "high": candle.get("high") or candle.get("price") or 0.0,
                "low": candle.get("low") or candle.get("price") or 0.0,
                "volume": candle.get("volume") or 0.0,
                "social_mentions": posts_count,
                "social_likes": total_likes,
                "social_reposts": total_reposts,
                "markers": markers,
            })

        # Lead-lag evaluation
        lead_lag_analysis = self._evaluate_lead_lag(candle_timeline)

        return {
            "status": "correlated",
            "token": token_meta or {},
            "total_candles": len(candle_timeline),
            "total_social_events": len(window_events),
            "lead_lag": lead_lag_analysis,
            "timeline": candle_timeline,
        }

    def _evaluate_lead_lag(self, timeline: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Evaluates whether social spikes preceded price pumps or vice versa."""
        if len(timeline) < 3:
            return {"verdict": "INSUFFICIENT_TIMELINE", "lead_minutes": 0, "signal": "NEUTRAL"}

        # Find initial price breakout candle (> 10% gain over 2 candles)
        breakout_idx = None
        for i in range(1, len(timeline)):
            p_prev = timeline[i - 1]["price"]
            p_curr = timeline[i]["price"]
            if p_prev > 0 and (p_curr - p_prev) / p_prev >= 0.10:
                breakout_idx = i
                break

        # Find first significant social surge
        social_spike_idx = None
        for i in range(len(timeline)):
            if timeline[i]["social_mentions"] >= 3 or any(m["type"] in ("DOG_PILE_SPIKE", "INFLUENCER_CALL") for m in timeline[i]["markers"]):
                social_spike_idx = i
                break

        if breakout_idx is None and social_spike_idx is None:
            return {"verdict": "LOW_ACTIVITY", "lead_minutes": 0, "signal": "NEUTRAL"}

        if social_spike_idx is not None and breakout_idx is not None:
            time_delta_sec = timeline[breakout_idx]["timestamp"] - timeline[social_spike_idx]["timestamp"]
            lead_min = time_delta_sec / 60.0

            if lead_min >= self.lead_threshold_min:
                verdict = "SOCIAL_LEADS_PRICE_ALPHA"
                signal = "ACCUMULATE_EARLY_MOMENTUM"
            elif lead_min <= -self.lead_threshold_min:
                verdict = "PRICE_LEADS_SOCIAL_FOMO"
                signal = "FADE_LATE_FOMO"
            else:
                verdict = "COINCIDENT_MOMENTUM"
                signal = "MOMENTUM_CONFIRMATION"

            return {
                "verdict": verdict,
                "lead_minutes": round(lead_min, 1),
                "breakout_time": timeline[breakout_idx]["timestamp"],
                "social_spike_time": timeline[social_spike_idx]["timestamp"],
                "signal": signal,
            }

        if social_spike_idx is not None and breakout_idx is None:
            return {
                "verdict": "SOCIAL_PUMP_PENDING_PRICE_CONFIRMATION",
                "lead_minutes": 0,
                "signal": "WATCH_FOR_BREAKOUT",
            }

        return {
            "verdict": "PRICE_MOVE_WITHOUT_SOCIAL_CATALYST",
            "lead_minutes": 0,
            "signal": "ORGANIC_OR_INSIDER_VOLUME",
        }
