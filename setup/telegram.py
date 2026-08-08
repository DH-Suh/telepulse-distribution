"""텔레그램 로그인과 방 목록 조회.

로그인이 여러 HTTP 요청에 걸쳐 있다(전화번호 요청과 코드 입력이 별도 요청이다).
그래서 클라이언트를 모듈 변수로 붙잡고 있는다.
세션 문자열은 호출자에게 돌려주기만 하고 여기서 디스크에 쓰지 않는다 —
세션 문자열은 계정 전체 접근 권한이다.

오류는 전부 WizardError 로 감싼다. 원문 예외를 화면에 그대로 내보내지 않는다.
비개발자가 읽는 화면이고, '무엇이 잘못됐는지 + 다음에 뭘 하면 되는지'가 같이 있어야 한다.
원문은 raw 에 담아 '오류 복사' 버튼용으로만 내려보낸다.
"""

from datetime import datetime, timedelta, timezone

from telethon import TelegramClient, errors
from telethon.sessions import StringSession

# 파이프라인 원본의 fetch_rooms 를 그대로 재사용한다. 같은 기능을 두 벌 만들지 않는다.
# 원본 폴더의 깊이가 개발/배포에서 다르므로 pipeline_path 가 찾아준다.
import pipeline_path  # noqa: E402,F401
import list_rooms  # noqa: E402


class WizardError(Exception):
    def __init__(self, message: str, kind: str = "error", raw: str = ""):
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.raw = raw


_client: TelegramClient | None = None
_phone: str | None = None
_hash: str | None = None      # phone_code_hash


def _flood(e) -> "WizardError":
    minutes = e.seconds // 60 + 1
    return WizardError(
        f"텔레그램이 잠시 대기를 요청했습니다. {minutes}분 뒤에 다시 시도하세요.",
        kind="flood", raw=repr(e))


async def send_code(api_id: str, api_hash: str, phone: str) -> None:
    """인증코드를 보낸다. 이미 붙어 있던 연결이 있으면 정리하고 새로 시작한다."""
    global _client, _phone, _hash
    await close()
    _phone = phone
    _client = TelegramClient(StringSession(), int(api_id), api_hash)
    try:
        await _client.connect()
    except (OSError, TimeoutError) as e:
        # MTProto 는 TLS 가 아니라 회사망 프록시를 타지 못한다. 우회 방법은 안내하지 않는다.
        raise WizardError(
            "텔레그램에 연결하지 못했습니다. 회사 네트워크에서는 텔레그램이 차단됩니다. "
            "GitHub Codespaces 에서 열면 정상 동작합니다.",
            kind="blocked", raw=repr(e))
    try:
        _hash = (await _client.send_code_request(phone)).phone_code_hash
    except errors.PhoneNumberInvalidError as e:
        raise WizardError(
            "텔레그램에 가입되지 않은 번호입니다. 국가번호를 포함해 다시 확인해 주세요.",
            kind="bad_phone", raw=repr(e))
    except errors.FloodWaitError as e:
        raise _flood(e)
    except (errors.ApiIdInvalidError, errors.ApiIdPublishedFloodError) as e:
        raise WizardError(
            "API ID 와 API HASH 가 맞지 않습니다. 1단계로 돌아가 다시 확인해 주세요.",
            kind="bad_api", raw=repr(e))


async def sign_in(code: str, password: str | None) -> str:
    """성공하면 세션 문자열을 돌려준다. 2FA 가 필요하면 needs_2fa 로 알린다."""
    if _client is None:
        raise WizardError("로그인 절차가 끊겼습니다. 전화번호부터 다시 시작해 주세요.",
                          kind="restart")
    try:
        await _client.sign_in(phone=_phone, code=code, phone_code_hash=_hash)
    except errors.SessionPasswordNeededError:
        # 2단계 인증을 켜둔 계정. 비밀번호 칸은 이때 처음 화면에 나타난다.
        if not password:
            raise WizardError("2단계 인증 비밀번호가 필요합니다.", kind="needs_2fa")
        try:
            await _client.sign_in(password=password)
        except errors.PasswordHashInvalidError as e:
            raise WizardError("2단계 인증 비밀번호가 맞지 않습니다.",
                              kind="bad_password", raw=repr(e))
    except errors.PhoneCodeInvalidError as e:
        raise WizardError(
            "코드가 맞지 않습니다. 다시 확인해 주세요. (2번 더 틀리면 잠시 대기해야 합니다)",
            kind="bad_code", raw=repr(e))
    except errors.PhoneCodeExpiredError as e:
        raise WizardError("코드가 만료됐습니다. 다시 보내기를 눌러 새 코드를 받으세요.",
                          kind="expired", raw=repr(e))
    except errors.FloodWaitError as e:
        raise _flood(e)
    return _client.session.save()


async def fetch(session: str, api_id: str, api_hash: str) -> list[dict]:
    """참여 중인 방 목록 + 최근 24h 건수·글자 수. 원본 list_rooms 함수를 그대로 쓴다."""
    client = TelegramClient(StringSession(session), int(api_id), api_hash)
    await client.connect()
    try:
        since = datetime.now(timezone.utc) - timedelta(days=1)
        return await list_rooms.fetch_rooms(client, since, count_mode=True)
    finally:
        await client.disconnect()


async def close() -> None:
    """붙어 있던 연결을 끊는다. 실패해도 무시한다 — 정리 실패로 흐름을 막지 않는다."""
    global _client
    if _client is not None:
        try:
            await _client.disconnect()
        except Exception:
            pass
        _client = None
