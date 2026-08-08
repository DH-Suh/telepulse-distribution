"""GitHub 반영 — device flow 인증 · Secrets · rooms.json · 워크플로.

인증에 device flow 를 쓰는 이유: 의존성이 없고(HTTP POST 두 번), 비개발자에게도
"8자 코드를 브라우저에 붙여넣기" 수준이다. Codespaces 기본 토큰은 Secrets 쓰기 권한이
없을 수 있어 이 경로가 안전하고, 같은 코드가 로컬에서도 동작한다.
client_id 는 프로그램에 내장한다 — device flow 는 client_secret 이 불필요하므로 안전하다.

Secrets 등록에 gh CLI 를 쓰는 이유: API 직접 호출은 값을 libsodium sealed box 로
암호화해야 해서 PyNaCl 의존성이 붙는다. gh 는 Codespaces 에 이미 있고 암호화를 대신해준다.
값은 명령행 인자가 아니라 표준입력으로 넘긴다 — 셸 기록에 남지 않는다.

네 동작 모두 멱등이다. 실패 후 다시 눌러도 안전해야 한다 —
비개발자가 스스로 복구할 수 있는 유일한 방법이기 때문이다.

오류는 telegram.py 와 같은 모양의 WizardError 로 감싼다(message/kind/raw).
서로 import 하지 않으려고 따로 정의한다 — 두 모듈은 각자 외부 서비스 하나만 안다.

**scope 를 repo 에서 넓히지 말 것.** OAuth App 은 `workflow` 권한 없이는
`.github/workflows/` 아래 파일을 API 로 만들거나 고칠 수 없다. 위저드는 워크플로를
**켜기만 하고 쓰지 않으므로** repo 로 충분하다. 워크플로 파일을 손대게 만들면
팀원에게 요구하는 권한이 넓어진다 — 승인 화면에 더 무서운 문구가 뜬다.
"""

import asyncio
import base64
import json
import os
import subprocess
import time

import aiohttp

# OAuth App 'Telepulse 설정' (배포자 계정에 등록 · Device Flow 활성). 2026-08-08 등록.
# 계정 이름을 여기 적지 않는다 — 이 파일은 공개 저장소로 그대로 나간다(release.py 화이트리스트).
# 공개해도 안전하다 — device flow 는 client_secret 을 쓰지 않는다.
CLIENT_ID = "Ov23lizoZb6bP6UVSsxs"
SCOPE = "repo"                            # Secrets · Contents · Actions 에 필요
API = "https://api.github.com"
DEVICE_TIMEOUT_S = 900                    # 15분


class WizardError(Exception):
    def __init__(self, message: str, kind: str = "error", raw: str = ""):
        super().__init__(message)
        self.message, self.kind, self.raw = message, kind, raw


async def _json(session, method, url, **kw):
    async with session.request(method, url, **kw) as res:
        text = await res.text()
        try:
            return res.status, json.loads(text) if text else {}
        except json.JSONDecodeError:
            return res.status, {"raw": text}


async def device_start() -> dict:
    async with aiohttp.ClientSession() as s:
        status, data = await _json(
            s, "POST", "https://github.com/login/device/code",
            data={"client_id": CLIENT_ID, "scope": SCOPE},
            headers={"Accept": "application/json"})
    if status != 200 or "device_code" not in data:
        raise WizardError("GitHub 인증을 시작하지 못했습니다. 잠시 뒤 다시 시도해 주세요.",
                          kind="device_start", raw=str(data))
    return data


async def device_poll(device_code: str, interval: int = 5) -> str:
    """사용자가 브라우저에서 코드를 넣을 때까지 기다린다. 최대 15분."""
    deadline = time.monotonic() + DEVICE_TIMEOUT_S
    async with aiohttp.ClientSession() as s:
        while time.monotonic() < deadline:
            await asyncio.sleep(interval)
            _, data = await _json(
                s, "POST", "https://github.com/login/oauth/access_token",
                data={"client_id": CLIENT_ID, "device_code": device_code,
                      "grant_type": "urn:ietf:params:oauth:grant-type:device_code"},
                headers={"Accept": "application/json"})
            if token := data.get("access_token"):
                return token
            e = data.get("error")
            if e == "authorization_pending":
                continue
            if e == "slow_down":
                interval += 5
                continue
            if e == "expired_token":
                raise WizardError("인증 시간이 지났습니다. 다시 인증을 눌러 주세요.", kind="expired")
            if e == "access_denied":
                raise WizardError("GitHub 인증이 거부됐습니다. 다시 시도해 주세요.", kind="denied")
            raise WizardError("GitHub 인증에 실패했습니다.", kind="device_poll", raw=str(data))
    raise WizardError("인증 시간이 지났습니다. 다시 인증을 눌러 주세요.", kind="expired")


