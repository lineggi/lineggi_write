#!/bin/bash
# ============================================================
#  뉴스 브리핑 봇 실행 (macOS 더블클릭용)
#  이 파일을 더블클릭하면 봇이 실행됩니다.
# ============================================================

# 이 스크립트가 있는 폴더로 이동 (.env, credentials.json 을 읽기 위해)
cd "$(dirname "$0")" || exit 1

echo "📂 실행 위치: $(pwd)"

# 가상환경(venv)이 없으면 최초 1회 자동 생성 + 패키지 설치
if [ ! -d "venv" ]; then
    echo "🔧 최초 실행 준비 중... 가상환경 생성 및 패키지 설치 (1~2분 걸릴 수 있어요)"
    python3 -m venv venv || { echo "❌ 가상환경 생성 실패 — python3 가 설치되어 있는지 확인하세요."; read -r; exit 1; }
    source venv/bin/activate
    python3 -m pip install --quiet --upgrade pip
    python3 -m pip install --quiet -r requirements.txt || { echo "❌ 패키지 설치 실패"; read -r; exit 1; }
else
    source venv/bin/activate
fi

# .env 파일 확인
if [ ! -f ".env" ]; then
    echo "⚠️  .env 파일이 없습니다. .env.example 을 복사해 키를 채워주세요."
fi

echo "🚀 봇을 실행합니다... (폰 텔레그램을 확인하세요)"
echo "   중지하려면 이 창에서 Control+C 를 누르거나 창을 닫으면 됩니다."
echo "------------------------------------------------------------"

python3 main.py

# 봇이 종료되거나 에러가 나도 창이 바로 닫히지 않도록 대기
echo "------------------------------------------------------------"
echo "🛑 봇이 종료되었습니다. 이 창은 닫으셔도 됩니다. (Enter 키를 누르면 닫힙니다)"
read -r
