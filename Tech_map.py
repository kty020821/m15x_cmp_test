"""
equipment/tech_map.py
════════════════════════════════════════════════════════════
TECH ↔ LOT_CD(device) 매핑 + OPER_ID ↔ 공정명 매핑

  구닥스 기준정보는 공정(OPER) 단위라 TECH↔LOT_CD 관계를 갖지 않는다.
  (device 별로 넣으면 PARAM × device 로 행이 폭증하므로)
  그래서 이 매핑만 따로 여기서 관리한다.

  공정명도 여기서 관리한다.
  ★ 웹 프로세스는 사내 모듈(Lake/구닥스 클라이언트)을 쓸 수 없다.
    그래서 화면에서 구닥스를 직접 조회해 OPER_DESC 를 가져오려 하면
    항상 실패하고, 드롭박스에 공정명 없이 OPER_ID 만 표시된다.
    공정명은 적재 배치가 아니라 화면이 쓰는 값이므로 여기 둔다.

  ★ device 가 추가되면 TECH_LOT_MAP 에 LOT_CD 한 줄,
    공정이 추가되면 OPER_NAME_MAP 에 한 줄만 넣으면 된다.
════════════════════════════════════════════════════════════
"""


# ── TECH → LOT_CD 목록 ────────────────────────────────────
TECH_LOT_MAP = {
    'LUCY': ['5E2', '5E9'],
    # 'ROSE': ['5F1', '5F2'],
    # device 추가 시 해당 TECH 목록에 LOT_CD 만 추가
}


# ── OPER_ID → 공정명 ──────────────────────────────────────
#   키는 구닥스 OPER_ID 와 정확히 같아야 한다 (대문자 기준으로 비교).
#   여기 없는 공정은 화면에 OPER_ID 만 표시된다 — 동작에는 문제 없다.
#   ★ 적재된 공정을 추가할 때 여기 한 줄
OPER_NAME_MAP = {
    # 'V5071000B': 'M1 Cu CMP',
    # 'X106100B':  'M2 Cu CMP',
}


def all_techs():
    """화면 TECH 드롭박스용"""
    return list(TECH_LOT_MAP.keys())


def lots_of_tech(tech):
    """해당 TECH 의 LOT_CD 목록"""
    return TECH_LOT_MAP.get(tech, [])


def tech_of_lot(lot_cd):
    """LOT_CD 로 TECH 역조회 (없으면 None)"""
    for tech, lots in TECH_LOT_MAP.items():
        if lot_cd in lots:
            return tech
    return None


def known_lots():
    """등록된 전체 LOT_CD"""
    return [lc for lots in TECH_LOT_MAP.values() for lc in lots]


def oper_names():
    """{OPER_ID(대문자): 공정명} — 화면 OPER 드롭박스용"""
    return {str(k).upper().strip(): str(v).strip()
            for k, v in OPER_NAME_MAP.items() if k and v}


def name_of_oper(oper_id):
    """OPER_ID 의 공정명 (없으면 빈 문자열)"""
    return oper_names().get(str(oper_id).upper().strip(), '')
