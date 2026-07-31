"""
equipment/param_types.py
════════════════════════════════════════════════════════════
파라미터 타입 분류 — 이 파일이 유일한 규칙 소재지

  기준정보 셋업(config_service)과 인라인 점검(monitor_service)이
  같은 규칙을 써야 한다. 예전에는 양쪽에 정규식이 흩어져 있어
  하나를 고치면 다른 쪽과 어긋났다. 규칙 수정은 여기서만 한다.

────────────────────────────────────────────────────────────
타입
  THK       두께·제거량 계열      판정: 평균±σ
  TIME      Polishing Time        판정: 평균±σ
  PRESSURE  압력 (존/리테이너링)  판정: 평균±σ
  PART      소모품 (Pad/Head/Disk/Brush/RRing)  판정: 제외 (아래 설명)
  DEFECT    Defect 카운트          판정: 중앙값 배수·분위수
  ETC       그 외                  판정: 평균±σ

분류 순서 (위에서 걸리면 끝)
  1  DEFECT      defect 어휘
  2  THK         THK / OCD / REV        ← 명시적 두께 어휘
  3  PRESSURE    Z<숫자> / A<숫자> / W<숫자> / _RR / 압력 어휘
  4  PART        PAD / HEAD / DISK / BRUSH / RRING
  5  TIME 계열   이름에 TIME 이 있으면
                   독립 숫자 세그먼트 있음 → TIME  (PR_4_TIME = 4step 연마시간)
                   없음                   → PART  (PR_TIME  = Platen R 패드시간)
  6  THK(2차)    AVG / RAN              ← 통계 접미사 (약한 신호)
  7  ETC

★ AVG / RAN 을 왜 6번으로 내렸나
  이 둘은 어디에나 붙는 통계 접미사다. Z1_AVG 는 존1 압력의 평균이지
  두께가 아니다. 그래서 PRESSURE·PART·TIME 판정을 먼저 하고,
  아무것도 안 걸린 것 중에서만 AVG/RAN 을 두께로 본다.
  THK / OCD / REV 는 명시적 두께 어휘라 2번에 그대로 둔다.

★ PART 를 왜 판정에서 빼나
  소모품 값은 사용량이 누적되며 단조 증가하고 PM 에서 0으로 리셋된다.
  '30일 평균 대비 σ' 로 보면 매일 이탈로 잡히므로 판정이 무의미하다.
  참고용으로만 표시한다. monitor_service.JUDGE_PART 로 켤 수 있다.
════════════════════════════════════════════════════════════
"""

import re

# ── 타입 목록 (화면 드롭박스 순서) ────────────────────────
TYPES = ['THK', 'TIME', 'PRESSURE', 'PART', 'DEFECT', 'ETC']

TYPE_LABELS = {
    'THK':      '두께·제거량',
    'TIME':     'Polishing Time',
    'PRESSURE': '압력',
    'PART':     '소모품 (Pad/Head/Disk)',
    'DEFECT':   'Defect',
    'ETC':      '기타',
}

# 평균±σ 규칙으로 판정하는 타입 (계측값 계열)
MEASURED_TYPES = {'THK', 'TIME', 'PRESSURE', 'ETC'}


# ══════════════════════════════════════════════════════════
# 어휘 — 규칙 수정은 여기서
# ══════════════════════════════════════════════════════════
# defect 임이 분명한 어휘만. CNT$/COUNT$ 처럼 넓게 잡으면
# PADCNT / DISKCNT 같은 소모품 카운터가 오탐된다.
RE_DEFECT = re.compile(r'DEFECT|SCRATCH|PARTICLE|RESIDUE|^DEF_|_DEF_|_DEF$')

# 명시적 두께 어휘
RE_THK_CORE = re.compile(r'THK|OCD|REV')

# 통계 접미사 — 약한 신호라 마지막에 본다
RE_THK_STAT = re.compile(r'AVG|RAN')

# 소모품
#   BRUSH  세정 브러시
#   RRING  리테이너 링 (부품 자체 — 사용량·수명)
#   ※ 압력값인 '_RR 로 끝나는 이름'(리테이너 링 압력)과는 다르다.
#     RRING_CNT → PART / PA_Z1_RR → PRESSURE 로 각각 갈린다.
RE_PART = re.compile(r'PAD|HEAD|DISK|BRUSH|RRING')

# 압력
#   Z1~Z5  캐리어 헤드 존 압력
#   A1~    존 표기 변형 (Z 와 같은 성격)
#   W1~    존 표기 변형
#   _RR    Retainer Ring 압력
#   세그먼트 시작 위치만 인정한다 (SIZE_W1 은 맞고 PAZ1 은 아님).
#   그래서 AREA1 · ADD_1 처럼 A 뒤에 숫자가 바로 오지 않는 이름은 걸리지 않는다.
RE_PRESSURE = re.compile(r'(^|_)Z\d|(^|_)A\d|(^|_)W\d|_RR$')

# ★ 사용자 규칙에 없던 보강.
#   HEAD_PRESSURE 처럼 압력인데 PART 어휘(HEAD)가 먼저 걸리는 이름이 있다.
#   불필요하면 이 줄만 빈 패턴으로 두면 된다.
RE_PRESSURE_WORD = re.compile(r'PRESS|PRS|PSI')


def _has_step_no(p):
    """
    '_' 로 나눈 세그먼트 중 순수 숫자가 있는가.

      PR_4_TIME   → [PR, 4, TIME]    → 있음 → 4step 연마시간
      PA_03_TIME  → [PA, 03, TIME]   → 있음
      PR_TIME     → [PR, TIME]       → 없음 → Platen R 패드시간
      PL1_TIME    → [PL1, TIME]      → 없음 (숫자가 존 표기에 붙어 있음)
      PR2_TIME    → [PR2, TIME]      → 없음
    """
    return any(seg.isdigit() for seg in p.split('_') if seg)


def classify(param):
    """파라미터 이름 → 타입. 판정 불가면 'ETC'"""
    p = str(param or '').upper().strip()
    if not p:
        return 'ETC'

    if RE_DEFECT.search(p):
        return 'DEFECT'
    if RE_THK_CORE.search(p):
        return 'THK'
    if RE_PRESSURE.search(p) or RE_PRESSURE_WORD.search(p):
        return 'PRESSURE'
    if RE_PART.search(p):
        return 'PART'
    if 'TIME' in p:
        return 'TIME' if _has_step_no(p) else 'PART'
    if RE_THK_STAT.search(p):
        return 'THK'
    return 'ETC'


def normalize(value):
    """입력된 타입 문자열을 유효한 타입으로. 아니면 빈 문자열(=자동)"""
    v = str(value or '').strip().upper()
    return v if v in TYPES else ''


def resolve(param, given=''):
    """
    지정 타입이 있으면 그것을, 없으면 자동 분류 결과를 쓴다.
    기준정보에 PARAM_TYPE 을 비워 둬도 동작하게 하는 지점.
    """
    return normalize(given) or classify(param)


def classify_many(params):
    """[param, ...] → {param: type}"""
    return {str(p).upper().strip(): classify(p)
            for p in (params or []) if str(p).strip()}


def label(t):
    return TYPE_LABELS.get(str(t or '').upper(), str(t or ''))


def options():
    """화면 드롭박스용 [(값, 표시명), ...]"""
    return [(t, f'{t} — {TYPE_LABELS[t]}') for t in TYPES]
