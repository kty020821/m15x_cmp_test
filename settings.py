"""
config/settings.py 맨 끝에 아래를 붙여 넣으세요.
════════════════════════════════════════════════════════════
"""

# ══════════════════════════════════════════════════════════
# 분석 AI(LLM) 설정
#
#   ★ 값은 config/llm_config.py 에서 관리한다.
#     이 파일(settings.py)은 손대지 않아도 된다.
#
#   ★ 파일이 없으면 조용히 넘어간다 — 서버는 정상 기동하고,
#     AI 답변 대신 계산 결과만 표시된다.
#     (배포 서버에 파일을 아직 안 만든 상태에서도 떠야 하므로)
#
#   ★ 환경변수(LLM_URL 등)가 이미 설정돼 있으면 그쪽이 우선이다.
#     기존 운영 방식을 쓰던 환경이 깨지지 않게 하기 위한 것.
# ══════════════════════════════════════════════════════════
import os as _os

LLM_URL = _os.getenv('LLM_URL', '')
LLM_API_KEY = _os.getenv('LLM_API_KEY', '')
LLM_MODEL = _os.getenv('LLM_MODEL', '')
LLM_TIMEOUT = int(_os.getenv('LLM_TIMEOUT', '120') or 120)

try:
    from .llm_config import (            # noqa: F401
        LLM_URL as _f_url,
        LLM_API_KEY as _f_key,
        LLM_MODEL as _f_model,
        LLM_TIMEOUT as _f_timeout,
    )
    # 환경변수가 비어 있을 때만 파일 값으로 채운다
    LLM_URL = LLM_URL or _f_url
    LLM_API_KEY = LLM_API_KEY or _f_key
    LLM_MODEL = LLM_MODEL or _f_model
    LLM_TIMEOUT = LLM_TIMEOUT or _f_timeout
except ImportError:
    pass                                  # 파일이 없으면 그대로 진행
