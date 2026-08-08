"""
2단계 — 텔레그램 메시지 수집

rooms.json 에서 "enabled": true 인 방들의 최근 N시간 메시지를 긁어와
data/YYYY-MM-DD.jsonl 파일로 저장한다.

이 파일이 이후 모든 작업의 입력이 된다. 한 번 저장해두면 프롬프트를 몇 번
고치든 텔레그램을 다시 긁을 필요가 없다(SPEC.md 원칙 4).

사용법:
  python collect.py                # 최근 24시간
  python collect.py --hours 48     # 최근 48시간 (테스트 데이터 더 확보용)
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession
from telethon.tl.types import MessageMediaWebPage, WebPage

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent
ROOMS_FILE = ROOT / "rooms.json"
DATA_DIR = ROOT / "data"
KST = timezone(timedelta(hours=9), "KST")  # 한국은 서머타임이 없어 고정 오프셋으로 충분

# 방 하나당 최대 조회 메시지 수 (폭주 방지 안전장치)
PER_ROOM_LIMIT = 3000
# 방 사이 대기 — 계정 제한(FloodWait) 예방
ROOM_DELAY = 0.5

# 브리핑 기준 시각 — 매일 이 시각을 경계로 하루를 자른다 (KST)
CUTOFF_HOUR = 8

HOURS = 24
if "--hours" in sys.argv:
    HOURS = int(sys.argv[sys.argv.index("--hours") + 1])


def daily_window(now: datetime, hours: int = HOURS) -> tuple[datetime, datetime]:
    """수집 구간을 '직전 08:00 기준 24시간'으로 고정한다. → 전날 08:00 ~ 당일 08:00

    실행 시각부터 거꾸로 24시간을 세면 안 된다. GitHub Actions 예약 실행은 수십 분
    늦을 수 있는데(10절), 08:25에 돌면 전날 08:25부터 보게 되어 **전날 08:00~08:25 뉴스가
    영구히 누락된다.** 매일 조금씩 새는 구조가 된다.

    경계를 고정하면 몇 분 늦게 실행돼도 창이 동일하다. 같은 날 두 번 실행해도 결과가 같다.
    """
    end = now.astimezone(KST).replace(hour=CUTOFF_HOUR, minute=0, second=0, microsecond=0)
    if now.astimezone(KST) < end:      # 08:00 이전 실행이면 종료 경계는 어제 08:00
        end -= timedelta(days=1)
    return end - timedelta(hours=hours), end

load_dotenv(ROOT / ".env")
API_ID = os.getenv("TELEGRAM_API_ID")
API_HASH = os.getenv("TELEGRAM_API_HASH")
SESSION = os.getenv("TELEGRAM_SESSION")

if not (API_ID and API_HASH and SESSION):
    sys.exit("[중단] .env 의 TELEGRAM_API_ID / API_HASH / SESSION 을 확인하세요. "
             "SESSION 이 비어 있으면 list_rooms.py 를 먼저 실행하세요.")


def load_targets() -> list:
    """수집 대상(enabled=true) 방만 골라온다."""
    if not ROOMS_FILE.exists():
        sys.exit("[중단] rooms.json 이 없습니다. list_rooms.py 를 먼저 실행하세요.")
    rooms = json.loads(ROOMS_FILE.read_text(encoding="utf-8-sig"))
    targets = [r for r in rooms if r["enabled"] is True]
    if not targets:
        sys.exit("[중단] 수집 대상이 없습니다. rooms.json 에서 enabled 를 true 로 바꾸세요.")
    return targets


def extract_preview(msg) -> dict | None:
    """메시지에 붙은 링크 미리보기(제목+요약)를 뽑는다.
    뉴스 기사를 직접 크롤링하지 않아도 되는 이유 — 텔레그램이 이미 만들어 놨다."""
    media = getattr(msg, "media", None)
    if not isinstance(media, MessageMediaWebPage):
        return None
    page = getattr(media, "webpage", None)
    if not isinstance(page, WebPage):
        return None  # WebPageEmpty / WebPagePending 등
    return {
        "title": page.title,
        "description": page.description,
        "url": page.url,
        "site": page.site_name,
    }


def extract_forward(msg) -> str | None:
    """다른 채널에서 퍼온 글이면 원본 출처명. 중복 판단에 쓰인다."""
    fwd = getattr(msg, "forward", None)
    if not fwd:
        return None
    chat = getattr(fwd, "chat", None)
    if chat is not None and getattr(chat, "title", None):
        return chat.title
    return getattr(fwd, "from_name", None)


async def collect_room(client, dialog, room_name, since, until) -> list:
    """한 방의 최근 메시지를 최신순으로 읽다가 기준 시각 이전이 나오면 중단.
    until 이후(=경계 08:00 이후)의 메시지는 다음 날 몫이므로 건너뛴다."""
    rows = []
    async for msg in client.iter_messages(dialog.entity, limit=PER_ROOM_LIMIT):
        if msg.date < since:
            break
        if msg.date >= until:
            continue          # 아직 오늘 브리핑 대상이 아님 — 내일 잡힌다
        text = (msg.message or "").strip()
        preview = extract_preview(msg)
        has_image = msg.photo is not None

        # 텍스트도 미리보기도 없는 순수 미디어(스티커·짤방)는 v1에서 쓰지 않지만,
        # "이미지 판독이 실제로 필요한가"를 나중에 판단하려면 흔적은 남겨야 한다.
        rows.append({
            "room": room_name,
            "room_id": dialog.id,
            "msg_id": msg.id,
            "date": msg.date.astimezone(KST).isoformat(),
            "text": text,
            "preview": preview,
            "has_image": has_image,
            "forward_from": extract_forward(msg),
            "views": getattr(msg, "views", None),
        })
    return rows


async def main():
    targets = load_targets()
    since, until = daily_window(datetime.now(timezone.utc))
    # 파일명은 실행일이 아니라 **데이터 범위**로 붙인다. 실행이 늦어도 이름이 흔들리지 않는다.
    out_path = DATA_DIR / f"{until.strftime('%Y-%m-%d')}.jsonl"

    print(f"수집 대상 {len(targets)}개 방")
    print(f"수집 구간: {since:%Y-%m-%d %H:%M} ~ {until:%Y-%m-%d %H:%M} KST ({HOURS}시간)")
    print(f"저장 파일: {out_path.name}\n")

    client = TelegramClient(StringSession(SESSION), int(API_ID), API_HASH)
    await client.start()

    # StringSession 은 방 정보를 캐시하지 않는다. 방 목록을 한 번 훑어
    # id -> dialog 를 만들어 두면 이후 조회가 안전하다.
    dialogs = {d.id: d async for d in client.iter_dialogs()}

    all_rows = []
    stats = []
    for r in targets:
        dialog = dialogs.get(r["id"])
        if dialog is None:
            print(f"  [건너뜀] {r['name']} — 방을 찾을 수 없음(나갔거나 삭제됨)")
            stats.append((r["name"], 0))
            continue

        try:
            rows = await collect_room(client, dialog, r["name"], since, until)
        except FloodWaitError as e:
            print(f"  [대기] 텔레그램 요청 제한 — {e.seconds}초 기다립니다")
            await asyncio.sleep(e.seconds + 1)
            rows = await collect_room(client, dialog, r["name"], since, until)

        all_rows.extend(rows)
        stats.append((r["name"], len(rows)))
        print(f"  {len(rows):>5}건  {r['name']}")
        await asyncio.sleep(ROOM_DELAY)

    await client.disconnect()

    # ── 저장 (오래된 것 → 최신 순으로 정렬) ────────────────────────────
    all_rows.sort(key=lambda x: x["date"])
    DATA_DIR.mkdir(exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for row in all_rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    # ── 요약 ────────────────────────────────────────────────────────────
    n = len(all_rows)
    with_text = sum(1 for r in all_rows if r["text"])
    with_preview = sum(1 for r in all_rows if r["preview"])
    with_image = sum(1 for r in all_rows if r["has_image"])
    empty = sum(1 for r in all_rows if not r["text"] and not r["preview"])
    forwarded = sum(1 for r in all_rows if r["forward_from"])
    chars = sum(len(r["text"]) for r in all_rows)

    # ── PC 시계 검사 ────────────────────────────────────────────────────
    # 텔레그램 서버가 찍은 시각이 이 PC 시각보다 미래라면, 틀린 쪽은 PC다.
    # 시계가 밀리면 수집 구간과 파일명이 통째로 어긋나는데, 결과물만 봐서는
    # 알아채기 어렵다(실측: 26시간 밀린 채로 50시간치가 수집된 적이 있음).
    if all_rows:
        newest = max(datetime.fromisoformat(r["date"]) for r in all_rows)
        skew = (newest - datetime.now(KST)).total_seconds()
        if skew > 300:
            print(f"\n{'!' * 60}")
            print(f"[경고] 이 PC의 시계가 최소 {skew / 3600:.1f}시간 느립니다.")
            print(f"  텔레그램 메시지 최신 시각 {newest:%Y-%m-%d %H:%M} > "
                  f"PC 현재 시각 {datetime.now(KST):%Y-%m-%d %H:%M}")
            print(f"  → 수집 구간과 파일명이 어긋났습니다. 시계를 맞춘 뒤 다시 수집하세요.")
            print(f"  → 설정 > 시간 및 언어 > 날짜 및 시간 > '지금 동기화'")
            print(f"{'!' * 60}")

    print(f"\n{'=' * 60}")
    print(f"총 {n:,}건 수집 → {out_path.relative_to(ROOT)}")
    print(f"{'=' * 60}")
    print(f"  본문 있음      {with_text:>6,}건")
    print(f"  링크 미리보기  {with_preview:>6,}건")
    print(f"  이미지 포함    {with_image:>6,}건")
    print(f"  내용 없음      {empty:>6,}건  (스티커·짤방 등)")
    print(f"  퍼온 글        {forwarded:>6,}건")
    print(f"  전체 글자 수   {chars:>6,}자  (대략 {chars // 2:,} 토큰)")
    print(f"\n다음: 이 파일로 3단계(필터·중복 묶기)를 진행합니다.")


if __name__ == "__main__":
    asyncio.run(main())
