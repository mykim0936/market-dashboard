# make_briefing.py — 지표+뉴스 기반 브리핑 생성
# 로컬(collect.py 파이프라인)에서는 claude -p CLI를 그대로 쓰고, 클라우드 배포본은
# claude CLI가 없으므로 Anthropic API를 requests로 직접 호출한다(생성 라이브러리처럼
# 다른 스크립트도 requests 기반이라 일관성을 위해 SDK 대신 raw HTTP를 쓴다).
import csv
import os
import shutil
import subprocess
from datetime import datetime, timezone

import requests

DATA_DIR = 'data'
INDICATORS_CSV = os.path.join(DATA_DIR, 'indicators.csv')
NEWS_CSV = os.path.join(DATA_DIR, 'news.csv')
BRIEFING_MD = os.path.join(DATA_DIR, 'briefing.md')

NEWS_LIMIT = 20
CLAUDE_TIMEOUT_SEC = 120

# 짧은 요약 작업이라 비용이 저렴한 Haiku로 충분하다.
ANTHROPIC_API_URL = 'https://api.anthropic.com/v1/messages'
ANTHROPIC_MODEL = 'claude-haiku-4-5'
ANTHROPIC_MAX_TOKENS = 500
ANTHROPIC_TIMEOUT_SEC = 30


def read_csv_rows(path):
    """파일이 없으면 None, 있으면 행 리스트(비어 있으면 [])를 반환."""
    if not os.path.exists(path):
        return None
    with open(path, encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def format_indicators(rows):
    lines = []
    for r in rows:
        lines.append(
            f"- {r.get('label')}: {r.get('value')} "
            f"(기준일 {r.get('as_of')}, 출처 {r.get('source')})"
        )
    return '\n'.join(lines)


def format_news(rows):
    lines = []
    for r in rows:
        lines.append(
            f"- [{r.get('published_at')}] {r.get('title')} "
            f"({r.get('source')}) - {r.get('excerpt')}"
        )
    return '\n'.join(lines)


def build_prompt(indicators_rows, news_rows):
    return f"""아래는 오늘 수집한 거시 지표와 최신 뉴스입니다. 이 브리핑을 작성하세요.

[규칙 - 반드시 지킬 것]
- 아래 입력에 없는 사실은 절대 언급하지 마세요.
- 형식은 반드시 "3줄 요약" + "테마 1줄"로 작성하세요.
- 전망, 투자 권유, 과장된 표현은 사용하지 마세요.

[지표]
{format_indicators(indicators_rows)}

[최신 뉴스 (최대 {NEWS_LIMIT}건)]
{format_news(news_rows)}
"""


def call_claude(prompt):
    claude_path = shutil.which('claude') or 'claude'  # Windows에서는 claude.cmd 등 확장자 포함 경로가 필요
    try:
        result = subprocess.run(
            [claude_path, '-p'],
            input=prompt,           # 인자 대신 표준입력으로 전달 (Windows .cmd 래퍼가 인자 내 줄바꿈을 command 구분자로 오인해 프롬프트가 잘리는 문제 방지)
            capture_output=True,
            text=True,
            encoding='utf-8',
            timeout=CLAUDE_TIMEOUT_SEC,
        )
    except FileNotFoundError:
        print('claude CLI를 찾을 수 없습니다. PATH에 설치되어 있는지 확인하세요.')
        return None
    except subprocess.TimeoutExpired:
        print(f'claude 호출이 {CLAUDE_TIMEOUT_SEC}초를 초과해 중단했습니다.')
        return None

    if result.returncode != 0:
        print(f'claude 호출 실패 (exit={result.returncode}): {result.stderr.strip()}')
        return None

    output = result.stdout.strip()
    if not output:
        print('claude 응답이 비어 있습니다.')
        return None

    return output


def generate_briefing_via_api(indicators_rows, news_rows):
    """app.py 에서 직접 호출하는 라이브러리 진입점 — claude CLI 없이 Anthropic API로
    브리핑 텍스트만 생성해 반환한다(파일 저장은 하지 않음). 키가 없거나 호출이
    실패하면 None을 반환한다."""
    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        return None

    prompt = build_prompt(indicators_rows, news_rows[:NEWS_LIMIT])

    try:
        resp = requests.post(
            ANTHROPIC_API_URL,
            headers={
                'x-api-key': api_key,
                'anthropic-version': '2023-06-01',
                'content-type': 'application/json',
            },
            json={
                'model': ANTHROPIC_MODEL,
                'max_tokens': ANTHROPIC_MAX_TOKENS,
                'messages': [{'role': 'user', 'content': prompt}],
            },
            timeout=ANTHROPIC_TIMEOUT_SEC,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.RequestException as e:
        print(f'브리핑 API 호출 실패: {e}')
        return None

    if data.get('stop_reason') == 'refusal':
        print('브리핑 API가 요청을 거부했습니다.')
        return None

    text_blocks = [b['text'] for b in data.get('content', []) if b.get('type') == 'text']
    output = ''.join(text_blocks).strip()
    return output or None


def save_briefing(text):
    generated_at = datetime.now(timezone.utc).isoformat()
    content = f"# 브리핑\n\n생성 시각: {generated_at}\n\n{text}\n"
    with open(BRIEFING_MD, 'w', encoding='utf-8') as f:
        f.write(content)
    print(f'-> {BRIEFING_MD} 저장 완료 (생성 시각 {generated_at})')


def main():
    indicators_rows = read_csv_rows(INDICATORS_CSV)
    if indicators_rows is None:
        print(f'{INDICATORS_CSV} 파일이 없어 브리핑 생성을 건너뜁니다.')
        return
    if not indicators_rows:
        print(f'{INDICATORS_CSV} 파일이 비어 있어 브리핑 생성을 건너뜁니다.')
        return

    news_rows = read_csv_rows(NEWS_CSV)
    if news_rows is None:
        print(f'{NEWS_CSV} 파일이 없어 브리핑 생성을 건너뜁니다.')
        return
    if not news_rows:
        print(f'{NEWS_CSV} 파일이 비어 있어 브리핑 생성을 건너뜁니다.')
        return

    prompt = build_prompt(indicators_rows, news_rows[:NEWS_LIMIT])

    output = call_claude(prompt)
    if output is None:
        print('브리핑 생성에 실패해 기존 briefing.md를 그대로 둡니다.')
        return

    save_briefing(output)


if __name__ == '__main__':
    main()
