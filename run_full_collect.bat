@echo off
rem run_full_collect.bat — 작업 스케줄러가 호출하는 장 마감 후 전체 수집 스크립트.
rem 시총 상위/투자자 수급까지 포함한 collect.py 전체를 실행한다.
cd /d "C:\Users\mykim\Claude\Projects"
set PYTHONIOENCODING=utf-8
"C:\Users\mykim\AppData\Local\Python\pythoncore-3.14-64\python.exe" collect.py >> data\full_collect.log 2>&1
