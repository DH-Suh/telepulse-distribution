"""
5단계 — 텔레그램 전송

reports/YYYY-MM-DD.report.json 을 읽어 내 '저장된 메시지'로 보낸다.

봇을 만들지 않는다. 이미 로그인한 내 계정으로 나에게 보내면 되므로
send_message('me', ...) 한 줄이면 된다 (SPEC 11절).

사용법:
  python send.py                 # 가장 최근 리포트 전송
  python send.py 2026-08-04      # 날짜 지정
  python send.py --dry-run       # 보내지 않고 화면에만 출력 (텔레그램 연결 불필요)
"""

import asyncio
import html
import json
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent
REPORTS_DIR = ROOT / "reports"

# 텔레그램 한 통 제한은 4,096자. HTML 태그도 길이에 포함되므로 여유를 둔다.
LIMIT = 3900
# 메시지 사이 간격 — 순서 보장 및 요청 제한 예방
SEND_DELAY = 0.7


def b(text: str) -> str:
    return f"<b>{html.escape(text)}</b>"


def esc(text: str) -> str:
    """텔레그램 HTML 파서가 깨지지 않도록. 마크다운 대신 HTML을 쓰는 이유가 이것이다 —
    종목명이나 기사 제목에 * _ [ ] 가 섞여도 안전하다."""
    return html.escape(text)


def short_room(name: str) -> str:
    """방 이름을 화면용으로 줄인다. 원래 이름은 최대 36자라 그대로 쓰면 화면을 덮는다.

    이모지를 그냥 지우면 단어 경계까지 사라진다("아침속보🚀특징주" → "아침속보특징주").
    공백으로 치환한 뒤 어절 단위로 앞부분만 남긴다.
    대괄호 안에 브랜드명이 있으면 그쪽이 더 정확하다("… [○○리서치]" → "○○리서치").

    예시는 실제 방 이름을 쓰지 않는다 — 이 파일은 공개 저장소로 그대로 나간다."""
    bracket = re.search(r"[\[(]\s*([^\])]+?)\s*[\])]", name)
    if bracket:
        return bracket.group(1)[:16]
    head = re.split(r"[|｜]", name)[0]
    words = [w for w in re.sub(r"[^\w가-힣]+", " ", head).split()
             if not re.fullmatch(r"[Vv]er\w*|\d+", w)]      # Ver2.0 같은 꼬리표 제거
    return " ".join(words[:2])[:16]


# ══ 브리핑 → 메시지 조각들 ═══════════════════════════════════════════

def build_blocks(report: dict, day: str) -> list[tuple[str, str]]:
    """(섹션명, 조각) 목록. 조각 하나는 쪼개면 안 되는 최소 단위다.

    메시지를 자를 때 문장 중간이 아니라 이 조각 경계에서 자른다.
    종목 설명이 두 통에 걸쳐 잘리면 읽기 어렵기 때문이다."""
    blocks: list[tuple[str, str]] = []

    blocks.append(("머리", f"📈 {b(day + ' 시장 브리핑')}"))

    blocks.append(("테마", b("📊 오늘의 3대 시장 핵심 테마")))
    for i, t in enumerate(report["themes"], 1):
        tag = f" · {t['room_count']}개 방" if t.get("room_count") else ""
        lines = [f"{i}. {b(t['title'])}{esc(tag)}", esc(t["summary"])]
        if t.get("tickers"):
            lines.append(f"↳ {esc(', '.join(t['tickers']))}")
        blocks.append(("테마", "\n".join(lines)))

    blocks.append(("중복종목", b("🔥 다수 방 중복 언급 종목")))
    if not report["cross_validated"]:
        blocks.append(("중복종목", "해당 없음."))
    for c in report["cross_validated"]:
        lines = [f"{b(c['ticker'])} — {esc(str(c['room_count']))}개 방"]
        if c.get("rooms"):
            lines.append(f"<i>{esc(' / '.join(short_room(r) for r in c['rooms']))}</i>")
        lines.append(f"· 사실: {esc(c['news'])}")
        lines.append(f"· 해석: {esc(c['interpretation'])}")
        blocks.append(("중복종목", "\n".join(lines)))

    blocks.append(("해외", b("🌐 해외 뉴스 & 글로벌 증시 영향")))
    if not report["global"]:
        blocks.append(("해외", "해당 없음."))
    for g in report["global"]:
        blocks.append(("해외", "\n".join([
            b(g["topic"]), f"· {esc(g['point'])}", f"· 국내 영향: {esc(g['impact'])}"])))

    ins = report["insight"]
    blocks.append(("인사이트", b("💡 종합 투자 인사이트")))
    blocks.append(("인사이트", esc(ins["summary"])))
    if ins.get("watchlist"):
        blocks.append(("인사이트", b("관찰 대상") + "\n"
                       + "\n".join(f"· {esc(w)}" for w in ins["watchlist"])))
    if ins.get("warnings"):
        blocks.append(("인사이트", b("주의") + "\n"
                       + "\n".join(f"⚠️ {esc(w)}" for w in ins["warnings"])))

    # 면책 문구는 섹션명을 앞과 같게 둔다 — 따로 두면 58자짜리 메시지가 한 통을 차지한다
    blocks.append(("인사이트", "<i>투자 판단 보조 자료이며 매매 신호가 아닙니다. "
                             "원문은 검증되지 않은 텔레그램 메시지입니다.</i>"))
    return blocks


