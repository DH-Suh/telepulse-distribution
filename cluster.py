"""
3단계 — 필터 + 중복 클러스터링 (LLM 호출 0회)

data/YYYY-MM-DD.jsonl 을 읽어서
  ① 내용 없는 메시지(스티커·짤방)를 버리고
  ② 잡담으로 보이는 메시지에 등급표시만 하고 (버리지 않는다)
  ③ 같은 뉴스를 한 덩어리로 묶고
  ④ "몇 개 방에서 나왔는가"를 코드로 센다
data/YYYY-MM-DD.clusters.json 을 만든다. 4단계(AI 요약)의 입력이 된다.

이 파일에는 판단이 없고 계산만 있다(SPEC.md 원칙 2).
"어떤 뉴스가 중요한가"는 4단계 LLM이, "몇 개 방에서 나왔는가"는 여기가 정한다.

사용법:
  python cluster.py              # data/ 의 가장 최근 파일
  python cluster.py 2026-08-04   # 날짜 지정
"""

import collections
import json
import re
import sys
from pathlib import Path

from rapidfuzz import fuzz

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
REPORTS_DIR = ROOT / "reports"

# ── 조정 가능한 기준값 ────────────────────────────────────────────────
# 잡담 판정: 이 길이 미만 + 링크·프리뷰·숫자 없음
CHAT_MAX_LEN = 60
# 유사도 판정 대상 최소 길이 — 짧은 글은 우연히 닮기 쉬워서 제외한다
FUZZY_MIN_LEN = 30
# 유사도 임계값(%) — 실측 결과 같은 기사는 95~100%, 다른 기사는 30% 아래로
# 뚜렷하게 갈렸다. 85는 그 사이의 넉넉한 안전선이다.
FUZZY_THRESHOLD = 85
# 완전일치 판정 대상 최소 길이
EXACT_MIN_LEN = 15
# 같은 링크가 이보다 많은 메시지에 등장하면 기사가 아니라 채널 서명(푸터)으로 본다.
# 실측 최대치는 5건(같은 종목 공시 묶음)이었으므로 10은 안전한 여유값이다.
BOILERPLATE_MIN = 10

# 추적용 꼬리표만 제거한다. 기사 식별자(rcpNo, code, idxno, newsid 등)는
# 물음표 뒤에 있으므로 절대 통째로 지우면 안 된다.
#   dart.fss.or.kr/dsaf001/main.do?rcpNo=... → 물음표 뒤를 지우면 공시 38건이 한 주소가 된다
TRACKING = re.compile(r"[?&](utm_[^&]*|si|fbclid|gclid|rc|ntype|sid|division)=[^&]*", re.I)
URL_RE = re.compile(r"https?://[^\s]+")
# 채널 초대·구독 링크. 내용과 무관한 서명이므로 중복 판정에 쓰지 않는다.
SELF_PROMO = re.compile(r"//t\.me/|//telegram\.me/", re.I)


# ══ 텍스트·링크 정규화 ═══════════════════════════════════════════════

def canon_url(u: str) -> str:
    """같은 기사를 같은 주소로 만든다. 단, 식별 정보는 손대지 않는다."""
    u = u.rstrip(".,)]>\"'…·").split("#")[0]
    u = TRACKING.sub("", u)
    return re.sub(r"[?&]+$", "", u).lower()


def normalize(text: str) -> str:
    """비교용 지문. 링크·공백·기호·이모지를 걷어내고 글자만 남긴다.
    '✅ 대미투자 조선…' 과 '대미투자 조선…' 이 같은 값이 되게 하는 것이 목적."""
    text = URL_RE.sub(" ", text)
    return re.sub(r"[^\w가-힣]+", "", text).lower()


