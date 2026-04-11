# SQLite는 단일 프로세스 필수 (workers=2 → database is locked)
web: uvicorn api.main:app --host 0.0.0.0 --port $PORT --workers 1
