@echo off
chcp 65001 > nul
cd /d "%~dp0"
echo 실거래가 검색 화면을 여는 중입니다... (이 창을 닫으면 종료)
py -m streamlit run app.py --server.headless false --browser.gatherUsageStats false
pause
