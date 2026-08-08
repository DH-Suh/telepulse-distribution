"""
4단계 — AI 요약 (Sol 1회 호출)

data/YYYY-MM-DD.clusters.json 을 읽어 브리핑을 만든다.
  reports/YYYY-MM-DD.report.json  구조화 결과 (5단계 전송이 읽음)
  reports/YYYY-MM-DD.md           사람이 읽는 형태

역할 분담 (SPEC.md 원칙 2)
  AI  — 흩어진 덩어리를 같은 이슈로 묶고, 무슨 의미인지 해석한다
  코드 — 그 묶음에 방이 몇 개 들어있는지 센다
AI 는 방 개수를 출력하지 않는다. 근거 덩어리 번호만 적고, 여기서 계산해 넣는다.

사용법:
  python summarize.py                # data/ 의 가장 최근 클러스터 파일
  python summarize.py 2026-08-04     # 날짜 지정
  python summarize.py --dry-run      # 호출하지 않고 분량·비용만 확인
  python summarize.py --force        # 이미 만든 브리핑을 무시하고 다시 생성 (재과금 주의)

같은 날 브리핑이 이미 있으면 API를 부르지 않는다. 한 번에 1,600원이라
파이프라인을 다시 돌릴 때 조용히 두 번 청구되는 것을 막는다.
"""

# 회사 네트워크의 SSL 검사 프록시 대응 — openai 임포트보다 반드시 먼저 (SPEC 14절)
import truststore
truststore.inject_into_ssl()

import collections
import json
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

import prompts

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
REPORTS_DIR = ROOT / "reports"

# Sol 단가 (SPEC 6절) — 실제 청구액 계산용
PRICE_IN, PRICE_OUT = 5.00, 30.00      # $ / 1M 토큰
USD_KRW = 1450

# 안전장치: 입력이 이보다 크면 중단한다. 사고로 큰 청구가 나가는 것을 막는다.
MAX_PAYLOAD_CHARS = 800_000

URL_RE = re.compile(r"https?://[^\s]+")


# ══ 입력 조립 ════════════════════════════════════════════════════════

def shorten_urls(text: str) -> str:
    """링크를 도메인만 남긴다.

    AI 는 링크를 열 수 없으므로 긴 주소를 읽을 이유가 없다. 다만 도메인은
    출처 신뢰도 신호(네이버뉴스인가 개인블로그인가)라서 남긴다.
    원본 주소는 clusters.json 의 urls 필드에 그대로 있으므로 잃는 것이 없다."""
    def repl(m):
        host = re.match(r"https?://([^/]+)", m.group()).group(1)
        return f"({host.removeprefix('www.')})"
    return URL_RE.sub(repl, text)


def trim_variant(variant: str, rep_norm: str) -> str:
    """대표 본문에 이미 있는 문장은 빼고, 그 방이 덧붙인 말만 남긴다."""
    keep = []
    for part in re.split(r"(?<=[.!?\n])", variant):
        core = re.sub(r"[^\w가-힣]+", "", part).lower()
        if len(core) >= 8 and core in rep_norm:
            continue
        keep.append(part)
    return "".join(keep).strip()


