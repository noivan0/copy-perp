"""
Error alerts + monitoring system
Production level: alerts on order failure, disconnect, server error
"""
import logging
import os
import time
from collections import deque
from typing import Optional

logger = logging.getLogger(__name__)

# 텔레그램 알림 (ALERT_TELEGRAM_TOKEN + ALERT_TELEGRAM_CHAT_ID 설정 시 활성)
ALERT_BOT_TOKEN = os.getenv("ALERT_TELEGRAM_TOKEN", "")
ALERT_CHAT_ID   = os.getenv("ALERT_TELEGRAM_CHAT_ID", "")

# 최근 알림 중복 방지 (동일 메시지 60s 이내 재알림 차단)
_recent_alerts: deque = deque(maxlen=100)
_DEDUP_WINDOW  = 60  # seconds


def _is_duplicate(msg: str) -> bool:
    now = time.time()
    for ts, m in _recent_alerts:
        if m == msg and now - ts < _DEDUP_WINDOW:
            return True
    return False


def _send_telegram(text: str) -> bool:
    """Send Telegram message (skip if not configured)"""
    if not ALERT_BOT_TOKEN or not ALERT_CHAT_ID:
        return False
    try:
        import urllib.request, json
        payload = json.dumps({"chat_id": ALERT_CHAT_ID, "text": text, "parse_mode": "HTML"}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{ALERT_BOT_TOKEN}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=5)
        return True
    except Exception as e:
        logger.debug(f"Telegram alert failed: {e}")
        return False


class AlertManager:
    """
    Central alert manager
    - Error alert on order failure
    - Warning on monitor disconnect
    - INFO on server restart
    """

    def __init__(self):
        self._error_count: dict[str, int] = {}
        self._last_alert:  dict[str, float] = {}
        # 최근 이벤트 로그 (API /events 에서 조회 가능)
        self.events: deque = deque(maxlen=500)

    def _log_event(self, level: str, category: str, msg: str):
        self.events.append({
            "ts": int(time.time()),
            "level": level,
            "category": category,
            "msg": msg,
        })

    def order_failed(self, follower: str, symbol: str, side: str, error: str):
        """Order failure notification"""
        key = f"order_fail:{follower[:8]}"
        self._error_count[key] = self._error_count.get(key, 0) + 1
        count = self._error_count[key]

        msg = f"🚨 Order failed [{follower[:8]}] {symbol} {side}\n{error}"
        self._log_event("error", "order", msg)

        # Alert on 3+ consecutive failures
        if count >= 3 and not _is_duplicate(msg):
            logger.error(msg)
            _recent_alerts.append((time.time(), msg))
            if ALERT_BOT_TOKEN:
                _send_telegram(f"<b>Copy Perp Order Failed</b>\nFollower: {follower[:12]}...\nSymbol: {symbol} {side}\nConsecutive failures: {count}\nError: {error[:100]}")
        else:
            logger.warning(msg)

    def order_success(self, follower: str, symbol: str, side: str, amount: str):
        """Order success — reset error counter"""
        key = f"order_fail:{follower[:8]}"
        self._error_count.pop(key, None)
        self._log_event("info", "order", f"✅ Order success [{follower[:8]}] {symbol} {side} {amount}")

    def monitor_disconnected(self, trader: str, reason: str):
        """Monitor disconnected warning"""
        msg = f"⚠️ Monitor disconnected [{trader[:12]}]: {reason}"
        self._log_event("warning", "monitor", msg)
        if not _is_duplicate(msg):
            logger.warning(msg)
            _recent_alerts.append((time.time(), msg))
            if ALERT_BOT_TOKEN:
                _send_telegram(f"<b>Copy Perp Monitor Disconnected</b>\nTrader: {trader[:16]}...\nReason: {reason[:100]}")

    def monitor_restored(self, trader: str):
        """Monitor restored notification"""
        msg = f"✅ Monitor restored [{trader[:12]}]"
        self._log_event("info", "monitor", msg)
        logger.info(msg)

    def server_started(self, network: str, monitors: int):
        """Server startup notification"""
        msg = f"🚀 Copy Perp server started | NETWORK={network} | monitors={monitors}"
        self._log_event("info", "server", msg)
        logger.info(msg)
        if ALERT_BOT_TOKEN:
            _send_telegram(f"<b>Copy Perp Started</b>\nNETWORK: {network}\nMonitors: {monitors}")

    def get_recent_events(self, limit: int = 50, level: Optional[str] = None) -> list:
        """Get recent events"""
        events = list(self.events)
        if level:
            events = [e for e in events if e["level"] == level]
        return events[-limit:]

    def get_error_summary(self) -> dict:
        """Error summary"""
        return {
            "total_error_counts": dict(self._error_count),
            "recent_events": len(self.events),
            "alert_telegram": bool(ALERT_BOT_TOKEN),
        }


# 글로벌 싱글턴
_alert_manager: Optional[AlertManager] = None


def get_alert_manager() -> AlertManager:
    global _alert_manager
    if _alert_manager is None:
        _alert_manager = AlertManager()
        # retry.py 알림 훅 등록
        from core.retry import register_alert_hook
        register_alert_hook(lambda level, msg: _alert_manager._log_event(level, "retry", msg))
    return _alert_manager
