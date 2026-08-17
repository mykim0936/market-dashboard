# portfolio_to_secrets.py — portfolio.csv를 Streamlit Secrets용 TOML로 변환해 클립보드에 복사
#
# 로컬 portfolio.csv 하나만 고치고 이 스크립트를 실행하면, 클라우드 Secrets 편집창에
# 붙여넣을 수 있는 전체 내용(API 키 + DASHBOARD_PASSWORD 포함)이 클립보드에 준비된다.
# API 키와 비밀번호는 이미 저장된 .streamlit/secrets.toml 에서 그대로 가져오고,
# [[portfolio]] 부분만 portfolio.csv 최신 내용으로 새로 만든다.
import os
import re
import sys

import pandas as pd

PORTFOLIO_CSV = 'portfolio.csv'
SECRETS_TOML = os.path.join('.streamlit', 'secrets.toml')


def toml_escape(value):
    return str(value).replace('\\', '\\\\').replace('"', '\\"')


def build_portfolio_toml(df):
    blocks = []
    for _, row in df.iterrows():
        blocks.append(
            "[[portfolio]]\n"
            f'name = "{toml_escape(row["name"])}"\n'
            f'ticker = "{toml_escape(row["ticker"])}"\n'
            f"quantity = {row['quantity']}\n"
            f"avg_price = {row['avg_price']}\n"
        )
    return '\n'.join(blocks)


def load_holdings():
    if not os.path.exists(PORTFOLIO_CSV):
        print(f"[FAIL] {PORTFOLIO_CSV} 가 없습니다.")
        sys.exit(1)
    df = pd.read_csv(PORTFOLIO_CSV, encoding='utf-8-sig', dtype={'ticker': str})
    df['ticker'] = df['ticker'].str.strip()
    return df


def load_non_portfolio_secrets():
    """기존 secrets.toml 에서 [[portfolio]] 블록을 제외한 나머지(API 키, 비밀번호)를 그대로 가져온다."""
    if not os.path.exists(SECRETS_TOML):
        print(f"[WARN] {SECRETS_TOML} 이 없어 API 키/비밀번호 없이 [[portfolio]]만 만듭니다.")
        return ''
    with open(SECRETS_TOML, encoding='utf-8') as f:
        content = f.read()
    # [[portfolio]] 로 시작하는 블록들을 통째로 제거하고 나머지(키/비밀번호/주석)만 남긴다.
    content = re.sub(r'\[\[portfolio\]\].*?(?=\n\[\[portfolio\]\]|\Z)', '', content, flags=re.DOTALL)
    return content.rstrip() + '\n'


def copy_to_clipboard(text):
    try:
        import pyperclip
        pyperclip.copy(text)
        return True
    except ImportError:
        pass

    # pyperclip 이 없으면 OS 기본 클립보드 명령으로 시도한다.
    try:
        if sys.platform == 'win32':
            import subprocess
            subprocess.run('clip', input=text, text=True, check=True, shell=True)
            return True
    except Exception:
        pass
    return False


def main():
    holdings = load_holdings()
    header = load_non_portfolio_secrets()
    portfolio_toml = build_portfolio_toml(holdings)
    full_text = f"{header}\n{portfolio_toml}" if header else portfolio_toml

    print(f"-> {PORTFOLIO_CSV} 의 {len(holdings)}개 종목을 TOML로 변환했습니다.\n")
    print(full_text)

    os.makedirs(os.path.dirname(SECRETS_TOML), exist_ok=True)
    with open(SECRETS_TOML, 'w', encoding='utf-8') as f:
        f.write(full_text)
    print(f"-> {SECRETS_TOML} 파일도 최신 내용으로 갱신했습니다.")

    if copy_to_clipboard(full_text):
        print("\n-> 클립보드에 복사 완료. Streamlit Cloud의 Settings > Secrets 에 그대로 붙여넣으세요.")
    else:
        print("\n-> 클립보드 복사에 실패했습니다. 위 내용을 직접 복사해 사용하세요.")
        print("   (pip install pyperclip 을 하면 다음부터 자동 복사됩니다.)")


if __name__ == '__main__':
    main()
