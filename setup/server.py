"""설정 위저드 서버 — aiohttp.

왜 aiohttp 인가: Telethon 이 asyncio 기반이고, 로그인 흐름이 여러 HTTP 요청에 걸쳐
하나의 클라이언트를 유지해야 한다(전화번호 요청과 코드 입력이 별도 요청이다).
표준 라이브러리 http.server 는 동기라 별도 스레드에서 이벤트 루프를 돌리는
브리지 코드가 필요해진다. 의존성 하나가 그 브리지보다 단순하다.

STATE 는 프로세스 메모리에만 있다. 세션 문자열을 디스크에 쓰지 않기 위해서다.
사용자가 한 명이고 브라우저가 하나라 쿠키·세션 관리를 두지 않는다.

  python setup/server.py
"""

import asyncio
import os
import re
import subprocess
import sys
from pathlib import Path

from aiohttp import web

sys.path.insert(0, str(Path(__file__).parent))
import estimate  # noqa: E402
import github  # noqa: E402
import pipeline_path  # noqa: E402,F401  — list_rooms 를 import 하기 전에 경로를 잡는다
import telegram  # noqa: E402
import validate  # noqa: E402

import list_rooms  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).parent
STATIC = HERE / "static"

# 배포본(build/)에서는 setup/ 의 부모가 저장소 루트다.
# 개발 중(distribution/setup/)에는 distribution/ 을 가리키므로 상위의 진짜 rooms.json 을
# 건드리지 않는다 — 개발 실행이 내 개인 방 목록을 읽거나 덮어쓰지 않는다.
REPO_ROOT = HERE.parent
PORT = 8765

STATE: dict = {
    "api_id": None,
    "api_hash": None,
    "phone": None,
    "session": None,      # 텔레그램 세션 문자열 — 메모리에만 둔다
    "rooms": [],          # rooms.json 에 그대로 쓰인다. 화면 전용 필드를 섞지 않는다
    "new_ids": set(),     # '새 방' 배지용. 방 기록이 아니라 이번 조회의 부산물이다
    "openai_key": None,
    "gh_token": None,
    "saved": {},          # 저장 4단계의 진행 상태 (Task 11)
}


def detect_repo() -> str | None:
    """owner/repo 를 알아낸다. Codespaces 는 환경변수로 준다 — 물어볼 필요가 없다."""
    if repo := os.getenv("GITHUB_REPOSITORY"):
        return repo
    try:
        url = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except Exception:
        return None
    m = re.search(r"github\.com[:/]([^/]+/[^/.]+)", url)
    return m.group(1) if m else None


async def index(_req):
    return web.FileResponse(STATIC / "index.html")


async def get_state(_req):
    saved = list_rooms.load_previous(REPO_ROOT / "rooms.json")
    return web.json_response({
        "repo": detect_repo(),
        "pricing": estimate.PRICING,
        "has_rooms": bool(STATE["rooms"]),
        "saved": STATE["saved"],
        # rooms.json 이 이미 있으면 재방문이다 — 처음부터 걷게 하지 않는다
        "has_saved_rooms": bool(saved),
        "saved_room_count": sum(1 for r in saved.values() if r.get("enabled") is True),
    })


async def post_credentials(req):
    """① 텔레그램 API 값. 형식만 본다 — 진짜 유효한지는 ②의 로그인에서 판명된다.

    어느 칸이 틀렸는지 field 로 알려준다. 화면이 문구를 보고 짐작하지 않게 하기 위해서다
    (두 오류 문구에 모두 '숫자'가 들어 있어 문자열 비교로는 갈라지지 않는다).
    """
    body = await req.json()
    for field, check in (("api_id", validate.api_id), ("api_hash", validate.api_hash)):
        if msg := check(body.get(field, "")):
            return web.json_response({"error": msg, "field": field}, status=400)
    STATE["api_id"] = body["api_id"].strip()
    STATE["api_hash"] = body["api_hash"].strip()
    return web.json_response({"ok": True})


