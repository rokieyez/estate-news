import sys
from pathlib import Path

# 저장소 루트를 import 경로에 넣어 `rebrief` 를 패키지로 쓴다.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
