"""
config/settings.py 맨 끝에 아래를 붙여 넣으세요.

  ★ settings.py 위쪽에 이미 `import os` 가 있을 것입니다.
    없다면 이 블록 앞에 한 줄 추가하세요.
════════════════════════════════════════════════════════════
"""

# ══════════════════════════════════════════════════════════
# 분석 AI(LLM) 설정
#
#   ★ 값은 config/llm_config.py 에서 관리한다.
#     모델이 바뀌면 그 파일의 LLM_MODEL 한 줄만 고치면 된다.
#
#   ★ 파일이 없으면 조용히 넘어간다 — 서버는 정상 기동하고,
#     AI 답변 대신 계산 결과만 표시된다.
#     (배포 서버에 파일을 아직 안 만든 상태에서도 떠야 하므로)
#
#   ★ 환경변수가 설정돼 있으면 그쪽이 우선이다.
#     기존 운영 방식을 쓰던 환경이 깨지지 않게 하기 위한 것.
# ══════════════════════════════════════════════════════════
LLM_URL = os.getenv('LLM_URL', '')
LLM_API_KEY = os.getenv('LLM_API_KEY', '')
LLM_MODEL = os.getenv('LLM_MODEL', '')
LLM_TIMEOUT = int(os.getenv('LLM_TIMEOUT', '120') or 120)

try:
    from . import llm_config

    # 환경변수가 비어 있을 때만 파일 값으로 채운다
    LLM_URL = LLM_URL or getattr(llm_config, 'LLM_URL', '')
    LLM_API_KEY = LLM_API_KEY or getattr(llm_config, 'LLM_API_KEY', '')
    LLM_MODEL = LLM_MODEL or getattr(llm_config, 'LLM_MODEL', '')
    LLM_TIMEOUT = LLM_TIMEOUT or getattr(llm_config, 'LLM_TIMEOUT', 120)
except ImportError:
    pass                                  # 파일이 없으면 그대로 진행
