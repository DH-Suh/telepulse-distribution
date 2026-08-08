"""파이프라인 원본(list_rooms.py 등)이 있는 폴더를 sys.path 에 넣는다.

임포트만 해도 효과가 난다:  import pipeline_path

**폴더 깊이가 두 환경에서 다르다.** 고정된 `.parent.parent.parent` 로 올라가면
한쪽이 반드시 틀린다.

    배포본   build/setup/server.py         → 원본은 build/          (한 단계 위)
    개발 중  distribution/setup/server.py  → 원본은 그 상위 폴더     (두 단계 위)

그래서 위로 올라가며 list_rooms.py 가 실제로 있는 폴더를 찾는다.
"""

import sys
from pathlib import Path

MARKER = "list_rooms.py"

_here = Path(__file__).resolve().parent
ROOT: Path | None = next(
    (p for p in (_here.parent, _here.parent.parent) if (p / MARKER).exists()), None)

if ROOT is None:
    raise ImportError(
        f"{MARKER} 를 찾지 못했습니다. setup/ 폴더가 파이프라인 코드와 같은 저장소에 "
        f"있어야 합니다. (찾아본 곳: {_here.parent}, {_here.parent.parent})")

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
