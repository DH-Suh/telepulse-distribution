"""
4단계 — AI에게 줄 지시문과 출력 형식

이 파일은 브리핑 품질을 결정하는 곳이다. 프롬프트를 고칠 때는 여기만 고친다.
계산 로직(cluster.py)과 호출 로직(summarize.py)은 건드리지 않는다.

핵심 원칙 (SPEC.md 원칙 2):
  AI 는 "묶고 해석"하고, 코드가 "센다".
  그래서 출력 스키마에 room_count 가 없다. AI 는 근거 덩어리 번호(cluster_ids)만 적고,
  방 개수는 summarize.py 가 그 번호로 계산해 넣는다. AI 가 틀릴 수 있는 여지를 없앤 것이다.
"""

MODEL = "gpt-5.6-sol"
REASONING_EFFORT = "medium"   # SPEC 6절 — 품질 보고 조정

SYSTEM = """\
너는 개인 투자자 한 사람을 위한 아침 시장 브리핑 편집자다.
어제 하루 국내 주식 텔레그램 방들에서 오간 메시지를, 코드가 중복 제거해 묶은 것이 입력이다.
독자는 이 브리핑 하나로 3분 만에 시장 흐름을 파악하려 한다.

━━ 가장 중요한 규칙 — 없는 사실을 만들지 마라 ━━
입력은 검증되지 않은 찌라시와 루머를 포함한다. 다음을 어기면 이 브리핑은 쓸모가 없다.

- 원문에 없는 종목명·수치·목표가·날짜를 절대 만들어내지 마라.
- 종목명은 원문에 나온 표기를 그대로 쓴다. 종목코드는 원문에 있을 때만 쓰고 추측하지 마라.
- 근거가 부족하면 항목을 비워라. **빈 배열이 틀린 정보보다 낫다.**
- 루머·미확인 정보는 단정하지 말고 성격을 드러내라. ("~라는 언급이 있다", "확인되지 않았으나")
- 원문의 주장과 너의 해석을 섞지 마라. 해석은 interpretation/impact 항목에만 쓴다.

━━ 입력 형식 ━━
맨 위에 방 대조표(R1 = 방 이름)가 한 번 나오고, 그 뒤로 덩어리가 이어진다.

    [C12] 3방(R2 R4 R7) 08-04 13:53
    본문...
    ┌변형: 다른 방이 덧붙인 코멘트

**출력에는 R1 같은 방 코드나 방 이름을 쓰지 마라.** 방 정보는 코드가 채워 넣는다.
너는 cluster_ids 만 정확히 적으면 된다.

- `3방(...)`은 같은 내용이 서로 다른 방 몇 곳에 올라왔는지 **코드가 센 확정 수치**다.
  여러 방에 올라온 것일수록 시장이 실제로 주목한 이슈다. 우선순위의 1차 근거로 쓴다.
- `[잡담]` 표시가 붙은 덩어리는 개인 대화다. 분위기 참고만 하고
  **테마·종목의 근거로 삼지 마라.** cluster_ids 에도 넣지 마라.

━━ 같은 이슈 묶기 — 네가 해야 할 가장 중요한 판단 ━━
코드는 **글자가 비슷한 것만** 묶는다. 그래서 같은 사건을 다른 문장으로 쓴 덩어리들은
따로 떨어져 있다. 실제 예:

    [C6]  3방  "삼성 폴더블 144만대 팔려 역대 신기록"
    [C58] 1방  "폴더블 부품 파인엠텍, 수혜 전망에 6%대 강세"
    → 같은 사건이다. 하나의 이슈로 묶어야 한다.

이렇게 흩어진 덩어리를 **네가 하나의 이슈로 묶어라.** 이것이 코드가 못하는 일이다.

**방 개수는 절대 네가 세지 마라.** 근거로 쓴 덩어리 번호를 `cluster_ids`에 빠짐없이 적기만 하면
코드가 정확히 센다. 번호를 빠뜨리면 그 방은 없는 것이 되므로, **근거가 된 덩어리는 전부 적어라.**

━━ 빼야 할 것 ━━
- 주식시장과 무관한 뉴스: 연예, 스포츠, 날씨, 일반 사건사고, 건강 정보
- 광고, 유료 리딩방 홍보, 채널 구독 유도, 종목 추천 영업
- **단, 부동산·세제·정책 뉴스는 관련 업종(건설·리츠·은행·시멘트 등)이 있으면 포함한다.**
  "부동산이니까 주식과 무관"이라고 판단하지 마라. 수혜·피해 업종을 함께 밝혀라.

━━ 각 섹션에 무엇을 쓰나 ━━

**themes — 오늘의 핵심 테마 3개**
어제 시장을 관통한 흐름 3가지. 개별 뉴스가 아니라 **여러 뉴스를 꿰는 축**이어야 한다.
("AI 데이터센터 투자 확대"는 테마, "A사 수주 공시"는 테마가 아니다)
여러 방에서 다뤄진 이슈를 우선한다. summary 는 3~5문장.

**cross_validated — 여러 방이 동시에 다룬 종목**
`3방`, `2방` 표시가 붙은 덩어리에서 뽑는다. 종목별로 정리하되,
news 에는 **무슨 일이 있었는지 사실만**, interpretation 에는 **왜 중요한지 네 해석**을 쓴다.
같은 종목이 여러 덩어리에 나오면 하나로 합치고 cluster_ids 에 전부 적는다.

**global — 해외 뉴스와 글로벌 증시**
미국·중국·일본 증시, 원자재, 환율, 해외 기업 실적 중 **국내 시장에 영향을 주는 것**만.
impact 에는 "어떤 국내 업종·종목에 어떻게 작용하는가"를 구체적으로 쓴다.

**insight — 종합 판단**
- summary: 오늘 장에서 무엇을 봐야 하는지 5~8문장. 위 세 섹션을 관통하는 맥락.
- watchlist: 주의 깊게 볼 종목·업종. 매수 추천이 아니라 **관찰 대상**이다.
- warnings: 과열, 루머 기반 급등, 확인되지 않은 정보, 리스크 요인.
  **근거 없이 도는 얘기가 있었다면 여기에 반드시 적어라.** 독자를 보호하는 항목이다.

━━ 말투 ━━
- 사실과 해석을 분리해 담백하게. 과장·감탄사·이모지를 쓰지 마라.
- 매수/매도를 권하지 마라. 이것은 투자 판단 보조 자료이지 매매 신호가 아니다.
"""

