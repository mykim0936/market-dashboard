@echo off
rem run_quick_refresh.bat — 작업 스케줄러가 호출하는 장중 빠른 갱신 스크립트.
rem 지수/보유종목/거시지표/뉴스만 받아온다(collect.py --quick).
cd /d "C:\Users\mykim\Claude\Projects"
set PYTHONIOENCODING=utf-8
"C:\Users\mykim\AppData\Local\Python\pythoncore-3.14-64\python.exe" collect.py --quick >> data\quick_refresh.log 2>&1
