#!/usr/bin/env bash
# ==============================================================
# 뉴스 웹 서버 및 UI 자동 검사 원클릭 실행 스크립트
# ==============================================================
set -e

PORT=${1:-8000}
TARGET_URL="http://localhost:${PORT}"

echo "🔎 [1/3] 서버 구동 확인 중 (${TARGET_URL})..."

# 서버가 이미 실행 중인지 확인
SERVER_PID=""
if curl -s -o /dev/null -w "%{http_code}" "${TARGET_URL}" | grep -q "200\|301\|302"; then
    echo " -> 이미 ${TARGET_URL}에서 서버가 실행 중입니다."
else
    echo " -> 로컬 백엔드 서버(server.py)를 포트 ${PORT}로 백그라운드 시작합니다..."
    python3 server.py "${PORT}" > /tmp/news_server.log 2>&1 &
    SERVER_PID=$!
    sleep 2
fi

cleanup() {
    if [ -n "$SERVER_PID" ]; then
        echo "🛑 임시 구동된 테스트 서버(PID: $SERVER_PID)를 종료합니다..."
        kill "$SERVER_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT

echo "🚀 [2/3] Playwright 자동 E2E 검사 실행..."
python3 test_news_page.py --url "${TARGET_URL}"

echo "🎉 [3/3] 모든 기능 검사가 성공적으로 완료되었습니다!"