def full_of(row: dict) -> str:
    """본문 + 링크 미리보기(제목·설명) 전부. **중복 판정용**.

    SPEC 파이프라인의 [4] 링크 병합이 여기서 끝난다 — HTTP 요청 0회.
    비교에는 정보를 최대한 넣는다. 두 방이 같은 기사를 올렸다면 각자 붙인 코멘트는
    달라도 미리보기 제목·설명은 똑같으므로, 그것이 가장 강한 일치 근거가 된다."""
    pv = row.get("preview") or {}
    parts = [row.get("text") or "", pv.get("title") or "", pv.get("description") or ""]
    return "\n".join(p for p in parts if p).strip()


def blob_of(row: dict) -> str:
    """AI에게 실제로 넘길 본문. **저장용**.

    많은 방이 기사 제목을 본문에 그대로 복사해 붙인다. 그대로 이어붙이면 같은
    문장이 두세 번 반복되어 4단계 입력이 부풀고 AI가 읽을 때도 방해가 된다.
    이미 본문에 들어있는 미리보기 조각은 넣지 않는다.

    판정용(full_of)과 저장용(blob_of)을 나눈 이유 — 목적이 다르다.
    비교는 정보가 많을수록 정확해지고, 저장은 적을수록 싸고 읽기 쉽다.
    하나로 합치면 둘 중 하나가 반드시 손해를 본다(실측: 합쳤을 때 3방 중복 1건 유실)."""
    text = row.get("text") or ""
    pv = row.get("preview") or {}
    parts, seen = [text], normalize(text)
    for extra in (pv.get("title"), pv.get("description")):
        piece = normalize(extra or "")
        if piece and piece not in seen:
            parts.append(extra)
            seen += piece
    return "\n".join(p for p in parts if p).strip()


def primary_url(row: dict) -> str | None:
    """이 메시지의 '대표 링크' 하나. 없으면 None.

    왜 하나만 쓰는가 — 증권사 리포트 모음글에는 링크가 10개씩 들어있다.
    링크를 전부 열쇠로 쓰면 그 글이 '환승역'이 되어, 서로 무관한 기사들을
    줄줄이 하나로 이어붙인다(실측: 무관한 메시지 257건이 한 덩어리가 됨).
    미리보기로 띄운 링크는 '이 메시지가 무엇에 관한 글인가'에 대한 텔레그램 자신의
    판단이므로 그것을 우선 쓰고, 없으면 본문에 링크가 정확히 하나일 때만 인정한다.
    """
    pv = row.get("preview") or {}
    if pv.get("url"):
        return canon_url(pv["url"])
    urls = list(dict.fromkeys(canon_url(u) for u in URL_RE.findall(row.get("text") or "")))
    return urls[0] if len(urls) == 1 else None


# ══ ② 등급 판정 ══════════════════════════════════════════════════════

def is_chat(row: dict) -> bool:
    """잡담으로 보이는가. '버릴까'가 아니라 '방 카운트에 넣을까'를 정하는 판정이다.

    길이만으로는 잡히지 않는다(1~9자 메시지는 하루 19건뿐). 실제 노이즈는
    '갤럭시 가주아?' 처럼 짧고 링크 없고 숫자 없는 대화체다.

    이 판정은 100% 정확하지 않다. 실측 126건 중 '금투세는 없었습니다' 처럼
    시장 정보인 것이 20% 가량 섞여 있었다. 그래서 삭제하지 않고 등급만 매긴다.
    틀려도 잃는 것이 표 한 장뿐이라면, 틀릴 수 있는 판정을 써도 안전하다.
    """
    text = row.get("text") or ""
    if not text or len(text) >= CHAT_MAX_LEN:
        return False
    if row.get("preview") or URL_RE.search(text):
        return False
    return not re.search(r"\d", text)  # 숫자가 있으면 시세·지표일 가능성이 높다


# ══ ③ 중복 묶기 ══════════════════════════════════════════════════════

class Groups:
    """Union-Find. 흩어진 메시지를 같은 덩어리로 합치는 자료구조."""

    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra

    def clusters(self) -> list[list[int]]:
        out = collections.defaultdict(list)
        for i in range(len(self.parent)):
            out[self.find(i)].append(i)
        return list(out.values())


