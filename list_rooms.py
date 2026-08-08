"""
방 목록 동기화 — 1단계에서 처음 쓰고, 이후에도 계속 쓰는 도구

하는 일:
  1) 텔레그램 로그인 (최초 1회만 인증 코드 입력)
  2) 세션 문자열 발급 → .env 의 TELEGRAM_SESSION 에 붙여넣으면 이후 자동 로그인
  3) 현재 참여 중인 채널/그룹을 rooms.json 과 동기화
       - 기존 방: 내가 정해둔 enabled 값을 그대로 보존
       - 새로 가입한 방: enabled=null (미결정) 로 추가 + 경고 출력
       - 나간 방: 목록에서 제거
  4) enabled=true 인 방만 2단계 수집 대상이 된다

사용법:
  .venv\\Scripts\\python.exe list_rooms.py            # 목록/동기화만 (빠름)
  .venv\\Scripts\\python.exe list_rooms.py --count    # 최근 24h 메시지 수까지 (권장, 느림)
  .venv\\Scripts\\python.exe list_rooms.py --review   # 미결정 방을 y/n 으로 정하기 (오프라인)
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

# 윈도우 콘솔에서 한글/이모지 방 이름이 깨지지 않도록
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent
ROOMS_FILE = ROOT / "rooms.json"
COUNT_MODE = "--count" in sys.argv
REVIEW_MODE = "--review" in sys.argv

# 24시간 메시지 카운트 시 방 하나당 최대 조회 수 (과도한 요청 방지)
COUNT_LIMIT = 500

load_dotenv(ROOT / ".env")
API_ID = os.getenv("TELEGRAM_API_ID")
API_HASH = os.getenv("TELEGRAM_API_HASH")
SESSION = os.getenv("TELEGRAM_SESSION") or None

def require_credentials() -> None:
    """CLI 로 직접 실행할 때만 검사한다.

    **모듈 수준에서 sys.exit 하면 안 된다.** 설정 위저드가 이 파일을 import 해서
    merge_rooms·fetch_rooms 를 쓰는데, 팀원의 새 Codespace 에는 .env 가 없다 —
    그 값을 받아내는 것이 위저드가 하는 일이다. import 만으로 종료되면
    위저드가 아예 뜨지 않는다. (실측 2026-08-08, 연습 Codespace 에서 확인)
    """
    if not REVIEW_MODE and (not API_ID or not API_HASH):
        sys.exit("[중단] .env 에 TELEGRAM_API_ID / TELEGRAM_API_HASH 를 먼저 채우세요.")


def room_type(dialog) -> str | None:
    """요약 대상이 될 수 있는 방인지 판별. 1:1 대화는 제외."""
    if dialog.is_channel and not dialog.is_group:
        return "channel"  # 공지형 채널 (주식방 대부분)
    if dialog.is_group:
        return "group"    # 대화형 그룹
    return None           # 개인 채팅 / 봇 → 제외


def load_previous(path=None) -> dict:
    """이전에 내려둔 enabled 결정을 id 기준으로 불러온다.

    path 인자는 설정 위저드용이다. 위저드는 Codespace 저장소 루트의 rooms.json 을 읽는다.
    기본값은 그대로라 CLI 동작은 바뀌지 않는다.
    """
    path = path or ROOMS_FILE
    if not path.exists():
        return {}
    try:
        # utf-8-sig: 편집기가 붙인 BOM 이 있어도 없어도 읽힌다
        return {r["id"]: r for r in json.loads(path.read_text(encoding="utf-8-sig"))}
    except (json.JSONDecodeError, KeyError, TypeError):
        print(f"[경고] {path.name} 을 읽지 못했습니다. 전부 미결정으로 다시 만듭니다.")
        return {}


def save_rooms(rooms: list, path=None) -> None:
    path = path or ROOMS_FILE
    path.write_text(
        json.dumps(rooms, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def review_mode() -> None:
    """미결정(null) 방을 하나씩 보여주며 y/n 으로 결정. 텔레그램 연결 없이 동작한다."""
    if not ROOMS_FILE.exists():
        sys.exit(f"[중단] {ROOMS_FILE.name} 이 없습니다. 먼저 --count 없이 한 번 실행하세요.")

    rooms = json.loads(ROOMS_FILE.read_text(encoding="utf-8-sig"))
    undecided = [r for r in rooms if r["enabled"] is None]

    if not undecided:
        enabled = sum(1 for r in rooms if r["enabled"] is True)
        print(f"미결정 방이 없습니다. (수집 대상 {enabled}개)")
        return

    print(f"\n미결정 방 {len(undecided)}개를 검토합니다.")
    print("  y = 수집 대상    n = 제외    s = 나중에    q = 저장하고 종료\n")

    for i, r in enumerate(undecided, 1):
        kind_ko = "채널" if r["type"] == "channel" else "그룹"
        mem = f"{r['members']:,}명" if r["members"] else "인원 미상"
        cnt = f"최근 24h {r['msgs_24h']}건" if r["msgs_24h"] is not None else "활동량 미조회"
        print(f"[{i}/{len(undecided)}] {r['name']}")
        print(f"         {kind_ko} · {mem} · {cnt}")

        while True:
            try:
                # replace: 파이프/리다이렉트로 입력할 때 섞여 들어오는 BOM 제거
                answer = input("         > ").replace("﻿", "").strip().lower()
            except (EOFError, KeyboardInterrupt):
                # Ctrl+C 로 중단해도 여기까지의 결정은 저장한다
                print("\n중단됨 — 여기까지의 결정을 저장합니다.")
                answer = "q"
                break
            if answer in ("y", "n", "s", "q"):
                break
            if answer:  # 빈 입력(엔터만)은 조용히 다시 물어본다
                print("         y / n / s / q 중에서 입력하세요.")

        if answer == "q":
            break
        if answer == "y":
            r["enabled"] = True
        elif answer == "n":
            r["enabled"] = False
        print()

    save_rooms(rooms)
    enabled = sum(1 for r in rooms if r["enabled"] is True)
    left = sum(1 for r in rooms if r["enabled"] is None)
    print(f"→ {ROOMS_FILE.name} 저장 완료  (수집 {enabled}개 / 미결정 {left}개)")


async def count_recent(client, dialog, since) -> tuple[int, int]:
    """최근 24시간 메시지 (건수, 글자수). 최신순으로 읽다가 24h 이전이 나오면 중단.

    글자 수는 비용 추정용이다(WIZARD_SPEC 4절). 건수보다 훨씬 정확하다 —
    비용은 방 개수가 아니라 글의 양으로 정해지기 때문이다.
    collect.py 가 저장하는 것과 같은 msg.message 를 센다.
    """
    n = chars = 0
    async for msg in client.iter_messages(dialog.id, limit=COUNT_LIMIT):
        if msg.date < since:
            break
        n += 1
        chars += len(msg.message or "")
    return n, chars


def merge_rooms(prev: dict, fetched: list[dict]) -> tuple[list, list, list]:
    """새로 조회한 방 목록에 이전 enabled 결정을 얹는다. 순수 함수.

    새 방이 None(미결정)으로 들어오는 것이 핵심이다. 자동으로 켜면 새로 들어간
    가족방·회사방 내용이 본인이 인지하기 전에 LLM 으로 전송된다.
    편의보다 프라이버시를 택한다.

    돌려주는 것: (정렬된 전체 목록, 새로 발견된 방, 사라진 방)
    """
    rooms = [{**r, "enabled": prev.get(r["id"], {}).get("enabled")} for r in fetched]
    current_ids = {r["id"] for r in rooms}
    new_rooms = [r for r in rooms if r["id"] not in prev]
    gone_rooms = [r for r in prev.values() if r["id"] not in current_ids]

    # 미결정 → 수집 대상 → 제외 순으로, 그 안에서는 활동량 순으로 정렬
    order = {None: 0, True: 1, False: 2}
    rooms.sort(key=lambda r: (order.get(r["enabled"], 0),
                              -(r["msgs_24h"] or 0),
                              -(r["members"] or 0)))
    return rooms, new_rooms, gone_rooms


async def fetch_rooms(client, since, count_mode: bool) -> list[dict]:
    """참여 중인 채널·그룹을 dict 목록으로. 1:1 대화와 봇은 제외된다.

    enabled 는 여기서 정하지 않는다 — merge_rooms 가 이전 결정을 얹는다.
    """
    rooms = []
    async for dialog in client.iter_dialogs():
        kind = room_type(dialog)
        if kind is None:
            continue

        msgs = chars = None
        if count_mode:
            msgs, chars = await count_recent(client, dialog, since)
            await asyncio.sleep(0.3)   # 계정 제한 방지용 딜레이

        rooms.append({
            "id": dialog.id,
            "name": dialog.name,
            "type": kind,
            "members": getattr(dialog.entity, "participants_count", None),
            "msgs_24h": msgs,
            "chars_24h": chars,        # 비용 추정용
            "enabled": None,
        })
    return rooms


async def main():
    client = TelegramClient(StringSession(SESSION), int(API_ID), API_HASH)

    print("텔레그램 연결 중...")
    if not SESSION:
        print("\n최초 로그인입니다. 아래 순서로 진행됩니다:")
        print("  1) 전화번호 입력 (국가코드 포함, 예: +821012345678)")
        print("  2) 텔레그램 앱으로 오는 인증 코드 입력")
        print("  3) 2단계 인증을 켜두셨다면 비밀번호 입력\n")

    await client.start()

    if not SESSION:
        print("\n" + "=" * 70)
        print("로그인 성공. 아래 문자열을 .env 의 TELEGRAM_SESSION= 뒤에 붙여넣으세요.")
        print("(다음 실행부터 인증 코드 입력이 없어집니다)")
        print("=" * 70)
        print(client.session.save())
        print("=" * 70 + "\n")

    me = await client.get_me()
    print(f"로그인 계정: {me.first_name or ''} (@{me.username or '-'})\n")

    prev = load_previous()
    since = datetime.now(timezone.utc) - timedelta(days=1)

    print("방 목록 수집 중" + (" (최근 24h 메시지 카운트 포함 — 시간이 걸립니다)" if COUNT_MODE else ""))
    fetched = await fetch_rooms(client, since, COUNT_MODE)
    # true=수집 / false=제외 / null=미결정(신규). 기존 방의 결정은 id 기준으로 보존된다
    rooms, new_rooms, gone_rooms = merge_rooms(prev, fetched)

    # ── 출력 ────────────────────────────────────────────────────────────
    mark = {True: "[O]", False: "[X]", None: "[?]"}
    print(f"\n총 {len(rooms)}개 (개인 채팅 제외)\n")
    print(f"{'':>4} {'#':>3}  {'구분':<6} {'24h':>5}  {'인원':>8}  이름")
    print("-" * 72)
    for i, r in enumerate(rooms, 1):
        kind_ko = "채널" if r["type"] == "channel" else "그룹"
        cnt = str(r["msgs_24h"]) if r["msgs_24h"] is not None else "-"
        mem = f"{r['members']:,}" if r["members"] else "-"
        print(f"{mark[r['enabled']]:>4} {i:>3}  {kind_ko:<6} {cnt:>5}  {mem:>8}  {r['name']}")
    print("-" * 72)
    print("  [O] 수집 대상   [X] 제외   [?] 미결정 → 아직 수집되지 않음")

    save_rooms(rooms)

    # ── 변경 사항 알림 ──────────────────────────────────────────────────
    if new_rooms:
        print(f"\n>>> 새로 발견된 방 {len(new_rooms)}개 (미결정 상태 — 수집되지 않습니다)")
        for r in new_rooms:
            print(f"      - {r['name']}")
        print('    rooms.json 에서 "enabled" 를 true 또는 false 로 정해주세요.')
    if gone_rooms:
        print(f"\n>>> 목록에서 사라진 방 {len(gone_rooms)}개 (나갔거나 삭제됨)")
        for r in gone_rooms:
            print(f"      - {r['name']}")

    undecided = sum(1 for r in rooms if r["enabled"] is None)
    enabled = sum(1 for r in rooms if r["enabled"] is True)
    print(f"\n→ {ROOMS_FILE.name} 저장 완료  (수집 {enabled}개 / 미결정 {undecided}개)")
    if undecided:
        print("   list_rooms.py --review 로 하나씩 정할 수 있습니다.")

    await client.disconnect()


if __name__ == "__main__":
    require_credentials()
    if REVIEW_MODE:
        review_mode()
    else:
        asyncio.run(main())
