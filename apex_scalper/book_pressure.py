"""Book Pressure v0.7.1 — granular level-based absorption detection.

Fixes vs v0.7.0 (FIX #5):
  Old absorption check compared avg_total_ask_vol / avg_total_bid_vol across
  50 snapshots. A spoof wall placed at levels 5-10+ was diluted by:
    (a) 49 other snapshots in the rolling window
    (b) all other levels in the same snapshot contributing to the total
  Result: a wall 6x the near-touch size at depth-8 would show as ~1.1x ratio
  across the window and pass the filter undetected.

  New design: on_tick() accepts level lists [(price, size), ...] in addition
  to legacy scalar fallback. Absorption is detected on two independent checks:

  Check A — Near-touch wall (bid/ask levels 0..NEAR_LEVELS-1):
    near_ask_sum / near_bid_sum > ABSORPTION_RATIO
    Detects genuine resistance close to mid that a market order would hit.

  Check B — Deep wall disproportionate to near-touch (levels NEAR..DEEP_LEVELS):
    deep_ask_sum > near_ask_sum * DEEP_WALL_MULT
    Classic spoof pattern: thin near-touch, huge wall at +5-10 ticks.
    Blocks entry even when near-touch ratio looks normal.

  Both checks use a rolling window of ABSORPTION_WINDOW ticks (default 5)
  to smooth single-tick noise. A spoofer would need to maintain the wall for
  5+ consecutive ticks to trigger, reducing false positives from transient OB
  changes.

  Params (all env-configurable):
    ABSORPTION_NEAR_LEVELS = 3    (levels considered 'near touch')
    ABSORPTION_DEEP_LEVELS = 10   (max depth scanned for walls)
    ABSORPTION_RATIO       = 3.0  (near-touch ask/bid threshold)
    DEEP_WALL_MULT         = 5.0  (deep wall vs near-touch multiplier)
    ABSORPTION_WINDOW      = 5    (rolling ticks for smoothing)

  Backward-compatible: if on_tick(bid_vol: float, ask_vol: float) is called
  with scalars, near=scalar and deep=0. Check A still works; Check B is
  disabled. Callers should migrate to passing level lists for full benefit.
"""
from __future__ import annotations

import os
from collections import deque
from loguru import logger
from typing import Union

BASE_THRESHOLD        = float(os.getenv("BP_BASE_THRESHOLD",     "50000"))
WINDOW_TICKS          = int(os.getenv("BP_WINDOW_TICKS",          "50"))
ACCEL_WINDOW          = int(os.getenv("BP_ACCEL_WINDOW",          "10"))
ABSORPTION_RATIO      = float(os.getenv("ABSORPTION_RATIO",       "3.0"))
ABSORPTION_NEAR_LEVELS = int(os.getenv("ABSORPTION_NEAR_LEVELS",  "3"))
ABSORPTION_DEEP_LEVELS = int(os.getenv("ABSORPTION_DEEP_LEVELS",  "10"))
DEEP_WALL_MULT        = float(os.getenv("DEEP_WALL_MULT",         "5.0"))
ABSORPTION_WINDOW     = int(os.getenv("ABSORPTION_WINDOW",        "5"))

LevelList = list[tuple[float, float]]   # [(price, size), ...]


def _near_deep_sums(
    levels: LevelList,
    near_n: int,
    deep_n: int,
) -> tuple[float, float]:
    """Return (near_sum, deep_sum) for a side's level list.

    near_sum = sum of sizes for levels[0 .. near_n-1]  (closest to mid)
    deep_sum = sum of sizes for levels[near_n .. deep_n-1]
    """
    near = sum(sz for _, sz in levels[:near_n])
    deep = sum(sz for _, sz in levels[near_n:deep_n])
    return near, deep


