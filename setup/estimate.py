"""비용 추정 — 순수 함수. 외부 호출도 파일 입출력도 없다.

근거는 SPEC 6절 / WIZARD_SPEC 4절.
2026-08-06 클라우드 3회차 실측: 13방 433건 → 하루 $0.556 → 월 24,185원.

핵심: 비용은 방 개수가 아니라 글자 수로 정해진다. 그리고 출력비는 방을 줄여도
거의 고정이다. 그래서 방별 금액에는 입력비만 배분하고, 고정비는 합계에만 더한다.
방에 고정비를 나눠 넣으면 "이 방을 빼면 이만큼 준다"가 거짓말이 된다.
"""

CHARS_PER_TOKEN = 1.6              # 한글 실측 — 약 1.6자 = 1토큰
INPUT_USD_PER_MTOK = 5.00          # gpt-5.6-sol 입력 단가
FIXED_OUTPUT_USD_PER_DAY = 0.17    # 실측 5,544 토큰 × $30/M. 방 수와 거의 무관
KRW_PER_USD = 1450
DAYS_PER_MONTH = 30

PRICING = {
    "chars_per_token": CHARS_PER_TOKEN,
    "input_usd_per_mtok": INPUT_USD_PER_MTOK,
    "fixed_output_usd_per_day": FIXED_OUTPUT_USD_PER_DAY,
    "krw_per_usd": KRW_PER_USD,
    "days_per_month": DAYS_PER_MONTH,
}


def input_krw_per_month(chars_per_day: float) -> float:
    """하루 수집 글자 수 → 월 입력비(원). 방별 금액은 이 함수로만 만든다."""
    tokens = chars_per_day / CHARS_PER_TOKEN
    usd_per_day = tokens / 1_000_000 * INPUT_USD_PER_MTOK
    return usd_per_day * KRW_PER_USD * DAYS_PER_MONTH


def fixed_krw_per_month() -> float:
    """방을 줄여도 줄지 않는 출력 고정비(원/월). 밴드 합계에만 더한다."""
    return FIXED_OUTPUT_USD_PER_DAY * KRW_PER_USD * DAYS_PER_MONTH


def monthly_krw(chars_per_day: float) -> float:
    """선택한 방 전체의 예상 월 비용(원)."""
    return input_krw_per_month(chars_per_day) + fixed_krw_per_month()