def err(e, status: int = 400):
    """WizardError → 화면이 읽을 JSON.

    kind 로 화면이 분기한다(문구가 아니라). raw 는 '오류 복사' 버튼용으로만 내려보낸다.
    """
    return web.json_response({"error": e.message, "kind": e.kind, "raw": e.raw}, status=status)


async def post_send_code(req):
    """② 전화번호 → 인증코드 발송."""
    body = await req.json()
    if msg := validate.phone(body.get("phone", "")):
        return web.json_response({"error": msg, "kind": "bad_phone"}, status=400)
    if not STATE["api_id"]:
        return web.json_response(
            {"error": "1단계의 API 값이 없습니다. 처음부터 다시 시작해 주세요.",
             "kind": "restart"}, status=400)
    STATE["phone"] = validate.normalize_phone(body["phone"])
    try:
        await telegram.send_code(STATE["api_id"], STATE["api_hash"], STATE["phone"])
    except telegram.WizardError as e:
        return err(e)
    return web.json_response({"ok": True})


async def post_sign_in(req):
    """② 인증코드(+2FA 비밀번호) → 세션 문자열. 세션은 메모리에만 둔다."""
    body = await req.json()
    try:
        STATE["session"] = await telegram.sign_in(
            body.get("code", "").strip(), body.get("password", "").strip() or None)
    except telegram.WizardError as e:
        return err(e)
    return web.json_response({"ok": True})


async def post_resend(_req):
    """코드 만료·미도착 시 재발송. 같은 번호로 다시 요청한다."""
    if not STATE["phone"]:
        return web.json_response(
            {"error": "전화번호부터 다시 입력해 주세요.", "kind": "restart"}, status=400)
    try:
        await telegram.send_code(STATE["api_id"], STATE["api_hash"], STATE["phone"])
    except telegram.WizardError as e:
        return err(e)
    return web.json_response({"ok": True})


# '대화 위주' 배지 기준. 판정이 아니라 참고 표시다 — 자동으로 켜거나 끄지 않는다.
CHATTY_MSGS = 100


async def get_rooms(_req):
    """③ 방 목록 + 방별 예상 입력비.

    한 번 불러오면 STATE 에 남긴다. 다시 조회하면 방마다 최대 500건을 훑느라 오래 걸린다.
    krw 는 **입력비 배분분만** 담는다. 출력 고정비는 fixed_krw 로 따로 내려보내
    밴드 합계에만 더한다 — 방에 나눠 넣으면 "이 방을 빼면 이만큼 준다"가 거짓이 된다.
    """
    if not STATE["rooms"]:
        if not STATE["session"]:
            return web.json_response(
                {"error": "텔레그램 로그인이 필요합니다. 이전 단계로 돌아가 주세요.",
                 "kind": "restart"}, status=400)
        try:
            fetched = await telegram.fetch(
                STATE["session"], STATE["api_id"], STATE["api_hash"])
        except telegram.WizardError as e:
            return err(e)
        prev = list_rooms.load_previous(REPO_ROOT / "rooms.json")
        rooms, new_rooms, _ = list_rooms.merge_rooms(prev, fetched)
        # 처음 설치라면 전부 새 방이다. 그때는 배지를 달지 않는다 — 의미가 없다.
        STATE["new_ids"] = {r["id"] for r in new_rooms} if prev else set()
        STATE["rooms"] = rooms

    if not STATE["rooms"]:
        return web.json_response(
            {"error": "요약할 채널·그룹이 없습니다. 텔레그램에서 주식 채널을 구독한 뒤 "
                      "이 화면을 다시 열어주세요.", "kind": "no_rooms"}, status=400)

    # 화면 전용 필드(krw·chatty·is_new)는 여기서만 붙인다. STATE["rooms"] 는
    # rooms.json 에 그대로 쓰이므로 파일에 들어가면 안 되는 것을 섞지 않는다 —
    # 동기화가 enabled 외의 필드를 새로 만들어 지운다(CLAUDE.md).
    out = [{**r,
            "krw": estimate.input_krw_per_month(r.get("chars_24h") or 0),
            "chatty": r.get("type") == "group" and (r.get("msgs_24h") or 0) >= CHATTY_MSGS,
            "is_new": r["id"] in STATE["new_ids"]}
           for r in STATE["rooms"]]
    return web.json_response({"rooms": out, "fixed_krw": estimate.fixed_krw_per_month()})