def find_boilerplate_urls(items: list[dict]) -> set[str]:
    """기사가 아니라 채널 서명인 링크를 골라낸다.
    실측: t.me/rocket_news1 이 248개 메시지 전부에 붙어 있었다."""
    counts = collections.Counter(it["url"] for it in items if it["url"])
    return {u for u, c in counts.items()
            if c >= BOILERPLATE_MIN or SELF_PROMO.search(u)}


def group_by_key(groups: Groups, items: list[dict], key_fn) -> None:
    """같은 열쇠값을 가진 메시지들을 한 덩어리로 합친다."""
    buckets = collections.defaultdict(list)
    for i, it in enumerate(items):
        k = key_fn(it)
        if k:
            buckets[k].append(i)
    for members in buckets.values():
        for m in members[1:]:
            groups.union(members[0], m)


def group_by_similarity(groups: Groups, items: list[dict]) -> int:
    """근사중복 묶기. 오타·머릿기호·문장 재배열까지 흡수한다.

    '대표자 비교' 방식을 쓴다. 모든 쌍을 비교해 닮은 것끼리 계속 이어붙이면
    A~B, B~C 가 닮았다는 이유로 A~C 가 전혀 달라도 한 덩어리가 된다.
    키 순서로 줄을 세워 '옆사람과 1cm 차이면 같은 조'로 묶으면 결국 전교생이
    한 조가 되는 것과 같다. 그래서 각 덩어리의 대표자에게만 비교한다.
    """
    order = sorted(range(len(items)), key=lambda i: -len(items[i]["norm"]))
    leaders: list[int] = []
    merged = 0
    for i in order:
        if len(items[i]["norm"]) < FUZZY_MIN_LEN:
            continue
        for lead in leaders:
            if fuzz.token_sort_ratio(items[i]["norm"], items[lead]["norm"],
                                     score_cutoff=FUZZY_THRESHOLD) >= FUZZY_THRESHOLD:
                groups.union(lead, i)
                merged += 1
                break
        else:
            leaders.append(i)
    return merged


# ══ 파이프라인 ═══════════════════════════════════════════════════════

def build(rows: list[dict]) -> tuple[list[dict], dict]:
    """수집 원본 → 클러스터 목록 + 통계. summarize.py 가 이 함수를 쓴다."""

    # ① 내용 없는 메시지 제외 — 본문도 미리보기도 없으면 읽을 것이 없다
    items, empty = [], []
    for row in rows:
        full = full_of(row)
        if not full:
            empty.append(row)
            continue
        items.append({
            "row": row,
            "blob": blob_of(row),      # 저장·전달용 (군더더기 제거)
            "norm": normalize(full),   # 비교용 (정보 최대)
            "url": primary_url(row),
            "tier": "chat" if is_chat(row) else "signal",
        })

    # 채널 서명 링크는 열쇠에서 제외
    boiler = find_boilerplate_urls(items)
    for it in items:
        if it["url"] in boiler:
            it["url"] = None

    # ③ 3단 열쇠로 묶기 — 링크 → 완전일치 → 유사도
    groups = Groups(len(items))
    group_by_key(groups, items, lambda it: it["url"])
    group_by_key(groups, items,
                 lambda it: it["norm"] if len(it["norm"]) >= EXACT_MIN_LEN else None)
    fuzzy_merges = group_by_similarity(groups, items)

    # ④ 방 카운트 — signal 등급만 투표권이 있고, 한 방은 몇 번 말해도 1표
    clusters = []
    for members in groups.clusters():
        picked = [items[m] for m in members]
        signal = [it for it in picked if it["tier"] == "signal"]
        vote_rooms = sorted({it["row"]["room"] for it in signal})
        chat_rooms = sorted({it["row"]["room"] for it in picked
                             if it["tier"] == "chat"} - set(vote_rooms))

        # 대표 본문은 가장 긴 것 — 정보가 가장 많이 담긴 판본
        ranked = sorted(picked, key=lambda it: (it["tier"] == "signal", len(it["blob"])),
                        reverse=True)
        rep = ranked[0]
        variants = list(dict.fromkeys(it["blob"] for it in ranked[1:]
                                      if it["norm"] != rep["norm"]))

        clusters.append({
            "cluster_id": 0,  # 정렬 후 부여
            "room_count": len(vote_rooms),
            "rooms": vote_rooms,
            "chat_rooms": chat_rooms,
            "msg_count": len(picked),
            "tier": "signal" if signal else "chat",
            "date": min(it["row"]["date"] for it in picked),
            "representative": rep["blob"],
            "variants": variants,
            "urls": sorted({it["url"] for it in picked if it["url"]}),
            "forwarded": sorted({it["row"]["forward_from"] for it in picked
                                 if it["row"].get("forward_from")}),
            "has_image": any(it["row"].get("has_image") for it in picked),
        })

    # 여러 방에서 나온 것이 앞에 오도록 — 4단계 LLM이 먼저 읽는 순서가 된다
    clusters.sort(key=lambda c: (-c["room_count"], -c["msg_count"], c["date"]))
    for n, c in enumerate(clusters, 1):
        c["cluster_id"] = n

    stats = {
        "total": len(rows),
        "empty": len(empty),
        "kept": len(items),
        "chat": sum(1 for it in items if it["tier"] == "chat"),
        "signal": sum(1 for it in items if it["tier"] == "signal"),
        "clusters": len(clusters),
        "fuzzy_merges": fuzzy_merges,
        "boilerplate_urls": sorted(boiler),
        "room_dist": collections.Counter(c["room_count"] for c in clusters),
        "chat_rows": [it["row"] for it in items if it["tier"] == "chat"],
        "empty_rows": empty,
    }
    return clusters, stats


