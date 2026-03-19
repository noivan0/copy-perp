#!/usr/bin/env bash
pkill -f "run_papertrading.py" 2>/dev/null && echo "페이퍼트레이딩 종료" || echo "실행 중인 프로세스 없음"
pkill -f "run_longterm.py"     2>/dev/null || true