class BookPressure:
    def __init__(self):
        self._deltas:   deque = deque(maxlen=WINDOW_TICKS)
        # FIX #5: per-level rolling buffers instead of total-vol buffers
        self._near_bid_buf: deque = deque(maxlen=ABSORPTION_WINDOW)
        self._near_ask_buf: deque = deque(maxlen=ABSORPTION_WINDOW)
        self._deep_bid_buf: deque = deque(maxlen=ABSORPTION_WINDOW)
        self._deep_ask_buf: deque = deque(maxlen=ABSORPTION_WINDOW)
        self._vol_zscore:   float = 0.0
        self.cum_delta:     float = 0.0
        self.acceleration:  float = 0.0
        self._ready:        bool  = False

    def on_tick(
        self,
        bid_side: Union[float, LevelList],
        ask_side: Union[float, LevelList],
    ) -> None:
        """Process one OB snapshot.

        Args:
            bid_side: list[(price, size)] for bid levels (best bid first),
                      OR scalar total bid volume (legacy/fallback).
            ask_side: list[(price, size)] for ask levels (best ask first),
                      OR scalar total ask volume (legacy/fallback).
        """
        # --- Compute near/deep sums ---
        if isinstance(bid_side, list) and isinstance(ask_side, list):
            near_bid, deep_bid = _near_deep_sums(
                bid_side, ABSORPTION_NEAR_LEVELS, ABSORPTION_DEEP_LEVELS
            )
            near_ask, deep_ask = _near_deep_sums(
                ask_side, ABSORPTION_NEAR_LEVELS, ABSORPTION_DEEP_LEVELS
            )
            # Total vol for delta: use full depth up to DEEP_LEVELS
            bid_vol = sum(sz for _, sz in bid_side[:ABSORPTION_DEEP_LEVELS])
            ask_vol = sum(sz for _, sz in ask_side[:ABSORPTION_DEEP_LEVELS])
        else:
            # Legacy scalar path
            bid_vol   = float(bid_side)
            ask_vol   = float(ask_side)
            near_bid  = bid_vol
            near_ask  = ask_vol
            deep_bid  = 0.0
            deep_ask  = 0.0

        # --- Delta / acceleration (unchanged from v0.7.0) ---
        delta = bid_vol - ask_vol
        self._deltas.append(delta)

        # --- Append to absorption rolling buffers ---
        self._near_bid_buf.append(near_bid)
        self._near_ask_buf.append(near_ask)
        self._deep_bid_buf.append(deep_bid)
        self._deep_ask_buf.append(deep_ask)

        if len(self._deltas) >= ACCEL_WINDOW * 2:
            self._ready    = True
            all_d          = list(self._deltas)
            self.cum_delta = sum(all_d)
            last           = sum(all_d[-ACCEL_WINDOW:])
            prev           = sum(all_d[-ACCEL_WINDOW * 2:-ACCEL_WINDOW])
            self.acceleration = last - prev

    def set_vol_zscore(self, z: float) -> None:
        self._vol_zscore = z

    def _threshold(self) -> float:
        return BASE_THRESHOLD * (1 + max(self._vol_zscore, 0) * 0.3)

    def _absorption_long(self) -> bool:
        """True if ask-side absorption detected (blocks long signal).

        Check A: rolling avg near-touch ask > near-touch bid * ABSORPTION_RATIO
        Check B: rolling avg deep ask > rolling avg near ask * DEEP_WALL_MULT
        Either check is sufficient to block.
        """
        if not self._near_ask_buf:
            return False

        avg_near_ask = sum(self._near_ask_buf) / len(self._near_ask_buf)
        avg_near_bid = sum(self._near_bid_buf) / len(self._near_bid_buf)
        avg_deep_ask = sum(self._deep_ask_buf) / len(self._deep_ask_buf)

        # Check A: near-touch seller wall dominates near-touch buyer
        if avg_near_bid > 0 and avg_near_ask / avg_near_bid > ABSORPTION_RATIO:
            logger.debug(
                f"[BP] Absorption-A LONG blocked: "
                f"near_ask={avg_near_ask:.0f} / near_bid={avg_near_bid:.0f} "
                f"= {avg_near_ask/avg_near_bid:.2f}x > {ABSORPTION_RATIO}x"
            )
            return True

        # Check B: deep ask wall disproportionate to near-touch ask
        if avg_near_ask > 0 and avg_deep_ask / avg_near_ask > DEEP_WALL_MULT:
            logger.debug(
                f"[BP] Absorption-B LONG blocked (deep ask wall): "
                f"deep_ask={avg_deep_ask:.0f} / near_ask={avg_near_ask:.0f} "
                f"= {avg_deep_ask/avg_near_ask:.2f}x > {DEEP_WALL_MULT}x"
            )
            return True

        return False

    def _absorption_short(self) -> bool:
        """True if bid-side absorption detected (blocks short signal)."""
        if not self._near_bid_buf:
            return False

        avg_near_bid = sum(self._near_bid_buf) / len(self._near_bid_buf)
        avg_near_ask = sum(self._near_ask_buf) / len(self._near_ask_buf)
        avg_deep_bid = sum(self._deep_bid_buf) / len(self._deep_bid_buf)

        # Check A: near-touch buyer wall dominates near-touch seller
        if avg_near_ask > 0 and avg_near_bid / avg_near_ask > ABSORPTION_RATIO:
            logger.debug(
                f"[BP] Absorption-A SHORT blocked: "
                f"near_bid={avg_near_bid:.0f} / near_ask={avg_near_ask:.0f} "
                f"= {avg_near_bid/avg_near_ask:.2f}x > {ABSORPTION_RATIO}x"
            )
            return True

        # Check B: deep bid wall disproportionate to near-touch bid
        if avg_near_bid > 0 and avg_deep_bid / avg_near_bid > DEEP_WALL_MULT:
            logger.debug(
                f"[BP] Absorption-B SHORT blocked (deep bid wall): "
                f"deep_bid={avg_deep_bid:.0f} / near_bid={avg_near_bid:.0f} "
                f"= {avg_deep_bid/avg_near_bid:.2f}x > {DEEP_WALL_MULT}x"
            )
            return True

        return False

    def pressure_long(self) -> bool:
        if not self._ready:
            return False
        th = self._threshold()
        if self.cum_delta < th:
            return False
        if self.acceleration <= 0:
            return False
        if self._absorption_long():
            return False
        return True

    def pressure_short(self) -> bool:
        if not self._ready:
            return False
        th = self._threshold()
        if self.cum_delta > -th:
            return False
        if self.acceleration >= 0:
            return False
        if self._absorption_short():
            return False
        return True

    def ready(self) -> bool:
        return self._ready

    def debug_str(self) -> str:
        avg_na = sum(self._near_ask_buf) / len(self._near_ask_buf) if self._near_ask_buf else 0
        avg_nb = sum(self._near_bid_buf) / len(self._near_bid_buf) if self._near_bid_buf else 0
        avg_da = sum(self._deep_ask_buf) / len(self._deep_ask_buf) if self._deep_ask_buf else 0
        avg_db = sum(self._deep_bid_buf) / len(self._deep_bid_buf) if self._deep_bid_buf else 0
        return (
            f"cum_delta={self.cum_delta:+.0f} "
            f"accel={self.acceleration:+.0f} "
            f"thr={self._threshold():.0f} "
            f"near_bid={avg_nb:.0f} near_ask={avg_na:.0f} "
            f"deep_bid={avg_db:.0f} deep_ask={avg_da:.0f} "
            f"long={self.pressure_long()} short={self.pressure_short()}"
        )


bp = BookPressure()
