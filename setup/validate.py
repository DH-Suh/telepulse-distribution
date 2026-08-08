"""입력 형식 검증 — 순수 함수.

돌려주는 값의 규칙: None 이면 통과, 문자열이면 그 문자열이 화면에 그대로 뜬다.
그래서 모든 문구는 비개발자가 읽고 다음에 뭘 할지 알 수 있는 한국어여야 한다.

형식만 본다. 실제 유효성은 ②의 텔레그램 로그인과 ④의 OpenAI 호출에서 판명된다.
여기서 과하게 막으면 정상 값을 가진 사람을 튕겨낸다.
"""

import re

_HEX32 = re.compile(r"^[0-9a-fA-F]{32}$")
_E164 = re.compile(r"^\+[1-9]\d{7,14}$")
_PHONE_NOISE = re.compile(r"[\s\-()]")


def api_id(v: str) -> str | None:
    v = v.strip()
    if not v:
        return "API ID 를 넣어주세요."
    if not v.isdigit():
        return "숫자만 넣어주세요. my.telegram.org 의 App api_id 값입니다."
    if not 5 <= len(v) <= 10:
        return "자릿수가 맞지 않습니다. 보통 7~8자리 숫자입니다."
    return None


def api_hash(v: str) -> str | None:
    v = v.strip()
    if not v:
        return "API HASH 를 넣어주세요."
    if not _HEX32.match(v):
        return "32자리 영문·숫자여야 합니다. App api_hash 값을 통째로 붙여넣으세요."
    return None


def normalize_phone(v: str) -> str:
    """공백·하이픈·괄호를 지워 +8210… 형태로 만든다. 붙여넣기 관용."""
    return _PHONE_NOISE.sub("", v.strip())


def phone(v: str) -> str | None:
    v = normalize_phone(v)
    if not v:
        return "전화번호를 넣어주세요."
    if not _E164.match(v):
        return "국가번호를 포함하세요 (예: +8210…). 텔레그램에 가입한 번호여야 합니다."
    return None


def openai_key(v: str) -> str | None:
    v = v.strip()
    if not v:
        return "OpenAI 키를 넣어주세요."
    if not v.startswith("sk-"):
        return "OpenAI 키는 sk- 로 시작합니다. 전체를 붙여넣었는지 확인하세요."
    if len(v) < 20:
        return "키가 너무 짧습니다. 잘리지 않고 전부 복사됐는지 확인하세요."
    return None
