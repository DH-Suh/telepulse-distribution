"""
전체 파이프라인 — 수집 → 묶기 → 요약 → 전송

매일 아침 스케줄러(GitHub Actions)가 이 파일 하나만 호출한다.

사용법:
  python main.py                  # 전 과정 실행
  python main.py --skip-collect   # 이미 수집한 파일로 재실행 (프롬프트 수정 후 테스트용)
  python main.py --no-send        # 전송 없이 브리핑 생성까지만

각 단계를 별도 프로세스로 실행한다. 한 단계가 실패하면 거기서 멈추고 무엇이 실패했는지
분명히 남는다. 또 각 스크립트를 따로 돌리는 방법(개발 중 계속 쓴다)과 동작이 같아진다.
"""

import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent
PY = sys.executable


def run(script: str, label: str) -> None:
    print(f"\n{'━' * 66}\n▶ {label}\n{'━' * 66}")
    result = subprocess.run([PY, str(ROOT / script)], cwd=ROOT)
    if result.returncode != 0:
        sys.exit(f"\n[중단] {script} 실패 (종료 코드 {result.returncode}). "
                 f"이후 단계를 실행하지 않습니다.")


def main() -> None:
    steps = []
    if "--skip-collect" not in sys.argv:
        steps.append(("collect.py", "1·2단계 — 텔레그램 수집"))
    steps.append(("cluster.py", "3단계 — 필터 + 중복 묶기"))
    steps.append(("summarize.py", "4단계 — AI 요약"))
    if "--no-send" not in sys.argv:
        steps.append(("send.py", "5단계 — 텔레그램 전송"))

    for script, label in steps:
        run(script, label)

    print(f"\n{'━' * 66}\n✅ 전 과정 완료\n{'━' * 66}")


if __name__ == "__main__":
    main()
