"""
core/circuit_breaker.py — Pacifica API Circuit Breaker

프로덕션 수준 장애 격리:
  - CLOSED: 정상 동작 (모든 호출 통과)
  - OPEN:   장애 중 (모든 호출 즉시 실패 반환, 외부 API 과부하 방지)
  - HALF_OPEN: 복구 탐색 (제한적 허용 → 성공 시 CLOSED 복귀)

사용:
    from core.circuit_breaker import get_breaker, CircuitOpenError

    breaker = get_breaker("pacifica")
    async with breaker.call():
        result = await pacifica_api_call()

설계 원칙:
  - 연속 실패 N회 → OPEN (빠른 실패)
  - OPEN 상태 T초 후 → HALF_OPEN (복구 탐색)
  - HALF_OPEN에서 1회 성공 → CLOSED, 실패 → OPEN 연장
  - AlertManager 연동: 상태 전환 시 자동 알림
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    CLOSED    = "CLOSED"
    OPEN      = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitOpenError(Exception):
    """Circuit Breaker OPEN 상태 — 즉시 실패"""
    def __init__(self, name: str, retry_after: float):
        self.name = name
        self.retry_after = retry_after
        super().__init__(
            f"Circuit '{name}' is OPEN. Retry after {retry_after:.1f}s"
        )


@dataclass
class CircuitBreakerConfig:
    # 연속 실패 N회 → OPEN 전환
    failure_threshold: int = 5
    # OPEN 유지 시간(초) → HALF_OPEN 전환
    recovery_timeout: float = 30.0
    # HALF_OPEN에서 성공 N회 → CLOSED 복귀
    half_open_success_threshold: int = 2
    # 타임아웃 오류도 실패로 집계
    count_timeout_as_failure: bool = True
    # 느린 호출 기준(초) — 이 이상이면 '느린 성공'으로 집계
    slow_call_threshold: float = 10.0
    # 느린 호출 연속 N회 → WARNING 알림 (OPEN 전환은 하지 않음)
    slow_call_warning_count: int = 3


class CircuitBreaker:
    """
    Thread-safe Async Circuit Breaker.

    사용 예:
        breaker = CircuitBreaker("pacifica", CircuitBreakerConfig())

        # context manager 방식
        async with breaker.call():
            await some_api()

        # 데코레이터 방식
        @breaker.protect
        async def my_fn(): ...
    """

    def __init__(self, name: str, config: Optional[CircuitBreakerConfig] = None):
        self.name = name
        self.cfg = config or CircuitBreakerConfig()

        self._state = CircuitState.CLOSED
        self._failure_count: int = 0
        self._success_count: int = 0   # HALF_OPEN 전용
        self._slow_call_count: int = 0
        self._last_failure_time: float = 0.0
        self._opened_at: float = 0.0
        self._lock = asyncio.Lock()

        # 통계
        self._stats: dict = {
            "total_calls": 0,
            "total_failures": 0,
            "total_successes": 0,
            "total_timeouts": 0,
            "total_slow_calls": 0,
            "state_transitions": [],   # (ts, from, to) 최대 50개
            "consecutive_failures": 0,
        }

    # ── 상태 읽기 ─────────────────────────────────────────────────

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def is_open(self) -> bool:
        return self._state == CircuitState.OPEN

    @property
    def is_closed(self) -> bool:
        return self._state == CircuitState.CLOSED

    def retry_after(self) -> float:
        """OPEN 상태에서 HALF_OPEN 전환까지 남은 초"""
        if self._state != CircuitState.OPEN:
            return 0.0
        elapsed = time.monotonic() - self._opened_at
        return max(0.0, self.cfg.recovery_timeout - elapsed)

    # ── 상태 전환 ─────────────────────────────────────────────────

    def _transition(self, new_state: CircuitState) -> None:
        old = self._state
        self._state = new_state
        ts = time.time()
        entry = (ts, old.value, new_state.value)
        self._stats["state_transitions"].append(entry)
        if len(self._stats["state_transitions"]) > 50:
            self._stats["state_transitions"] = self._stats["state_transitions"][-50:]

        logger.warning(
            f"[CircuitBreaker/{self.name}] {old.value} → {new_state.value} "
            f"(failures={self._failure_count})"
        )
        self._notify_transition(old, new_state)

    def _notify_transition(self, old: CircuitState, new: CircuitState) -> None:
        """AlertManager에 상태 전환 통지"""
        try:
            from core.alerting import get_alert_manager
            am = get_alert_manager()
            if new == CircuitState.OPEN:
                am.monitor_disconnected(
                    self.name,
                    f"Circuit breaker OPEN after {self._failure_count} failures"
                )
            elif new == CircuitState.CLOSED and old == CircuitState.HALF_OPEN:
                am.monitor_restored(self.name)
        except Exception:
            pass  # alerting 실패는 circuit breaker를 막지 않음

    # ── 핵심 로직 ─────────────────────────────────────────────────

    async def _check_state(self) -> None:
        """호출 전 상태 확인. OPEN이면 CircuitOpenError 발생."""
        async with self._lock:
            if self._state == CircuitState.CLOSED:
                return

            if self._state == CircuitState.OPEN:
                elapsed = time.monotonic() - self._opened_at
                if elapsed >= self.cfg.recovery_timeout:
                    # HALF_OPEN으로 전환 — 1회 탐색 허용
                    self._success_count = 0
                    self._transition(CircuitState.HALF_OPEN)
                    return
                raise CircuitOpenError(self.name, self.retry_after())

            # HALF_OPEN: 통과 허용 (단 1개 동시 요청만)

    async def _on_success(self, elapsed_sec: float) -> None:
        async with self._lock:
            self._stats["total_successes"] += 1
            self._stats["total_calls"] += 1
            self._stats["consecutive_failures"] = 0

            # 느린 호출 탐지
            if elapsed_sec > self.cfg.slow_call_threshold:
                self._slow_call_count += 1
                self._stats["total_slow_calls"] += 1
                if self._slow_call_count >= self.cfg.slow_call_warning_count:
                    logger.warning(
                        f"[CircuitBreaker/{self.name}] 느린 호출 {self._slow_call_count}회 연속 "
                        f"({elapsed_sec:.1f}s > {self.cfg.slow_call_threshold}s)"
                    )
            else:
                self._slow_call_count = 0

            if self._state == CircuitState.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self.cfg.half_open_success_threshold:
                    self._failure_count = 0
                    self._transition(CircuitState.CLOSED)

            elif self._state == CircuitState.CLOSED:
                self._failure_count = 0

    async def _on_failure(self, exc: Exception) -> None:
        async with self._lock:
            is_timeout = isinstance(exc, (asyncio.TimeoutError, TimeoutError))
            if is_timeout:
                self._stats["total_timeouts"] += 1
                if not self.cfg.count_timeout_as_failure:
                    return

            self._stats["total_failures"] += 1
            self._stats["total_calls"] += 1
            self._stats["consecutive_failures"] += 1
            self._failure_count += 1
            self._last_failure_time = time.monotonic()

            should_open = (
                self._failure_count >= self.cfg.failure_threshold or
                self._state == CircuitState.HALF_OPEN  # HALF_OPEN에서 실패 → 즉시 OPEN
            )

            if should_open and self._state != CircuitState.OPEN:
                self._opened_at = time.monotonic()
                self._transition(CircuitState.OPEN)

    # ── Public API ────────────────────────────────────────────────

    @asynccontextmanager
    async def call(self):
        """
        async with breaker.call():
            result = await api_fn()
        """
        await self._check_state()
        start = time.monotonic()
        try:
            yield
            await self._on_success(time.monotonic() - start)
        except CircuitOpenError:
            raise
        except Exception as exc:
            await self._on_failure(exc)
            raise

    def protect(self, fn):
        """
        @breaker.protect
        async def my_fn(arg):
            ...
        """
        import functools

        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            async with self.call():
                return await fn(*args, **kwargs)
        return wrapper

    def get_stats(self) -> dict:
        return {
            "name":  self.name,
            "state": self._state.value,
            "failure_count": self._failure_count,
            "success_count_half_open": self._success_count,
            "slow_call_count": self._slow_call_count,
            "retry_after_sec": round(self.retry_after(), 1),
            "config": {
                "failure_threshold":          self.cfg.failure_threshold,
                "recovery_timeout":           self.cfg.recovery_timeout,
                "half_open_success_threshold":self.cfg.half_open_success_threshold,
                "slow_call_threshold":        self.cfg.slow_call_threshold,
            },
            **self._stats,
        }

    def reset(self) -> None:
        """강제 리셋 (테스트/수동 복구용)"""
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._slow_call_count = 0
        self._stats["consecutive_failures"] = 0
        logger.info(f"[CircuitBreaker/{self.name}] 수동 리셋")


# ── 글로벌 레지스트리 ──────────────────────────────────────────

_registry: dict[str, CircuitBreaker] = {}


def get_breaker(
    name: str,
    config: Optional[CircuitBreakerConfig] = None,
) -> CircuitBreaker:
    """
    이름으로 Circuit Breaker 인스턴스 반환 (싱글턴).
    최초 호출 시 config 적용, 이후 config 무시.
    """
    if name not in _registry:
        _registry[name] = CircuitBreaker(name, config)
    return _registry[name]


def get_all_stats() -> dict[str, dict]:
    """모든 Circuit Breaker 상태 반환 (/metrics, /health 엔드포인트용)"""
    return {name: cb.get_stats() for name, cb in _registry.items()}


def reset_all() -> None:
    """모든 Circuit Breaker 강제 리셋 (테스트용)"""
    for cb in _registry.values():
        cb.reset()
