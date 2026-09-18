@echo off
rem run_recommendations.bat — 작업 스케줄러가 호출하는 종목추천 스캔 스크립트.
rem 장 마감 후 전체 수집(MarketDashboardFullCollect, 15:50)이 kospi.csv를
rem 갱신한 뒤에 돌아야 그날 거래일이 거래일 목록에 포함된다.
cd /d "C:\Users\mykim\Claude\Projects"
set PYTHONIOENCODING=utf-8
"C:\Users\mykim\AppData\Local\Python\pythoncore-3.14-64\python.exe" collect_recommendations.py >> data\recommendations_collect.log 2>&1