def build_payload(clusters: list[dict]) -> tuple[str, dict]:
    """클러스터 목록 → AI 에게 보낼 한 덩어리 텍스트.

    방 이름은 R1, R2… 코드로 줄이고 맨 위에 대조표를 한 번만 넣는다.
    방 이름이 최대 36자라 932번 반복하면 그것만으로 1.7만 자다.
    최종 리포트의 방 이름은 코드가 cluster_ids 로 다시 채우므로 잃는 것이 없다."""
    codes = {r: f"R{i}" for i, r in
             enumerate(sorted({r for c in clusters for r in c["rooms"]}), 1)}
    legend = ["[방 대조표]"] + [f"  {v} = {k}" for k, v in codes.items()] + [""]

    lines, saved = [], 0
    for c in clusters:
        raw = len(c["representative"]) + sum(len(v) for v in c["variants"])

        if c["tier"] == "chat":
            head = f"[C{c['cluster_id']}] [잡담]"
        else:
            rooms = " ".join(codes[r] for r in c["rooms"]) if c["rooms"] else "출처불명"
            head = f"[C{c['cluster_id']}] {c['room_count']}방({rooms}) {c['date'][5:16]}"

        body = shorten_urls(c["representative"]).strip()
        block = [head, body]

        rep_norm = re.sub(r"[^\w가-힣]+", "", c["representative"]).lower()
        for v in c["variants"]:
            extra = trim_variant(shorten_urls(v), rep_norm)
            if len(re.sub(r"[^\w가-힣]+", "", extra)) >= 10:
                block.append(f"┌변형: {extra}")

        text = "\n".join(block)
        saved += raw - len(text)
        lines.append(text)

    payload = "\n".join(legend) + "\n" + "\n\n".join(lines)
    return payload, {"clusters": len(clusters), "chars": len(payload), "saved": saved}


# ══ 결과 검증 및 방 개수 계산 ════════════════════════════════════════

def attach_counts(report: dict, clusters: list[dict]) -> dict:
    """AI 가 적은 cluster_ids 로 방 개수를 코드가 계산해 넣는다.

    이것이 Cross-Validation 을 지탱하는 장치다. AI 는 묶기만 하고 세지 않는다.
    잡담 등급 덩어리는 투표권이 없으므로 방 카운트에서 빠진다(3단계와 동일 규칙)."""
    by_id = {c["cluster_id"]: c for c in clusters}
    stats = {"cited": set(), "bad_ids": [], "items": 0}

    for section in ("themes", "cross_validated", "global"):
        for item in report.get(section, []):
            stats["items"] += 1
            rooms, valid = set(), []
            for cid in item.get("cluster_ids", []):
                c = by_id.get(cid)
                if c is None:
                    stats["bad_ids"].append(cid)      # AI 가 없는 번호를 지어낸 경우
                    continue
                valid.append(cid)
                stats["cited"].add(cid)
                rooms.update(c["rooms"])              # rooms 는 signal 등급만 들어있다
            item["cluster_ids"] = valid
            item["rooms"] = sorted(rooms)
            item["room_count"] = len(rooms)           # ← 코드가 센 확정 수치

    # 중복 언급 종목은 방 개수 순으로 재정렬 — 브리핑의 핵심 순서
    report["cross_validated"].sort(key=lambda x: -x["room_count"])
    return stats


def contribution(report: dict, clusters: list[dict]) -> list[tuple]:
    """어느 방의 글이 실제로 브리핑에 쓰였나.

    3단계에서 남긴 숙제(방 선별 판단)의 근거를 여기서 만든다.
    '단독 인용'은 그 방 혼자 올린 글이 브리핑에 쓰인 횟수다 —
    그 방을 뺐을 때 실제로 사라지는 내용이 얼마인지를 뜻한다."""
    by_id = {c["cluster_id"]: c for c in clusters}
    cited = set()
    for section in ("themes", "cross_validated", "global"):
        for item in report.get(section, []):
            cited.update(item["cluster_ids"])

    total, solo = collections.Counter(), collections.Counter()
    for cid in cited:
        c = by_id[cid]
        for r in c["rooms"]:
            total[r] += 1
        if c["room_count"] == 1:
            solo[c["rooms"][0]] += 1

    rooms = {r for c in clusters for r in c["rooms"]}
    return sorted(((r, total[r], solo[r]) for r in rooms), key=lambda x: -x[1])


# ══ 사람이 읽는 형태로 ═══════════════════════════════════════════════