def split_long(text: str) -> list[str]:
    """조각 하나가 한 통을 넘으면 줄 단위로 쪼갠다. 마지막 안전장치."""
    out, cur = [], ""
    for line in text.split("\n"):
        while len(line) > LIMIT:              # 한 줄 자체가 넘치는 극단적 경우
            out.append(line[:LIMIT])
            line = line[LIMIT:]
        if len(cur) + len(line) + 1 > LIMIT:
            out.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out


def pack(blocks: list[tuple[str, str]]) -> list[str]:
    """조각들을 4,096자 한도 안에서 메시지로 묶는다.

    섹션이 바뀌면 새 메시지를 시작한다. 한 통에 우겨넣는 것보다
    섹션마다 한 통씩 오는 편이 아침에 읽기 좋다."""
    messages: list[str] = []
    cur, cur_section = "", None

    for section, text in blocks:
        for piece in (split_long(text) if len(text) > LIMIT else [text]):
            starts_section = section != cur_section and cur_section not in (None, "머리")
            if cur and (starts_section or len(cur) + len(piece) + 2 > LIMIT):
                messages.append(cur)
                cur = ""
            cur = f"{cur}\n\n{piece}" if cur else piece
            cur_section = section
    if cur:
        messages.append(cur)
    return messages


# ══ 전송 ═════════════════════════════════════════════════════════════

async def send_all(messages: list[str]) -> None:
    load_dotenv(ROOT / ".env")
    api_id, api_hash = os.getenv("TELEGRAM_API_ID"), os.getenv("TELEGRAM_API_HASH")
    session = os.getenv("TELEGRAM_SESSION")
    if not (api_id and api_hash and session):
        sys.exit("[중단] .env 의 TELEGRAM_API_ID / API_HASH / SESSION 을 확인하세요.")

    client = TelegramClient(StringSession(session), int(api_id), api_hash)
    await client.start()
    me = await client.get_me()
    print(f"로그인: {me.first_name or ''} (@{me.username or '-'})")

    for i, msg in enumerate(messages, 1):
        try:
            await client.send_message("me", msg, parse_mode="html",
                                      link_preview=False)
        except FloodWaitError as e:
            print(f"  [대기] 요청 제한 — {e.seconds}초")
            await asyncio.sleep(e.seconds + 1)
            await client.send_message("me", msg, parse_mode="html",
                                      link_preview=False)
        print(f"  전송 {i}/{len(messages)}  ({len(msg):,}자)")
        await asyncio.sleep(SEND_DELAY)

    await client.disconnect()


# ══ 실행 ═════════════════════════════════════════════════════════════

def pick_report(args: list[str]) -> Path:
    days = [a for a in args if re.fullmatch(r"\d{4}-\d{2}-\d{2}", a)]
    if days:
        p = REPORTS_DIR / f"{days[0]}.report.json"
        if not p.exists():
            sys.exit(f"[중단] {p.name} 이 없습니다. summarize.py 를 먼저 실행하세요.")
        return p
    files = sorted(REPORTS_DIR.glob("*.report.json"))
    if not files:
        sys.exit("[중단] 리포트가 없습니다. summarize.py 를 먼저 실행하세요.")
    return files[-1]


def main() -> None:
    src = pick_report(sys.argv[1:])
    day = src.name.removesuffix(".report.json")
    report = json.loads(src.read_text(encoding="utf-8"))
    messages = pack(build_blocks(report, day))

    print(f"입력: reports/{src.name}")
    print(f"메시지 {len(messages)}통으로 분할 "
          f"(각 {', '.join(f'{len(m):,}' for m in messages)}자)")
    over = [i for i, m in enumerate(messages, 1) if len(m) > 4096]
    if over:
        sys.exit(f"[중단] {over}번 메시지가 4,096자를 넘습니다. 분할 로직 확인 필요.")
    print("  → 모두 4,096자 이내\n")

    if "--dry-run" in sys.argv:
        for i, m in enumerate(messages, 1):
            print(f"{'─' * 60}\n[{i}/{len(messages)}] {len(m):,}자\n{'─' * 60}")
            print(re.sub(r"<[^>]+>", "", m))     # 태그를 지우고 실제 보일 모습으로
            print()
        print("(--dry-run: 전송하지 않았습니다)")
        return

    asyncio.run(send_all(messages))
    print(f"\n완료 — 텔레그램 '저장된 메시지'를 확인하세요.")


if __name__ == "__main__":
    main()
