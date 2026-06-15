"""Circuit Breaker v0.9.2 — Improvement #1.

Protectie impotriva erorilor repetate de exchange (5xx, timeout, retCode!=0).
Pattern: CLOSED -> OPEN (dupa N erori) -> HALF_OPEN (dupa cooldown) -> CLOSED.

Stari:
  CLOSED    — normal, apeluri permise
  OPEN      — blocat, toate apelurile respinse imediat cu CircuitOpenError
  HALF_OPEN — un singur apel de test permis; daca reuseste -> CLOSED,
               daca esueaza -> OPEN din nou cu cooldown dublu (max 10min)

Integrare:
  from .circuit_breaker import circuit_breaker, CircuitOpenError

  try:
      await circuit_breaker.call(trader.place_order, ...)
  except CircuitOpenError:
      logger.warning("Circuit open, skipping order")
"""
from __future__ import annotations

import asyncio
import time
from enum import Enum
from loguru import logger


class CircuitOpenError(Exception):
    """Ridicata cand circuit breaker-ul e in starea OPEN."""


class CBState(Enum):
    CLOSED    = "CLOSED"
    OPEN      = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreaker:
    """Circuit breaker async pentru apeluri catre exchange.

    Parametri:
      failure_threshold  — numar de esecuri consecutive pentru a deschide circuitul
      success_threshold  — numar de succese in HALF_OPEN pentru a inchide circuitul
      base_cooldown_s    — timp initial de asteptare in starea OPEN (secunde)
      max_cooldown_s     — cooldown maxim (exponential backoff pana la acest plafon)
    """

    def __init__(
        self,
        failure_threshold: int   = 5,
        success_threshold: int   = 2,
        base_cooldown_s:   float = 30.0,
        max_cooldown_s:    float = 600.0,
    ):
        self._failure_threshold = failure_threshold
        self._success_threshold = success_threshold
        self._base_cooldown     = base_cooldown_s
        self._max_cooldown      = max_cooldown_s

        self._state:             CBState = CBState.CLOSED
        self._failure_count:     int     = 0
        self._success_count:     int     = 0
        self._opened_at:         float   = 0.0
        self._current_cooldown:  float   = base_cooldown_s
        self._half_open_lock:    asyncio.Lock | None = None

    def _get_lock(self) -> asyncio.Lock:
        # Lazy init pentru a evita erori daca obiectul e creat inainte de event loop
        if self._half_open_lock is None:
            self._half_open_lock = asyncio.Lock()
        return self._half_open_lock

    @property
    def state(self) -> CBState:
        if self._state == CBState.OPEN:
            if time.monotonic() - self._opened_at >= self._current_cooldown:
                self._state = CBState.HALF_OPEN
                self._success_count = 0
                logger.info(
                    f"CircuitBreaker → HALF_OPEN "
                    f"(cooldown {self._current_cooldown:.0f}s expirat, test apel permis)"
                )
        return self._state

    def _on_success(self) -> None:
        if self._state == CBState.HALF_OPEN:
            self._success_count += 1
            if self._success_count >= self._success_threshold:
                self._state            = CBState.CLOSED
                self._failure_count    = 0
                self._current_cooldown = self._base_cooldown  # reset backoff
                logger.info("CircuitBreaker → CLOSED (exchange responsive)")
                self._notify_telegram("\u2705 *Circuit Breaker*: exchange responsive — CLOSED.")
        else:
            self._failure_count = 0

    def _on_failure(self, reason: str) -> None:
        self._failure_count += 1
        if self._state == CBState.HALF_OPEN:
            # Test esuat — redeschide cu cooldown dublu
            self._current_cooldown = min(self._current_cooldown * 2, self._max_cooldown)
            self._state     = CBState.OPEN
            self._opened_at = time.monotonic()
            logger.warning(
                f"CircuitBreaker → OPEN (half-open test failed: {reason}, "
                f"next cooldown {self._current_cooldown:.0f}s)"
            )
            self._notify_telegram(
                f"\U0001f6a8 *Circuit Breaker*: OPEN din nou.\n"
                f"Reason: `{reason}`\n"
                f"Cooldown: {self._current_cooldown:.0f}s"
            )
        elif self._failure_count >= self._failure_threshold:
            self._state     = CBState.OPEN
            self._opened_at = time.monotonic()
            logger.error(
                f"CircuitBreaker → OPEN ({self._failure_count} esecuri consecutive: {reason})"
            )
            self._notify_telegram(
                f"\U0001f6a8 *Circuit Breaker*: OPEN!\n"
                f"Reason: `{reason}`\n"
                f"Esecuri: {self._failure_count}/{self._failure_threshold}\n"
                f"Ordine blocate pentru {self._current_cooldown:.0f}s."
            )

    def _notify_telegram(self, msg: str) -> None:
        try:
            import asyncio as _asyncio
            from .telegram_ui import send_message
            loop = _asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(send_message(msg))
        except Exception:
            pass

    async def call(self, fn, *args, **kwargs):
        """Executa fn(*args, **kwargs) prin circuit breaker.

        Raises:
            CircuitOpenError: daca circuitul e OPEN
            Exception: orice exceptie din fn() e propagata dupa inregistrarea erorii
        """
        current = self.state

        if current == CBState.OPEN:
            raise CircuitOpenError(
                f"Circuit OPEN — exchange indisponibil "
                f"(retry in {max(0, self._current_cooldown - (time.monotonic() - self._opened_at)):.0f}s)"
            )

        if current == CBState.HALF_OPEN:
            # Doar un singur apel de test simultan in HALF_OPEN
            async with self._get_lock():
                return await self._execute(fn, *args, **kwargs)

        return await self._execute(fn, *args, **kwargs)

    async def _execute(self, fn, *args, **kwargs):
        try:
            result = await fn(*args, **kwargs) if asyncio.iscoroutinefunction(fn) else fn(*args, **kwargs)
            # Verifica retCode pentru erori Bybit (5xx echivalent in REST API)
            if isinstance(result, dict):
                ret_code = result.get("retCode", 0)
                if ret_code in (10004, 10016, 10001, 500, 503):  # server errors
                    self._on_failure(f"retCode={ret_code} retMsg={result.get('retMsg', '')}")
                else:
                    self._on_success()
            else:
                self._on_success()
            return result
        except (ConnectionError, TimeoutError, OSError) as e:
            self._on_failure(str(e))
            raise
        except Exception as e:
            # Erori de aplicatie (ex. ValueError) nu conteaza pentru circuit
            self._on_success()
            raise

    @property
    def is_open(self) -> bool:
        return self.state == CBState.OPEN

    def status(self) -> dict:
        """Returneaza statusul curent — folosit de /watchdog si pulse."""
        return {
            "state":          self.state.value,
            "failure_count":  self._failure_count,
            "cooldown_s":     self._current_cooldown,
            "opened_ago_s":   round(time.monotonic() - self._opened_at, 1) if self._opened_at else 0,
        }


# Singleton global
circuit_breaker = CircuitBreaker(
    failure_threshold=5,
    success_threshold=2,
    base_cooldown_s=30.0,
    max_cooldown_s=600.0,
)
