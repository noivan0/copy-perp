"""
재시도 유틸리티 — 네트워크 오류 시 지수 백오프 재시도
실서비스 수준: 원인별 분기, 알림 훅, 429 전용 딜레이
"""
import asyncio
import logging
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# 재시도 가능한 오류 유형
RETRYABLE_ERRORS = (
    TimeoutError,
    ConnectionRefusedError,
    ConnectionResetError,
    OSError,
    BrokenPipeError,
)

# 재시도 불가 오류 키워드 (즉시 포기)
NON_RETRYABLE_KEYWORDS = [
    "insufficient balance",
    "invalid signature",
    "symbol not found",
    "market closed",
    "invalid amount",
    "below minimum",
    "400",               # Bad Request — 파라미터 오류
    "unauthorized",
    "forbidden",
]

# 원인별 딜레이 (초)
DELAY_429       = 5.0   # Rate Limit
DELAY_500       = 2.0   # Server Error
DELAY_CONN      = 1.0   # Connection Error

# 글로벌 알림 훅 (외부에서 등록)
_alert_hooks: list[Callable[[str, str], None]] = []


def register_alert_hook(fn: Callable[[str, str], None]) -> None:
    """알림 훅 등록: fn(level, message) — 'error' / 'warning'"""
    _alert_hooks.append(fn)


def _fire_alert(level: str, msg: str) -> None:
    for hook in _alert_hooks:
        try:
            hook(level, msg)
        except Exception as e:
            logger.debug(f"무시된 예외: {e}")


def classify_error(exc: Exception) -> tuple[bool, float]:
    """
    (retryable, delay_seconds) 반환
    """
    msg = str(exc).lower()

    # 레이트 리밋 → 5s 딜레이 후 재시도
    if "429" in msg or "rate limit" in msg or "too many requests" in msg:
        return True, DELAY_429

    # 서버 에러 → 2s 딜레이
    if "500" in msg or "502" in msg or "503" in msg or "internal server error" in msg:
        return True, DELAY_500

    # 명시적으로 재시도 불가
    for kw in NON_RETRYABLE_KEYWORDS:
        if kw in msg:
            return False, 0

    # 연결 오류 → 1s 딜레이
    if isinstance(exc, RETRYABLE_ERRORS):
        return True, DELAY_CONN

    return False, 0


async def retry_async(
    fn,
    *args,
    max_retries: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 30.0,
    label: str = "",
    alert_on_final_fail: bool = False,
    **kwargs,
):
    """
    비동기 함수 재시도 with 지수 백오프 + 원인별 딜레이

    Args:
        fn: 실행할 비동기 함수
        max_retries: 최대 재시도 횟수 (최소 3)
        base_delay: 초기 지연 (초)
        max_delay: 최대 지연 (초)
        label: 로깅/알림용 레이블
        alert_on_final_fail: 최종 실패 시 알림 훅 호출
    """
    max_retries = max(max_retries, 3)  # 최소 3회 보장
    last_exc: Optional[Exception] = None

    for attempt in range(max_retries + 1):
        try:
            return await fn(*args, **kwargs)
        except Exception as exc:
            last_exc = exc
            retryable, cause_delay = classify_error(exc)

            if not retryable:
                logger.debug(f"[retry/{label}] 재시도 불가 ({type(exc).__name__}): {exc}")
                raise

            if attempt >= max_retries:
                msg = f"[retry/{label}] 최대 {max_retries}회 실패: {exc}"
                logger.warning(msg)
                if alert_on_final_fail:
                    _fire_alert("error", f"🚨 주문 최종 실패 [{label}]: {exc}")
                raise

            # 딜레이 계산: cause_delay 우선, 없으면 지수 백오프
            delay = cause_delay if cause_delay else min(base_delay * (2 ** attempt), max_delay)
            logger.info(f"[retry/{label}] 시도 {attempt+1}/{max_retries} 실패 ({delay:.1f}s 대기): {exc}")
            await asyncio.sleep(delay)

    raise last_exc  # type: ignore


def retry_sync(
    fn,
    *args,
    max_retries: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 30.0,
    label: str = "",
    alert_on_final_fail: bool = False,
    **kwargs,
):
    """
    동기 함수 재시도 with 지수 백오프 + 원인별 딜레이
    """
    max_retries = max(max_retries, 3)
    last_exc: Optional[Exception] = None

    for attempt in range(max_retries + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            last_exc = exc
            retryable, cause_delay = classify_error(exc)

            if not retryable:
                logger.debug(f"[retry/{label}] 재시도 불가: {exc}")
                raise

            if attempt >= max_retries:
                msg = f"[retry/{label}] 최대 {max_retries}회 실패: {exc}"
                logger.warning(msg)
                if alert_on_final_fail:
                    _fire_alert("error", f"🚨 주문 최종 실패 [{label}]: {exc}")
                raise

            delay = cause_delay if cause_delay else min(base_delay * (2 ** attempt), max_delay)
            logger.info(f"[retry/{label}] 시도 {attempt+1}/{max_retries} 실패 ({delay:.1f}s 대기): {exc}")
            time.sleep(delay)

    raise last_exc  # type: ignore