def render(report: dict, day: str) -> str:
    L = [f"# 📈 {day} 시장 브리핑", ""]

    L += ["## 📊 오늘의 3대 시장 핵심 테마", ""]
    for i, t in enumerate(report["themes"], 1):
        tag = f" · {t['room_count']}개 방" if t["room_count"] else ""
        L.append(f"### {i}. {t['title']}{tag}")
        L.append(t["summary"])
        if t["tickers"]:
            L.append(f"관련 종목: {', '.join(t['tickers'])}")
        L.append("")

    L += ["## 🔥 다수 방 중복 언급 종목", ""]
    if not report["cross_validated"]:
        L.append("해당 없음.")
    for c in report["cross_validated"]:
        L.append(f"### {c['ticker']} — {c['room_count']}개 방")
        if c["rooms"]:
            L.append(f"*{' / '.join(c['rooms'])}*")
        L.append(f"- **사실**: {c['news']}")
        L.append(f"- **해석**: {c['interpretation']}")
        L.append("")

    L += ["## 🌐 해외 뉴스 & 글로벌 증시 영향", ""]
    if not report["global"]:
        L.append("해당 없음.")
    for g in report["global"]:
        L.append(f"### {g['topic']}")
        L.append(f"- {g['point']}")
        L.append(f"- **국내 영향**: {g['impact']}")
        L.append("")

    ins = report["insight"]
    L += ["## 💡 종합 투자 인사이트", "", ins["summary"], ""]
    if ins["watchlist"]:
        L += ["**관찰 대상**", ""] + [f"- {w}" for w in ins["watchlist"]] + [""]
    if ins["warnings"]:
        L += ["**주의**", ""] + [f"- ⚠️ {w}" for w in ins["warnings"]] + [""]

    L += ["---", "", "*투자 판단 보조 자료이며 매매 신호가 아닙니다. "
          "원문은 검증되지 않은 텔레그램 메시지입니다.*"]
    return "\n".join(L)


# ══ 실행 ═════════════════════════════════════════════════════════════

def pick_input(args: list[str]) -> Path:
    days = [a for a in args if re.fullmatch(r"\d{4}-\d{2}-\d{2}", a)]
    if days:
        p = DATA_DIR / f"{days[0]}.clusters.json"
        if not p.exists():
            sys.exit(f"[중단] {p.name} 이 없습니다. cluster.py 를 먼저 실행하세요.")
        return p
    files = sorted(DATA_DIR.glob("*.clusters.json"))
    if not files:
        sys.exit("[중단] 클러스터 파일이 없습니다. cluster.py 를 먼저 실행하세요.")
    return files[-1]