def set_secret(repo: str, name: str, value: str, token: str) -> None:
    """gh secret set. 값은 표준입력으로 넘긴다 — 셸 기록에 남지 않는다. 덮어쓰기라 멱등.

    --body 를 주지 않으면 gh 가 표준입력에서 읽는다. `--body-file -` 같은 플래그는 없다
    (실측: `unknown flag: --body-file`). 원본의 setup_secrets.py:50 과 같은 형태다.
    """
    try:
        p = subprocess.run(
            ["gh", "secret", "set", name, "--repo", repo],
            input=value, text=True, capture_output=True, timeout=60,
            # 환경을 통째로 물려준다. PATH 만 넘기면 윈도우에서 gh 가 뜨지 않는다.
            env={**os.environ, "GH_TOKEN": token})
    except FileNotFoundError as e:
        raise WizardError("gh 명령을 찾지 못했습니다. Codespaces 에서 열면 이미 설치돼 있습니다.",
                          kind="no_gh", raw=repr(e))
    except subprocess.TimeoutExpired as e:
        raise WizardError("GitHub 응답이 없습니다. 잠시 뒤 다시 시도해 주세요.",
                          kind="timeout", raw=repr(e))
    if p.returncode != 0:
        raise WizardError(
            f"비밀값 {name} 을 저장하지 못했습니다. 저장소에 쓰기 권한이 있는지 확인해 주세요.",
            kind="secret", raw=(p.stderr or "").strip())


async def put_rooms_json(repo: str, rooms: list, token: str) -> None:
    """Contents API 로 갱신한다. 로컬 git 상태에 의존하지 않아 재실행에 안전하다."""
    url = f"{API}/repos/{repo}/contents/rooms.json"
    head = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    payload = json.dumps(rooms, ensure_ascii=False, indent=2).encode("utf-8")
    async with aiohttp.ClientSession(headers=head) as s:
        _, cur = await _json(s, "GET", url)
        body = {"message": "설정 위저드 — 방 목록 갱신",
                "content": base64.b64encode(payload).decode()}
        if sha := cur.get("sha"):
            body["sha"] = sha            # 기존 파일 갱신 (sha 기반이라 멱등)
        status, data = await _json(s, "PUT", url, json=body)
    if status not in (200, 201):
        raise WizardError("방 목록을 저장하지 못했습니다. 저장소 쓰기 권한을 확인해 주세요.",
                          kind="rooms", raw=str(data))


async def enable_workflow(repo: str, token: str, wf: str = "daily.yml") -> None:
    """템플릿에서 만든 저장소는 워크플로가 꺼진 채 시작한다. 이미 켜져 있으면 성공 처리."""
    head = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    async with aiohttp.ClientSession(headers=head) as s:
        status, data = await _json(s, "PUT", f"{API}/repos/{repo}/actions/workflows/{wf}/enable")
    if status not in (204, 200, 409):    # 409 = 이미 활성
        raise WizardError(
            "자동 실행을 켜지 못했습니다. 손으로 켤 수 있습니다 — 저장소 페이지의 Actions 탭에서 "
            "'I understand my workflows, go ahead and enable them' 버튼을 한 번 누르면 됩니다.",
            kind="workflow", raw=str(data))


async def dispatch_workflow(repo: str, token: str, wf: str = "daily.yml", ref: str = "main") -> None:
    head = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    async with aiohttp.ClientSession(headers=head) as s:
        status, data = await _json(
            s, "POST", f"{API}/repos/{repo}/actions/workflows/{wf}/dispatches", json={"ref": ref})
    if status != 204:
        raise WizardError("지금 실행을 시작하지 못했습니다. 저장소 Actions 탭에서 직접 실행할 수 있습니다.",
                          kind="dispatch", raw=str(data))
