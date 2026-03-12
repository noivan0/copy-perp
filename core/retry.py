"""
재시도 유틸리티 — 네트워크 오류 시 지수 백오프 재시도
"""
import asyncio
import logging
import time

logger = logging.getLogger(__name__)

# 재시도 가능한 오류 유형
RETRYABLE_ERRORS = (
    TimeoutError,
    ConnectionRefusedError,
    ConnectionResetError,
    OSError,
)

# 재시도 불가 오류 키워드 (즉시 포기)
NON_RETRYABLE_KEYWORDS = [
    "insufficient balance",
    "invalid signature",
    "symbol not found",
    "market closed",
    "invalid amount",
    "below minimum",
]


def is_retryable(exc: Exception) -> bool:
    """재시도 가능한 예외인지 판단"""
    if isinstance(exc, RETRYABLE_ERRORS):
        return True
    msg = str(exc).lower()
    # 레이트 리밋은 재시도 가능
    if "429" in msg or "rate limit" in msg or "too many requests" in msg:
        return True
    # 서버 500은 재시도 가능
    if "500" in msg or "internal server error" in msg:
        return True
    # 명시적으로 재시도 불가한 에러
    for kw in NON_RETRYABLE_KEYWORDS:
        if kw in msg:
            return False
    return False


async def retry_async(
    fn,
    *args,
    max_retries: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 10.0,
    label: str = "",
    **kwargs,
):
    """
    비동기 함수 재시도 with 지수 백오프

    Args:
        fn: 실행할 비동기 함수
        max_retries: 최대 재시도 횟수 (초기 시도 제외)
        base_delay: 초기 지연 (초)
        max_delay: 최대 지연 (초)
        label: 로깅용 레이블

    Returns:
        fn 실행 결과

    Raises:
        마지막 예외 (모든 재시도 소진 후)
    """
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            return await fn(*args, **kwargs)
        except Exception as exc:
            last_exc = exc
            if not is_retryable(exc):
                logger.debug(f"[retry/{label}] 재시도 불가 에러: {exc}")
                raise

            if attempt >= max_retries:
                logger.warning(f"[retry/{label}] 최대 재시도 {max_retries}회 소진: {exc}")
                raise

            delay = min(base_delay * (2 ** attempt), max_delay)
            logger.info(f"[retry/{label}] 시도 {attempt+1}/{max_retries} 실패, {delay:.1f}s 후 재시도: {exc}")
            await asyncio.sleep(delay)

    raise last_exc


def retry_sync(
    fn,
    *args,
    max_retries: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 10.0,
    label: str = "",
    **kwargs,
):
    """
    동기 함수 재시도 with 지수 백오프
    """
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            last_exc = exc
            if not is_retryable(exc):
                logger.debug(f"[retry/{label}] 재시도 불가 에러: {exc}")
                raise

            if attempt >= max_retries:
                logger.warning(f"[retry/{label}] 최대 재시도 {max_retries}회 소진: {exc}")
                raise

            delay = min(base_delay * (2 ** attempt), max_delay)
            logger.info(f"[retry/{label}] 시도 {attempt+1}/{max_retries} 실패, {delay:.1f}s 후 재시도: {exc}")
            time.sleep(delay)

    raise last_exc
