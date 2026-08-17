# fetch_news.py — 금융 뉴스 RSS 수집 스크립트 (제목/링크/발행시각/출처/짧은 발췌만)
import csv
import html.entities
import os
import re
import socket
import time
from datetime import datetime, timedelta, timezone

import feedparser
import requests

# feedparser.parse() 는 내부적으로 urllib 을 쓰는데 자체 타임아웃이 없어 피드 서버가
# 응답을 멈추면 무한 대기한다. 소켓 기본 타임아웃으로 상한을 건다.
socket.setdefaulttimeout(15)

DATA_DIR = 'data'
OUTPUT_CSV = os.path.join(DATA_DIR, 'news.csv')

EXCERPT_MAX_LEN = 150
RECENT_HOURS = 24

# (region, url) — region 은 대시보드에서 국내/해외 탭을 나누는 기준이다.
# 필요한 만큼 자유롭게 추가/삭제
FEEDS = [
    ('국내', 'https://www.yna.co.kr/rss/economy.xml'),
    ('국내', 'https://www.hankyung.com/feed/economy'),
    ('국내', 'https://www.mk.co.kr/rss/30100041/'),
    ('국내', 'https://www.mk.co.kr/rss/50200011/'),
    ('해외', 'https://finance.yahoo.com/news/rssindex'),
    ('해외', 'https://feeds.content.dowjones.io/public/rss/mw_marketpulse'),
    ('해외', 'https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258'),
    ('해외', 'https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10001147'),
    ('해외', 'https://www.ft.com/markets?format=rss'),
    ('해외', 'https://www.investing.com/rss/news_25.rss'),
]


def make_excerpt(entry):
    raw = entry.get('summary') or entry.get('description') or ''
    text = ' '.join(raw.split())  # 태그/개행 제거는 안 하므로 완벽하진 않지만 길이만 제한
    import re
    text = re.sub('<[^<]+?>', '', text)  # 간단한 HTML 태그 제거
    if len(text) > EXCERPT_MAX_LEN:
        text = text[:EXCERPT_MAX_LEN].rstrip() + '...'
    return text


def parse_published(entry):
    for field in ('published_parsed', 'updated_parsed'):
        value = entry.get(field)
        if value:
            return datetime(*value[:6], tzinfo=timezone.utc)
    return None


# XML이 기본으로 아는 엔티티는 이 5개뿐이라, &nbsp; 같은 HTML 엔티티가 그대로
# 들어있는 피드(예: 한국경제)는 파서가 "undefined entity"로 통째로 실패한다.
XML_SAFE_ENTITIES = {'amp', 'lt', 'gt', 'quot', 'apos'}
ENTITY_RE = re.compile(rb'&([a-zA-Z][a-zA-Z0-9]*);')
FETCH_HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; market-dashboard/1.0)'}


def sanitize_entities(raw):
    """정의되지 않은 HTML 엔티티를 숫자 참조로 바꿔 XML 파서가 읽을 수 있게 만든다."""
    def repl(m):
        name = m.group(1).decode('ascii')
        if name in XML_SAFE_ENTITIES:
            return m.group(0)
        codepoint = html.entities.name2codepoint.get(name)
        return f'&#{codepoint};'.encode('ascii') if codepoint else m.group(0)

    return ENTITY_RE.sub(repl, raw)


def parse_with_fallback(url):
    """일반 파싱을 먼저 시도하고, 엔티티 문제로 깨지면 정제 후 재파싱한다."""
    parsed = feedparser.parse(url)
    if parsed.entries:
        return parsed

    resp = requests.get(url, headers=FETCH_HEADERS, timeout=15)
    resp.raise_for_status()
    return feedparser.parse(sanitize_entities(resp.content))


def fetch_feed(url, region):
    parsed = parse_with_fallback(url)

    if parsed.bozo and not parsed.entries:
        raise RuntimeError(str(parsed.bozo_exception))
    if parsed.get('status') and parsed.status >= 400:
        raise RuntimeError(f"HTTP {parsed.status}")

    source = parsed.feed.get('title', url)
    articles = []
    for entry in parsed.entries:
        published = parse_published(entry)
        articles.append({
            'title': entry.get('title', '').strip(),
            'link': entry.get('link', '').strip(),
            'published_at': published,
            'region': region,
            'source': source,
            'excerpt': make_excerpt(entry),
        })
    return articles


def collect_articles():
    all_articles = []
    failed_feeds = []

    for region, url in FEEDS:
        try:
            articles = fetch_feed(url, region)
            all_articles.extend(articles)
            print(f"[OK] [{region}] {url}: {len(articles)}건")
        except Exception as e:
            failed_feeds.append(url)
            print(f"[FAIL] [{region}] {url}: {e}")

    if failed_feeds:
        print('수집 실패한 피드:', ', '.join(failed_feeds))

    return all_articles


def dedupe_and_filter(articles):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=RECENT_HOURS)

    seen_links = set()
    result = []
    for a in articles:
        if not a['link'] or a['link'] in seen_links:
            continue
        if a['published_at'] is None or a['published_at'] < cutoff:
            continue
        seen_links.add(a['link'])
        result.append(a)

    result.sort(key=lambda a: a['published_at'], reverse=True)
    return result


def fetch_recent_news():
    """app.py 에서 직접 호출하는 라이브러리 진입점. CSV로 저장하지 않고 바로 반환한다."""
    return dedupe_and_filter(collect_articles())


def save_news(articles):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(OUTPUT_CSV, 'w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=['title', 'link', 'published_at', 'region', 'source', 'excerpt'])
        writer.writeheader()
        for a in articles:
            writer.writerow({
                'title': a['title'],
                'link': a['link'],
                'published_at': a['published_at'].isoformat(),
                'region': a['region'],
                'source': a['source'],
                'excerpt': a['excerpt'],
            })
    print(f"-> {len(articles)}건을 {OUTPUT_CSV} 에 저장했습니다.")


def main():
    articles = collect_articles()
    filtered = dedupe_and_filter(articles)
    save_news(filtered)
    for region in ('국내', '해외'):
        print(f"   {region}: {sum(1 for a in filtered if a['region'] == region)}건")


if __name__ == '__main__':
    main()