async def post_select(req):
    """고른 방을 STATE 에 반영한다. 저장은 아직 하지 않는다(Task 11)."""
    ids = set((await req.json()).get("ids", []))
    if not ids:
        return web.json_response(
            {"error": "최소 한 개는 골라야 합니다.", "kind": "empty"}, status=400)
    for r in STATE["rooms"]:
        r["enabled"] = r["id"] in ids
    return web.json_response({"ok": True})


async def post_verify_key(req):
    """④ OpenAI 키 확인. models.list() 는 무료다.

    실패 원인을 셋으로 갈라 안내가 달라지게 한다 — "키가 유효하지 않습니다" 하나로 뭉치면
    카드 미등록인 사람이 멀쩡한 키를 계속 다시 만든다.
    """
    key = (await req.json()).get("key", "").strip()
    if msg := validate.openai_key(key):
        return web.json_response({"error": msg, "kind": "format"}, status=400)

    # truststore 를 openai 임포트보다 먼저 — 회사망 SSL 검사 프록시 대응 (SPEC 14절)
    import truststore
    truststore.inject_into_ssl()
    import openai

    try:
        await asyncio.to_thread(lambda: openai.OpenAI(api_key=key).models.list())
    except openai.AuthenticationError as e:
        return web.json_response(
            {"error": "키가 유효하지 않습니다. 전체가 복사됐는지, 삭제된 키가 아닌지 확인해 주세요.",
             "kind": "auth", "raw": repr(e)}, status=400)
    except (openai.PermissionDeniedError, openai.RateLimitError) as e:
        return web.json_response(
            {"error": "카드를 먼저 등록해야 키가 작동합니다. platform.openai.com 의 Billing 에서 "
                      "결제 수단을 등록한 뒤 다시 시도해 주세요.",
             "kind": "billing", "raw": repr(e)}, status=400)
    except Exception as e:
        return web.json_response(
            {"error": "OpenAI 에 연결하지 못했습니다. 잠시 뒤 다시 시도해 주세요.",
             "kind": "network", "raw": repr(e)}, status=400)

    STATE["openai_key"] = key
    return web.json_response({"ok": True})


async def post_dispatch(_req):
    """완료 화면의 '지금 한 번 만들어보기'. 실제로 과금되는 실행을 시작한다."""
    try:
        await github.dispatch_workflow(detect_repo(), STATE["gh_token"])
    except github.WizardError as e:
        return err(e)
    return web.json_response({"ok": True})


async def get_saved_rooms(_req):
    """재방문 — 저장된 방 목록만 읽는다. 텔레그램 로그인이 필요 없는 경로다.

    메모리에 있으면 그것을 먼저 쓴다. 방금 저장을 마친 같은 세션에서는 **로컬
    rooms.json 이 아직 옛것**이다 — 저장은 GitHub API 로 원격에만 했고 작업 폴더에는
    쓰지 않기 때문이다. 파일만 읽으면 '방 목록 수정하기'가 방금 고른 것을 잃는다.
    """
    rooms = STATE["rooms"] or list(
        list_rooms.load_previous(REPO_ROOT / "rooms.json").values())
    if not rooms:
        return web.json_response({"error": "저장된 방 목록이 없습니다.", "kind": "no_saved"},
                                 status=400)
    STATE["rooms"] = rooms
    out = [{**r, "is_new": False,
            "krw": estimate.input_krw_per_month(r.get("chars_24h") or 0),
            "chatty": (r.get("type") == "group" and (r.get("msgs_24h") or 0) >= CHATTY_MSGS)}
           for r in rooms]
    return web.json_response({"rooms": out, "fixed_krw": estimate.fixed_krw_per_month()})