# ══ 실행 ═════════════════════════════════════════════════════════════

def pick_input() -> Path:
    if len(sys.argv) > 1:
        p = DATA_DIR / f"{sys.argv[1]}.jsonl"
        if not p.exists():
            sys.exit(f"[중단] {p.relative_to(ROOT)} 가 없습니다.")
        return p
    files = sorted(DATA_DIR.glob("*.jsonl"))
    if not files:
        sys.exit("[중단] data/ 에 수집 파일이 없습니다. collect.py 를 먼저 실행하세요.")
    return files[-1]


def write_dropped_report(path: Path, stats: dict) -> None:
    """걸러낸 것을 눈으로 확인할 수 있게 남긴다.
    보이지 않는 필터는 시간이 지나면 무엇을 버리고 있는지 아무도 모르게 된다."""
    lines = [
        "이 파일은 3단계에서 '방 카운트 제외' 또는 '버림' 처리된 메시지 목록입니다.",
        "필터가 잘못 걸러낸 것이 있는지 눈으로 확인하는 용도입니다.",
        "",
        f"── 잡담 등급 {len(stats['chat_rows'])}건 "
        "(버리지 않음 · AI에는 그대로 전달 · 방 카운트에서만 제외) " + "─" * 10,
        "",
    ]
    for row in stats["chat_rows"]:
        lines.append(f"[{row['room']}] {row['text']!r}")
    lines += ["", f"── 내용 없음 {len(stats['empty_rows'])}건 (본문·미리보기 모두 없음 · 버림) "
                  + "─" * 10, ""]
    counts = collections.Counter(r["room"] for r in stats["empty_rows"])
    for room, c in counts.most_common():
        lines.append(f"{c:>5}건  {room}")
    # 사람이 읽는 파일이므로 BOM 을 붙인다. 없으면 메모장·엑셀·PowerShell 이
    # 시스템 코드페이지로 읽어 한글이 깨진다(윈도우 기본 동작).
    # 반대로 clusters.json 은 코드가 읽으므로 BOM 없는 순수 UTF-8 을 유지한다.
    # 줄바꿈은 \n 으로 쓴다 — 윈도우에서는 파이썬이 알아서 \r\n 으로 바꿔준다.
    path.write_text("\n".join(lines), encoding="utf-8-sig")