def main() -> None:
    dry = "--dry-run" in sys.argv
    src = pick_input(sys.argv[1:])
    day = src.name.removesuffix(".clusters.json")
    clusters = json.loads(src.read_text(encoding="utf-8"))

    payload, info = build_payload(clusters)
    est_in = len(payload) // 1000
    print(f"입력: {src.relative_to(ROOT)}  ({info['clusters']:,} 클러스터)")
    print(f"압축: 원본 대비 {info['saved']:,}자 절감 → 전송 {info['chars']:,}자")
    print(f"모델: {prompts.MODEL} (추론 강도 {prompts.REASONING_EFFORT})")
    print(f"예상 입력 토큰: 대략 {est_in * 7 // 10}k~{est_in * 12 // 10}k\n")

    if info["chars"] > MAX_PAYLOAD_CHARS:
        sys.exit(f"[중단] 입력이 {info['chars']:,}자로 안전 한도({MAX_PAYLOAD_CHARS:,})를 넘습니다.")

    if dry:
        sample = payload[:1200]
        print("─" * 66)
        print(sample)
        print("─" * 66)
        print("(--dry-run: 호출하지 않았습니다)")
        return

    # 이미 만든 브리핑이 있으면 다시 부르지 않는다. Sol 호출은 한 번에 1,600원이고,
    # 자동화에서 재시도가 걸리거나 파이프라인을 다시 돌릴 때 조용히 두 번 청구된다.
    out_json = REPORTS_DIR / f"{day}.report.json"
    if out_json.exists() and "--force" not in sys.argv:
        print(f"[건너뜀] reports/{out_json.name} 이 이미 있습니다. API를 호출하지 않았습니다.")
        print("         다시 만들려면: python summarize.py --force")
        return

    load_dotenv(ROOT / ".env")
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        sys.exit("[중단] .env 의 OPENAI_API_KEY 가 비어 있습니다.")

    print("Sol 호출 중… (추론에 1~3분 걸릴 수 있습니다)")
    client = OpenAI(api_key=api_key)
    resp = client.chat.completions.create(
        model=prompts.MODEL,
        messages=[
            {"role": "system", "content": prompts.SYSTEM},   # 고정 프롬프트를 앞에 (캐싱)
            {"role": "user", "content": payload},
        ],
        response_format=prompts.RESPONSE_FORMAT,
        reasoning_effort=prompts.REASONING_EFFORT,
    )

    report = json.loads(resp.choices[0].message.content)
    stats = attach_counts(report, clusters)

    # ── 비용 (추정이 아니라 실제 청구 기준) ──────────────────────────
    u = resp.usage
    det = getattr(u, "completion_tokens_details", None)
    reasoning = getattr(det, "reasoning_tokens", 0) or 0
    cost = u.prompt_tokens / 1e6 * PRICE_IN + u.completion_tokens / 1e6 * PRICE_OUT

    REPORTS_DIR.mkdir(exist_ok=True)
    report["_meta"] = {
        "date": day, "model": prompts.MODEL, "reasoning_effort": prompts.REASONING_EFFORT,
        "clusters_in": len(clusters), "clusters_cited": len(stats["cited"]),
        "prompt_tokens": u.prompt_tokens, "completion_tokens": u.completion_tokens,
        "reasoning_tokens": reasoning, "cost_usd": round(cost, 4),
    }
    (REPORTS_DIR / f"{day}.report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    md = render(report, day)
    (REPORTS_DIR / f"{day}.md").write_text(md, encoding="utf-8-sig")

    print(f"\n{'=' * 66}")
    print(f"실제 사용량 — 추정이 아니라 청구 기준")
    print(f"{'=' * 66}")
    print(f"  입력  {u.prompt_tokens:>8,} 토큰   ${u.prompt_tokens/1e6*PRICE_IN:.3f}")
    print(f"  출력  {u.completion_tokens:>8,} 토큰   ${u.completion_tokens/1e6*PRICE_OUT:.3f}"
          f"   (그중 추론 {reasoning:,})")
    print(f"  합계                    ${cost:.3f}  ≈ {cost*USD_KRW:,.0f}원/일"
          f"  → 월 {cost*USD_KRW*30:,.0f}원")

    print(f"\n생성 결과")
    print(f"  테마 {len(report['themes'])}건 / 중복종목 {len(report['cross_validated'])}건 "
          f"/ 해외 {len(report['global'])}건 / 관찰 {len(report['insight']['watchlist'])}종목 "
          f"/ 경고 {len(report['insight']['warnings'])}건")
    print(f"  인용한 덩어리 {len(stats['cited']):,} / 전체 {len(clusters):,}개 "
          f"({len(stats['cited'])/len(clusters)*100:.0f}%)")
    if stats["bad_ids"]:
        print(f"  ⚠️ 존재하지 않는 덩어리 번호 {len(stats['bad_ids'])}개를 지어냄: "
              f"{stats['bad_ids'][:10]} — 코드가 제거함")

    print(f"\n{'=' * 66}")
    print("방별 기여 — 브리핑에 실제로 쓰인 글이 어느 방에서 왔나")
    print(f"{'=' * 66}")
    print(f"  {'방':<34}{'인용':>6}{'단독인용':>9}   단독인용 = 그 방을 빼면 사라지는 내용")
    for room, tot, solo in contribution(report, clusters):
        print(f"  {room[:32]:<34}{tot:>6}{solo:>9}")

    print(f"\n저장: reports/{day}.md  ← 이걸 읽어보세요")
    print(f"      reports/{day}.report.json  ← 5단계 전송이 읽습니다")


if __name__ == "__main__":
    main()