# ── 저장 진행 (Task 11) ──────────────────────────────────────────────
# 줄 이름은 '무엇을 하는지'가 보여야 한다. '비밀값 4개 등록'은 개발자 말이라
# 보는 사람이 뭘 넣는다는 건지 알 수 없었다(실사용 지적, 2026-08-08).
SAVE_STEPS = [
    ("github",   "GitHub 연결"),
    ("secrets",  "텔레그램·OpenAI 연결 정보 저장"),
    ("rooms",    "방 목록 저장"),
    ("workflow", "매일 아침 자동 실행 켜기"),
]

SAVE: dict = {"running": False, "state": {}, "sub": {}, "device": None, "error": None}


def _mark(key, state, sub=""):
    SAVE["state"][key] = state
    if sub:
        SAVE["sub"][key] = sub


async def _run_save():
    """4단계를 순서대로. 이미 done 인 항목은 건너뛴다(멱등).

    멱등이 전부다 — 저장이 중간에 깨졌을 때 비개발자가 스스로 복구할 수 있는
    유일한 방법이 '다시 시도'이기 때문이다.
    """
    repo = detect_repo()
    try:
        if not repo:
            # 여기서 막지 않으면 repo=None 인 채로 API 를 불러 404 가 나고,
            # 화면에는 '쓰기 권한을 확인하세요'라는 엉뚱한 안내가 뜬다.
            raise github.WizardError(
                "저장할 GitHub 저장소를 찾지 못했습니다. 이 폴더가 GitHub 저장소에서 "
                "받아온 것이 맞는지 확인해 주세요.", kind="no_repo")

        if SAVE["state"].get("github") != "done":
            _mark("github", "run")
            d = await github.device_start()
            SAVE["device"] = {"code": d["user_code"], "url": d["verification_uri"]}
            STATE["gh_token"] = await github.device_poll(d["device_code"], d.get("interval", 5))
            SAVE["device"] = None
            _mark("github", "done", f"{repo} 에 연결됨")

        token = STATE["gh_token"]

        if SAVE["state"].get("secrets") != "done":
            _mark("secrets", "run")
            # **이번에 새로 받은 값만 쓴다.** 재방문(방 체크만 고치기)에서는 이 값들이
            # 비어 있다 — Secrets 는 쓰기 전용이라 되읽을 수 없기 때문이다.
            # 빈 값을 그대로 넘기면 이미 저장된 Secrets 를 지워버리고,
            # 다음 날 아침 브리핑이 조용히 죽는다.
            pairs = [(n, v) for n, v in (("TELEGRAM_API_ID", STATE["api_id"]),
                                         ("TELEGRAM_API_HASH", STATE["api_hash"]),
                                         ("TELEGRAM_SESSION", STATE["session"]),
                                         ("OPENAI_API_KEY", STATE["openai_key"]))
                     if v]
            for name, value in pairs:
                await asyncio.to_thread(github.set_secret, repo, name, value, token)
            _mark("secrets", "done",
                  f"GitHub 금고(Secrets)에 암호화되어 저장 · {len(pairs)}개" if pairs
                  else "이미 저장돼 있어 건너뜀")

        if SAVE["state"].get("rooms") != "done":
            _mark("rooms", "run")
            picked = sum(1 for r in STATE["rooms"] if r["enabled"] is True)
            await github.put_rooms_json(repo, STATE["rooms"], token)
            _mark("rooms", "done", f"rooms.json · {picked}개 방")

        if SAVE["state"].get("workflow") != "done":
            _mark("workflow", "run")
            await github.enable_workflow(repo, token)
            _mark("workflow", "done")

        SAVE["error"] = None
    except github.WizardError as e:
        _fail_running(e.message, e.kind, e.raw)
    except Exception as e:
        # 예상 못 한 예외까지 반드시 잡는다. 안 잡으면 진행 중이던 줄이 'run' 인 채로
        # 남아 화면이 영원히 도는 스피너가 된다 — 사용자가 할 수 있는 게 없어진다.
        _fail_running("예상하지 못한 오류가 발생했습니다. 다시 시도해 주세요.",
                      "unexpected", repr(e))
    finally:
        SAVE["running"] = False
        SAVE["device"] = None


