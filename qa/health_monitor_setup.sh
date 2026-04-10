#!/bin/bash
# P002 배포 환경 30분 헬스체크 설정 스크립트
# 사용법: DEPLOY_URL=https://your-url.com bash health_monitor_setup.sh

DEPLOY_URL="${DEPLOY_URL:-PLACEHOLDER}"
LOG_FILE="/tmp/p002_health.log"
ALERT_FILE="/tmp/p002_alert.log"

if [ "$DEPLOY_URL" = "PLACEHOLDER" ]; then
  echo "❌ DEPLOY_URL 환경변수를 설정하세요."
  echo "   예시: DEPLOY_URL=https://copy-perp.vercel.app bash health_monitor_setup.sh"
  exit 1
fi

echo "▶ P002 헬스체크 cron 설정 중..."
echo "  URL: $DEPLOY_URL"
echo "  로그: $LOG_FILE"
echo "  알림: $ALERT_FILE"

# 기존 p002 cron 제거 후 새로 등록
(crontab -l 2>/dev/null | grep -v "p002_health"; \
 echo "*/30 * * * * curl -sf ${DEPLOY_URL}/healthz -o /dev/null && echo \"\$(date '+\%Y-\%m-\%d \%H:\%M') OK\" >> ${LOG_FILE} || echo \"\$(date '+\%Y-\%m-\%d \%H:\%M') ALERT: ${DEPLOY_URL} 응답 없음\" >> ${ALERT_FILE}") | crontab -

echo "✅ cron 등록 완료"
echo ""
echo "확인: crontab -l | grep p002"
echo "로그: tail -f $LOG_FILE"
echo "알림: cat $ALERT_FILE"