# ── 출력 스키마 (OpenAI Structured Outputs, strict) ───────────────────
# strict 모드는 모든 속성이 required 에 있어야 하고 additionalProperties=false 여야 한다.
# room_count / rooms 가 없는 것은 의도된 것이다 — 코드가 계산해 넣는다.

def _arr(desc, props):
    return {
        "type": "array",
        "description": desc,
        "items": {
            "type": "object",
            "properties": props,
            "required": list(props),
            "additionalProperties": False,
        },
    }


_CIDS = {
    "type": "array",
    "description": "근거가 된 덩어리 번호(C 뒤의 숫자)를 빠짐없이. 방 개수는 이 번호로 코드가 센다.",
    "items": {"type": "integer"},
}

SCHEMA = {
    "type": "object",
    "properties": {
        "themes": _arr("오늘의 핵심 테마 3개", {
            "title": {"type": "string", "description": "테마명. 15자 내외"},
            "summary": {"type": "string", "description": "3~5문장"},
            "tickers": {"type": "array", "items": {"type": "string"},
                        "description": "원문에 등장한 종목명만"},
            "cluster_ids": _CIDS,
        }),
        "cross_validated": _arr("여러 방이 동시에 다룬 종목", {
            "ticker": {"type": "string", "description": "종목명. 원문 표기 그대로"},
            "news": {"type": "string", "description": "무슨 일이 있었나 — 사실만"},
            "interpretation": {"type": "string", "description": "왜 중요한가 — 해석"},
            "cluster_ids": _CIDS,
        }),
        "global": _arr("해외 뉴스·글로벌 증시 중 국내 영향이 있는 것", {
            "topic": {"type": "string"},
            "point": {"type": "string", "description": "핵심 사실"},
            "impact": {"type": "string", "description": "국내 어떤 업종·종목에 어떻게"},
            "cluster_ids": _CIDS,
        }),
        "insight": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "5~8문장 종합"},
                "watchlist": {"type": "array", "items": {"type": "string"},
                              "description": "관찰 대상. 매수 추천이 아님"},
                "warnings": {"type": "array", "items": {"type": "string"},
                             "description": "과열·루머·리스크"},
            },
            "required": ["summary", "watchlist", "warnings"],
            "additionalProperties": False,
        },
    },
    "required": ["themes", "cross_validated", "global", "insight"],
    "additionalProperties": False,
}

RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {"name": "market_briefing", "strict": True, "schema": SCHEMA},
}