def _fail_running(message: str, kind: str, raw: str) -> None:
    """진행 중이던 줄을 실패로 바꾼다.

    어느 줄도 시작하기 전에 터졌다면(저장소를 못 찾은 경우 등) 첫 미완료 줄에 붙인다.
    아무 데도 안 붙이면 failed 도 done 도 아닌 상태가 되어 화면이 영원히 돈다.
    """
    marked = False
    for key, _ in SAVE_STEPS:
        if SAVE["state"].get(key) == "run":
            _mark(key, "fail", message)
            marked = True
    if not marked:
        for key, _ in SAVE_STEPS:
            if SAVE["state"].get(key) != "done":
                _mark(key, "fail", message)
                break
    SAVE["error"] = {"message": message, "kind": kind, "raw": raw}


async def post_save_start(_req):
    if not SAVE["running"]:
        SAVE["running"] = True
        asyncio.create_task(_run_save())
    return web.json_response({"ok": True})


async def get_save_status(_req):
    steps = [{"key": k, "label": label,
              "state": SAVE["state"].get(k, "wait"),
              "sub": SAVE["sub"].get(k, "")} for k, label in SAVE_STEPS]
    return web.json_response({
        "steps": steps,
        "device": SAVE["device"],
        "error": SAVE["error"],
        "done": all(s["state"] == "done" for s in steps),
        "failed": any(s["state"] == "fail" for s in steps),
    })


@web.middleware
async def no_cache(req, handler):
    """화면 파일을 캐시하지 않게 한다.

    이유 둘.
    1) 이 위저드는 한 번 쓰고 닫는다. 캐시로 아낄 것이 없다.
    2) 캐시하면 **HTML 은 새것, CSS 는 옛것**이 섞인다. 새로 추가한 열에 규칙이 없어
       본문 기본값(16px·검정)으로 그려지는 식이다. 실제로 개발 중에 겪었고,
       팀원이 코드를 갱신한 뒤에도 똑같이 겪는다.

    no-cache 는 '쓰지 마라'가 아니라 '쓰기 전에 반드시 물어봐라'다.
    바뀐 게 없으면 서버가 304 로 답하므로 비용은 거의 없다.
    """
    res = await handler(req)
    if req.path == "/" or req.path.startswith("/static/"):
        res.headers["Cache-Control"] = "no-cache"
    return res


def build_app() -> web.Application:
    app = web.Application(middlewares=[no_cache])
    app.router.add_get("/", index)
    app.router.add_get("/api/state", get_state)
    app.router.add_post("/api/telegram/credentials", post_credentials)
    app.router.add_post("/api/telegram/send-code", post_send_code)
    app.router.add_post("/api/telegram/sign-in", post_sign_in)
    app.router.add_post("/api/telegram/resend", post_resend)
    app.router.add_get("/api/rooms", get_rooms)
    app.router.add_post("/api/rooms/select", post_select)
    app.router.add_post("/api/openai/verify", post_verify_key)
    app.router.add_post("/api/save/start", post_save_start)
    app.router.add_get("/api/save/status", get_save_status)
    app.router.add_post("/api/dispatch", post_dispatch)
    app.router.add_get("/api/rooms/saved", get_saved_rooms)
    app.router.add_static("/static/", STATIC)
    return app


if __name__ == "__main__":
    print(f"\n  Telepulse 설정 화면 → http://127.0.0.1:{PORT}\n")
    try:
        web.run_app(build_app(), host="127.0.0.1", port=PORT, print=None)
    except OSError as e:
        # Codespace 는 켜질 때 이 서버를 이미 띄운다(devcontainer 의 postAttachCommand).
        # 화면이 자동으로 안 열려서 손으로 한 번 더 실행하면 여기로 온다.
        # 파이썬 오류 원문을 그대로 보여주면 비개발자는 자기가 뭘 망가뜨린 줄 안다.
        # 실측 2026-08-08, 연습 Codespace. errno 는 리눅스 98 / 윈도우 10048.
        if e.errno in (48, 98, 10048):
            print("  설정 화면은 이미 실행 중입니다. 새로 켤 필요가 없습니다.\n"
                  "  아래 [포트] 탭에서 8765 줄의 지구본 아이콘을 누르면 화면이 열립니다.")
            sys.exit(0)
        raise