def main() -> None:
    src = pick_input()
    rows = [json.loads(line) for line in src.open(encoding="utf-8") if line.strip()]
    clusters, st = build(rows)

    day = src.stem
    out = DATA_DIR / f"{day}.clusters.json"
    out.write_text(json.dumps(clusters, ensure_ascii=False, indent=1), encoding="utf-8")
    REPORTS_DIR.mkdir(exist_ok=True)
    dropped = REPORTS_DIR / f"{day}.dropped.txt"
    write_dropped_report(dropped, st)

    multi = [c for c in clusters if c["room_count"] >= 2]
    three = [c for c in clusters if c["room_count"] >= 3]

    print(f"입력: {src.relative_to(ROOT)}\n")
    print(f"원본 {st['total']:,}건")
    print(f"  ├ 내용 없음 제외      {st['empty']:>5,}건  (스티커·짤방)")
    print(f"  └ 남은 메시지         {st['kept']:>5,}건")
    print(f"      ├ 신호 등급       {st['signal']:>5,}건  (방 카운트 투표권 있음)")
    print(f"      └ 잡담 등급       {st['chat']:>5,}건  (AI엔 전달 · 투표권 없음)")
    print(f"\n중복 묶기 → 클러스터 {st['clusters']:,}개  (유사도로 병합 {st['fuzzy_merges']}건)")
    dist = ", ".join(f"{k}방 {v}개" if k else f"투표권없음(잡담만) {v}개"
                     for k, v in sorted(st["room_dist"].items()))
    print(f"  방 개수 분포: {dist}")
    print(f"  2개 방 이상 중복: {len(multi)}건")
    print(f"  3개 방 이상 중복: {len(three)}건   ← Cross-Validation 근거")

    if st["boilerplate_urls"]:
        print(f"\n중복 판정에서 제외한 채널 서명 링크 {len(st['boilerplate_urls'])}개:")
        for u in st["boilerplate_urls"]:
            print(f"  · {u[:88]}")

    biggest = max(c["msg_count"] for c in clusters) if clusters else 0
    print(f"\n가장 큰 클러스터 {biggest}건 "
          f"— 이 값이 10을 넘으면 무관한 글이 뭉친 것이니 확인이 필요합니다.")

    print(f"\n{'=' * 66}")
    print(f"3개 방 이상에서 중복 언급된 이슈 {len(three)}건")
    print(f"{'=' * 66}")
    for c in three:
        head = c["representative"].replace("\n", " ")[:78]
        print(f"  [{c['room_count']}방] {head}")
        print(f"         {' / '.join(c['rooms'])}")

    print(f"\n{'=' * 66}")
    print(f"2개 방 중복 이슈 {len(multi) - len(three)}건 (앞 15건만)")
    print(f"{'=' * 66}")
    for c in [c for c in multi if c["room_count"] == 2][:15]:
        print(f"  {c['representative'].replace(chr(10), ' ')[:78]}")

    # 4단계 비용 예측용 — AI에 실제로 넘길 본문 분량
    payload = sum(len(c["representative"]) + sum(len(v) for v in c["variants"])
                  for c in clusters)
    print(f"\n저장: {out.relative_to(ROOT)}  ← 4단계 입력")
    # 한글은 토크나이저에 따라 글자당 0.7~1.2 토큰이다. 범위로만 적는다.
    # 정확한 값은 4단계에서 API 응답의 usage 로 확인해야 한다.
    print(f"      본문 {payload:,}자 → 대략 {payload * 7 // 10 // 1000}k~"
          f"{payload * 12 // 10 // 1000}k 토큰 (Sol 입력비 "
          f"${payload * 0.7 / 1e6 * 5:.2f}~${payload * 1.2 / 1e6 * 5:.2f}/일)")
    print("      ※ SPEC 6절은 입력 40k 토큰을 가정했다. 실측은 그보다 크다 — 4단계에서 재확인 필요")
    print(f"검증: {dropped.relative_to(ROOT)}  ← 걸러낸 목록(눈으로 확인용)")


if __name__ == "__main__":
    main()
