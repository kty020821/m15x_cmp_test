"""
equipment/tech_map.py
════════════════════════════════════════════════════════════
TECH ↔ LOT_CD(device) 매핑

  구닥스 기준정보는 공정(OPER) 단위라 TECH↔LOT_CD 관계를 갖지 않는다.
  (device 별로 넣으면 PARAM × device 로 행이 폭증하므로)
  그래서 이 매핑만 따로 여기서 관리한다.

  ★ device 가 추가되면 아래 TECH_LOT_MAP 에 LOT_CD 한 줄만 넣으면 된다.
    파라미터 정보와 무관하므로 여기는 절대 커지지 않는다.
════════════════════════════════════════════════════════════
"""


# ── TECH → LOT_CD 목록 ────────────────────────────────────
TECH_LOT_MAP = {
    'LUCY': ['5E2', '5E9'],
    # 'ROSE': ['5F1', '5F2'],
    # device 추가 시 해당 TECH 목록에 LOT_CD 만 추가
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
