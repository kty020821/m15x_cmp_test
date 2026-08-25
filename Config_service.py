"""
equipment/config_service.py
════════════════════════════════════════════════════════════
기준정보 관리 — PostgreSQL 이 원본(single source of truth)

  구닥스가 불안정해 기준정보를 자체 DB 로 옮겼다.
  수정은 웹 셋업 페이지에서만 한다. 구닥스는 초기 가져오기 소스일 뿐이다.

────────────────────────────────────────────────────────────
구조 — "곱하기" 설계

  cmp_cfg_oper    공정 1행    OPER_DESC, FAB, EQ_MODEL, 사전공정 3종
  cmp_cfg_lot     device 1행  LOT_CD + RECIPE_ID
  cmp_cfg_param   항목 1행    PARAM, PARAM_TYPE
  cmp_cfg_defect  device 1행  LOT_CD + STEP_ID (defect 계측 스텝)

  ★ 평면 형태는 저장하지 않는다. build_config_df() 가 읽을 때
    oper ⋈ lot ⋈ param 으로 조합해 구닥스가 주던 것과 똑같은
    평면 DataFrame(PARAM 마다 1행, 공정 정보 반복)을 만든다.
    그래서 get_oper_cond / fetch_src / fetch_apc 등 배치 파이프라인은
    한 줄도 바뀌지 않는다.

  ★ 새 device 가 생기면 cmp_cfg_lot 에 한 줄만 넣으면 된다.
    파라미터가 50개든 100개든 입력량은 그대로다.
    (구닥스는 평면이라 device 하나에 파라미터 수만큼 행이 늘었다)

  ★ RECIPE_ID 를 LOT_CD 와 같은 행에 둔 이유
    레시피는 보통 device 종속이다 (E2_M1CU_... 의 E2 가 5E2).
    따로 관리하면 조합할 때 5E2 × E9레시피 같은 없는 짝이 생기고,
    새 device 를 넣을 때 레시피를 어디에 적을지가 애매해진다.
    한 device 가 레시피 여러 개를 쓰면 행을 여러 개 넣으면 된다.
════════════════════════════════════════════════════════════
"""

import re
from datetime import datetime

import pandas as pd
from django.db import connections

from . import param_types as pt

T_OPER   = 'cmp_cfg_oper'
T_LOT    = 'cmp_cfg_lot'
T_PARAM  = 'cmp_cfg_param'
T_DEFECT = 'cmp_cfg_defect'
T_RESP   = 'cmp_cfg_response'

# ★ 타입 목록과 분류 규칙은 param_types.py 가 단일 소재지다.
#   여기서 다시 정의하면 양쪽이 어긋난다.
PARAM_TYPES = pt.TYPES

# 구닥스가 주던 평면 컬럼 (순서 유지 — 기존 코드가 이 이름으로 읽는다)
FLAT_COLS = ['FAB', 'LOT_CD', 'OPER_ID', 'OPER_DESC', 'EQ_MODEL', 'RECIPE_ID',
             'PARAM', 'PRE_OPER_ID', 'PRE_OPER_DESC', 'PRE_OPER_PARAM',
             'PARAM_TYPE']


def _conn():
    return connections['analysis_db']


def _an_table(oper_id):
    return f"cmp_analysis_{re.sub(r'[^0-9A-Za-z_]', '_', str(oper_id)).lower()}"


def _s(v):
    return '' if v is None else str(v).strip()


def _up(v):
    return _s(v).upper()


# ══════════════════════════════════════════════════════════
# 테이블
# ══════════════════════════════════════════════════════════
def ensure_tables():
    with _conn().cursor() as cur:
        cur.execute(f'''
            CREATE TABLE IF NOT EXISTS {T_OPER} (
              oper_id        VARCHAR(100) PRIMARY KEY,
              oper_desc      VARCHAR(200),
              fab            VARCHAR(50),
              eq_model       VARCHAR(50),
              pre_oper_id    VARCHAR(100),
              pre_oper_desc  VARCHAR(200),
              pre_oper_param VARCHAR(200),
              use_yn         VARCHAR(1) DEFAULT 'Y',
              updated_at     TIMESTAMP,
              updated_by     VARCHAR(100)
            )
        ''')
        # device 1행 = LOT_CD + 그 device 가 쓰는 RECIPE_ID
        cur.execute(f'''
            CREATE TABLE IF NOT EXISTS {T_LOT} (
              id BIGSERIAL PRIMARY KEY,
              oper_id   VARCHAR(100),
              lot_cd    VARCHAR(50),
              recipe_id VARCHAR(200),
              use_yn    VARCHAR(1) DEFAULT 'Y'
            )
        ''')
        cur.execute(f'''
            CREATE TABLE IF NOT EXISTS {T_PARAM} (
              id BIGSERIAL PRIMARY KEY,
              oper_id    VARCHAR(100),
              param      VARCHAR(200),
              param_type VARCHAR(20),
              use_yn     VARCHAR(1) DEFAULT 'Y'
            )
        ''')
        # defect 계측 스텝 — device 마다 검사 스텝이 다르다
        cur.execute(f'''
            CREATE TABLE IF NOT EXISTS {T_DEFECT} (
              id BIGSERIAL PRIMARY KEY,
              oper_id VARCHAR(100),
              lot_cd  VARCHAR(50),
              step_id VARCHAR(100),
              step_desc VARCHAR(200),
              param   VARCHAR(100),
              use_yn  VARCHAR(1) DEFAULT 'Y'
            )
        ''')
        # 기존 테이블에는 param 이 없다 — 있으면 그대로 두고 없으면 추가한다
        cur.execute(f"ALTER TABLE {T_DEFECT} "
                    f"ADD COLUMN IF NOT EXISTS param VARCHAR(100)")

        # ── Response 계측 ────────────────────────────────
        #   Inline 에서 실제로 관리하는 것은 Response 와 Defect 다.
        #   Defect 과 같은 구조 — 스텝 하나에 관리 파라미터 여러 개,
        #   파라미터 1개당 1행으로 등록한다.
        cur.execute(f'''
            CREATE TABLE IF NOT EXISTS {T_RESP} (
              id BIGSERIAL PRIMARY KEY,
              oper_id VARCHAR(100),
              lot_cd  VARCHAR(50),
              step_id VARCHAR(100),
              step_desc VARCHAR(200),
              param   VARCHAR(100),
              use_yn  VARCHAR(1) DEFAULT 'Y'
            )
        ''')
        cur.execute(f'CREATE INDEX IF NOT EXISTS idx_{T_RESP}_oper '
                    f'ON {T_RESP} (oper_id)')
        for t in (T_LOT, T_PARAM, T_DEFECT):
            cur.execute(f'CREATE INDEX IF NOT EXISTS idx_{t}_oper '
                        f'ON {t} (oper_id)')


def _exists(cur, t):
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", [t])
    return bool(cur.fetchone()[0])


# ══════════════════════════════════════════════════════════
# 조회
# ══════════════════════════════════════════════════════════
def list_opers():
    """공정 목록 — 구성 개수와 적재 테이블 존재 여부까지"""
    ensure_tables()
    with _conn().cursor() as cur:
        cur.execute(f'''
            SELECT o.oper_id, o.oper_desc, o.fab, o.eq_model, o.use_yn,
                   o.updated_at,
                   (SELECT COUNT(DISTINCT l.lot_cd) FROM {T_LOT} l
                     WHERE l.oper_id = o.oper_id),
                   (SELECT COUNT(*) FROM {T_PARAM}  p WHERE p.oper_id = o.oper_id),
                   (SELECT COUNT(*) FROM {T_DEFECT} d WHERE d.oper_id = o.oper_id),
                   (SELECT COUNT(*) FROM {T_RESP}   s WHERE s.oper_id = o.oper_id)
            FROM {T_OPER} o ORDER BY o.oper_desc NULLS LAST, o.oper_id
        ''')
        rows = cur.fetchall()

        cur.execute("SELECT tablename FROM pg_tables WHERE tablename LIKE %s",
                    ['cmp_analysis_%'])
        tables = {r[0] for r in cur.fetchall()}

    return [{
        'oper_id': r[0], 'oper_desc': r[1] or '', 'fab': r[2] or '',
        'eq_model': r[3] or '', 'use_yn': r[4] or 'Y',
        'updated_at': str(r[5])[:19] if r[5] else '',
        'n_lot': r[6], 'n_param': r[7], 'n_defect': r[8], 'n_resp': r[9],
        'loaded': _an_table(r[0]) in tables,
    } for r in rows]


def get_oper(oper_id):
    """공정 1건 전체 구성"""
    ensure_tables()
    with _conn().cursor() as cur:
        cur.execute(f'''
            SELECT oper_id, oper_desc, fab, eq_model,
                   pre_oper_id, pre_oper_desc, pre_oper_param, use_yn
            FROM {T_OPER} WHERE oper_id = %s
        ''', [oper_id])
        row = cur.fetchone()
        if not row:
            return None

        cur.execute(f'''
            SELECT lot_cd, recipe_id, use_yn FROM {T_LOT}
            WHERE oper_id = %s ORDER BY lot_cd, recipe_id
        ''', [oper_id])
        lots = [{'lot_cd': r[0] or '', 'recipe_id': r[1] or '',
                 'use_yn': r[2] or 'Y'} for r in cur.fetchall()]

        cur.execute(f'''
            SELECT param, param_type, use_yn FROM {T_PARAM}
            WHERE oper_id = %s ORDER BY param
        ''', [oper_id])
        params = [{'param': r[0], 'param_type': r[1] or '', 'use_yn': r[2] or 'Y'}
                  for r in cur.fetchall()]

        # Defect · Response — 둘 다 '스텝 + 관리 파라미터 1개' 가 한 행이다
        cur.execute(f'''
            SELECT lot_cd, step_id, step_desc, param, use_yn FROM {T_DEFECT}
            WHERE oper_id = %s ORDER BY lot_cd, step_id, param
        ''', [oper_id])
        defects = [{'lot_cd': r[0] or '', 'step_id': r[1] or '',
                    'step_desc': r[2] or '', 'param': r[3] or '',
                    'use_yn': r[4] or 'Y'}
                   for r in cur.fetchall()]

        cur.execute(f'''
            SELECT lot_cd, step_id, step_desc, param, use_yn FROM {T_RESP}
            WHERE oper_id = %s ORDER BY lot_cd, step_id, param
        ''', [oper_id])
        resps = [{'lot_cd': r[0] or '', 'step_id': r[1] or '',
                  'step_desc': r[2] or '', 'param': r[3] or '',
                  'use_yn': r[4] or 'Y'}
                 for r in cur.fetchall()]

    return {
        'oper_id': row[0], 'oper_desc': row[1] or '', 'fab': row[2] or '',
        'eq_model': row[3] or '', 'pre_oper_id': row[4] or '',
        'pre_oper_desc': row[5] or '', 'pre_oper_param': row[6] or '',
        'use_yn': row[7] or 'Y',
        'lots': lots, 'params': params, 'defects': defects,
        'resps': resps,
        # ★ 연계 공정 — 사전공정 chamber·측정값을 device 단위로 담는다
        'links': links_of_oper(oper_id),
    }


# ══════════════════════════════════════════════════════════
# 저장 / 삭제
# ══════════════════════════════════════════════════════════
def type_overview():
    """
    전 공정 파라미터의 현재 타입 분포와, ETC 로 남은 이름 목록.

    ETC 목록이 핵심이다 — 규칙이 실제 이름 형태를 못 잡고 있으면
    여기 그대로 드러난다. (예: 규칙은 (^|_)A\\d 인데 실제 이름이
    PAA1 처럼 A 앞에 글자가 붙어 있으면 안 걸린다)
    """
    ensure_tables()
    counts, etc = {}, []
    with _conn().cursor() as cur:
        cur.execute(f'''
            SELECT DISTINCT param, COALESCE(param_type, '')
            FROM {T_PARAM} ORDER BY param
        ''')
        for param, saved in cur.fetchall():
            t = pt.resolve(param, saved)
            counts[t] = counts.get(t, 0) + 1
            if t == 'ETC' and len(etc) < 80:
                etc.append({'param': param,
                            'saved': saved or '(자동)',
                            'auto': pt.classify(param)})
    return {'counts': counts, 'etc': etc, 'rule': pt.rule_summary()}


def type_mismatches():
    """
    저장된 param_type 과 현재 규칙의 자동 분류 결과가 다른 항목.

    분류 규칙(param_types.py)을 고치면 이미 저장된 타입은 그대로 남아
    같은 파라미터가 공정마다 다른 타입을 갖게 된다. 그걸 찾아낸다.
    """
    ensure_tables()
    out = []
    with _conn().cursor() as cur:
        cur.execute(f'''
            SELECT p.oper_id, o.oper_desc, p.param, COALESCE(p.param_type, '')
            FROM {T_PARAM} p LEFT JOIN {T_OPER} o ON o.oper_id = p.oper_id
            ORDER BY p.param, p.oper_id
        ''')
        for oper_id, desc, param, saved in cur.fetchall():
            auto = pt.classify(param)
            if saved and saved != auto:
                out.append({'oper_id': oper_id, 'oper_desc': desc or '',
                            'param': param, 'saved': saved, 'auto': auto})
    return out


def reclassify_all(mode='refresh', dry_run=False):
    """
    전 공정의 파라미터 타입을 현재 규칙으로 정리한다.

      mode='refresh'  저장된 타입을 지금 자동 분류 결과로 덮어쓴다.
                      화면에 타입이 그대로 보이지만, 규칙을 또 고치면
                      다시 어긋나므로 그때마다 실행해야 한다.

      mode='clear'    저장된 타입을 비운다(=자동).
                      읽을 때 param_types.resolve 가 매번 분류하므로
                      규칙을 고치면 저장된 값을 건드리지 않아도 따라간다.
                      ★ 이쪽이 근본 해결이다. 다만 손으로 지정한
                        타입이 있었다면 같이 사라진다.

      dry_run=True    바꾸지 않고 무엇이 바뀔지만 돌려준다.
    """
    ensure_tables()
    if mode not in ('refresh', 'clear'):
        raise ValueError("mode 는 'refresh' 또는 'clear' 여야 합니다")

    changed, detail = 0, []
    with _conn().cursor() as cur:
        cur.execute(f'''
            SELECT id, oper_id, param, COALESCE(param_type, '')
            FROM {T_PARAM} ORDER BY oper_id, param
        ''')
        rows = cur.fetchall()

        for rid, oper_id, param, saved in rows:
            new_t = '' if mode == 'clear' else pt.classify(param)
            if new_t == saved:
                continue
            if not dry_run:
                cur.execute(f'UPDATE {T_PARAM} SET param_type = %s WHERE id = %s',
                            [new_t, rid])
            changed += 1
            if len(detail) < 200:
                detail.append({
                    'oper_id': oper_id, 'param': param,
                    'before': saved or '(자동)', 'after': new_t or '(자동)',
                })

    return {'mode': mode, 'dry_run': dry_run, 'total': len(rows),
            'changed': changed, 'detail': detail}


def save_oper(d, user=''):
    """
    공정 1건 저장 (덮어쓰기).
    하위 목록은 통째로 교체한다 — 화면이 항상 전체를 보내므로
    부분 갱신보다 단순하고 어긋날 여지가 없다.
    """
    ensure_tables()
    oper_id = _up(d.get('oper_id'))
    if not oper_id:
        raise ValueError('OPER_ID 는 필수입니다')
    if not re.match(r'^[0-9A-Za-z_\-]+$', oper_id):
        raise ValueError('OPER_ID 에 사용할 수 없는 문자가 있습니다')

    # device — (LOT_CD, RECIPE_ID) 쌍. 같은 쌍이 두 번 오면 하나로.
    lots, seen = [], set()
    for it in d.get('lots', []):
        lot = _up(it.get('lot_cd'))
        if not lot:
            continue
        rec = _s(it.get('recipe_id'))
        key = (lot, rec)
        if key in seen:
            continue
        seen.add(key)
        lots.append((lot, rec, 'N' if _up(it.get('use_yn')) == 'N' else 'Y'))

    params, pseen = [], set()
    for p in d.get('params', []):
        name = _up(p.get('param'))
        if not name or name in pseen:
            continue
        pseen.add(name)
        # 빈 값은 그대로 저장한다 — 읽을 때 param_types.resolve 가
        # 자동 분류하므로, 사용자가 지정한 것과 자동 판정을 구분해 둔다.
        t = pt.normalize(p.get('param_type'))
        params.append((name, t,
                       'N' if _up(p.get('use_yn')) == 'N' else 'Y'))

    def _steps(key):
        """
        Defect · Response 공통 — 스텝 + 관리 파라미터 1개가 한 행.
        중복 판단 키는 (lot_cd, step_id, param) 이다.
        파라미터가 비어 있어도 스텝만 등록할 수 있게 허용한다
        (조회 스텝은 정해졌는데 관리 항목이 아직 안 정해진 경우).
        """
        out, seen = [], set()
        for it in d.get(key, []):
            lot  = _up(it.get('lot_cd'))
            step = _up(it.get('step_id'))
            prm  = _up(it.get('param'))
            if not step:
                continue
            k = (lot, step, prm)
            if k in seen:
                continue
            seen.add(k)
            out.append((lot, step, _s(it.get('step_desc')), prm,
                        'N' if _up(it.get('use_yn')) == 'N' else 'Y'))
        return out

    defects = _steps('defects')
    resps   = _steps('resps')

    with _conn().cursor() as cur:
        cur.execute(f'''
            INSERT INTO {T_OPER} (oper_id, oper_desc, fab, eq_model,
                   pre_oper_id, pre_oper_desc, pre_oper_param,
                   use_yn, updated_at, updated_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (oper_id) DO UPDATE SET
              oper_desc=EXCLUDED.oper_desc, fab=EXCLUDED.fab,
              eq_model=EXCLUDED.eq_model, pre_oper_id=EXCLUDED.pre_oper_id,
              pre_oper_desc=EXCLUDED.pre_oper_desc,
              pre_oper_param=EXCLUDED.pre_oper_param,
              use_yn=EXCLUDED.use_yn, updated_at=EXCLUDED.updated_at,
              updated_by=EXCLUDED.updated_by
        ''', [oper_id, _s(d.get('oper_desc')), _s(d.get('fab')),
              _up(d.get('eq_model')), _up(d.get('pre_oper_id')),
              _s(d.get('pre_oper_desc')), _up(d.get('pre_oper_param')),
              'N' if _up(d.get('use_yn')) == 'N' else 'Y',
              datetime.now(), _s(user)[:100]])

        for t in (T_LOT, T_PARAM, T_DEFECT, T_RESP):
            cur.execute(f'DELETE FROM {t} WHERE oper_id = %s', [oper_id])

        for lot, rec, use in lots:
            cur.execute(f'INSERT INTO {T_LOT} (oper_id, lot_cd, recipe_id, '
                        f'use_yn) VALUES (%s,%s,%s,%s)',
                        [oper_id, lot, rec, use])
        for name, t, use in params:
            cur.execute(f'INSERT INTO {T_PARAM} (oper_id, param, param_type, '
                        f'use_yn) VALUES (%s,%s,%s,%s)',
                        [oper_id, name, t, use])
        for tbl, rows_ in ((T_DEFECT, defects), (T_RESP, resps)):
            for lot, step, desc, prm, use in rows_:
                cur.execute(f'INSERT INTO {tbl} (oper_id, lot_cd, step_id, '
                            f'step_desc, param, use_yn) '
                            f'VALUES (%s,%s,%s,%s,%s,%s)',
                            [oper_id, lot, step, desc, prm, use])

    # ★ 연계 공정은 표가 따로라 별도 함수로 저장한다.
    #   화면이 links 를 안 보내면 건드리지 않는다 —
    #   payload 누락으로 등록이 통째로 날아가는 일을 막기 위해서다.
    n_link = 0
    if 'links' in d:
        n_link = save_links(oper_id, d.get('links') or [])

    return {'oper_id': oper_id, 'lots': len(lots),
            'params': len(params), 'defects': len(defects),
            'resps': len(resps), 'links': n_link}


def delete_oper(oper_id):
    """
    공정 1건 삭제.

    ★ 하위 표를 빠짐없이 지운다 — T_RESP 와 T_LINK 가 빠져 있어
      공정을 지워도 Response·연계 행이 남던 문제가 있었다.
      그 행들은 어느 화면에도 안 보이면서 조회에는 섞인다.
    """
    ensure_tables()
    ensure_link_table()
    with _conn().cursor() as cur:
        for t in (T_LOT, T_PARAM, T_DEFECT, T_RESP, T_LINK, T_OPER):
            cur.execute(f'DELETE FROM {t} WHERE oper_id = %s', [oper_id])
    return True


# ══════════════════════════════════════════════════════════
# 평면 DataFrame — 배치가 쓰던 형태 그대로
# ══════════════════════════════════════════════════════════
def build_config_df(include_unused=False):
    """
    oper ⋈ lot ⋈ param → 구닥스가 주던 평면 DataFrame.

      행 수 = (device 행 수) × (파라미터 수)
      LOT_CD 와 RECIPE_ID 는 같은 행에서 나오므로
      없는 짝(5E2 × E9레시피)이 생기지 않는다.

    include_unused=False 면 use_yn='N' 인 항목은 제외한다.
    """
    ensure_tables()
    cond = "" if include_unused else "WHERE COALESCE(use_yn,'Y') <> 'N'"

    with _conn().cursor() as cur:
        cur.execute(f'''
            SELECT oper_id, oper_desc, fab, eq_model,
                   pre_oper_id, pre_oper_desc, pre_oper_param
            FROM {T_OPER} {cond} ORDER BY oper_id
        ''')
        opers = cur.fetchall()
        if not opers:
            return pd.DataFrame(columns=FLAT_COLS)

        cur.execute(f'SELECT oper_id, lot_cd, recipe_id FROM {T_LOT} {cond}')
        lots = {}
        for o, lot, rec in cur.fetchall():
            lots.setdefault(o, []).append((lot or '', rec or ''))

        cur.execute(f'SELECT oper_id, param, param_type FROM {T_PARAM} {cond}')
        params = {}
        for o, p, t in cur.fetchall():
            params.setdefault(o, []).append((p, t or ''))

    rows = []
    for (oid, desc, fab, model, pre_id, pre_desc, pre_param) in opers:
        ls = lots.get(oid) or []
        ps = params.get(oid) or []
        if not ls or not ps:
            # device 나 파라미터가 없으면 조회 조건을 만들 수 없다
            continue
        for (lot, rec) in ls:
            for (pname, ptype) in ps:
                rows.append({
                    'FAB': fab or '', 'LOT_CD': lot,
                    'OPER_ID': oid, 'OPER_DESC': desc or '',
                    'EQ_MODEL': model or '', 'RECIPE_ID': rec,
                    'PARAM': pname, 'PRE_OPER_ID': pre_id or '',
                    'PRE_OPER_DESC': pre_desc or '',
                    'PRE_OPER_PARAM': pre_param or '',
                    # 지정 타입이 없으면 이름으로 자동 분류해서 내려보낸다
                    'PARAM_TYPE': pt.resolve(pname, ptype),
                })

    return pd.DataFrame(rows, columns=FLAT_COLS)


# ══════════════════════════════════════════════════════════
# Response · Defect 컬럼 이름 규칙
#   ★ 여기가 단일 소재지다. merge 코드·화면 미리보기·점검이 모두 이 함수를
#     통해야 이름이 갈리지 않는다. 규칙을 바꿀 일이 생겨도 한 곳만 고친다.
# ══════════════════════════════════════════════════════════
STEP_PREFIX = {'resp': 'RESP', 'def': 'DEF'}


def _slug_name(v):
    """컬럼명에 쓸 수 있게 정리 — 영숫자·_ 만 남기고 대문자로"""
    out = re.sub(r'[^0-9A-Za-z_]+', '_', str(v or '').strip().upper())
    return re.sub(r'_+', '_', out).strip('_')


def step_column(kind, step, param):
    """
    Response·Defect 측정값이 들어갈 최종 컬럼 이름.

      kind  'resp' | 'def'
      step  측정 스텝 명 (스텝 이름이 있으면 그것, 없으면 STEP_ID)
      param 관리 파라미터

    예) step_column('resp', 'ADI_CD', 'THK') -> 'RESP_ADI_CD_THK'

    ★ 스텝 명을 넣는 이유: 같은 파라미터를 여러 스텝에서 관리하면
      스텝 없이는 컬럼이 겹친다.
    ★ 접두어를 두는 이유: 이름만으로 resp/def 를 걸러낼 수 있어야 하고,
      공정 파라미터와 이름이 겹쳐도(THK 등) 충돌하지 않는다.
    """
    pre = STEP_PREFIX.get(str(kind).lower(), 'RESP')
    st, pm = _slug_name(step), _slug_name(param)
    if not pm:
        return ''                       # 파라미터가 없으면 컬럼도 없다
    return f'{pre}_{st}_{pm}' if st else f'{pre}_{pm}'


def _step_config_df(table, include_unused=False):
    """
    Defect · Response 계측 스텝 목록 (구조가 같다).
    컬럼: OPER_ID, LOT_CD, STEP_ID, STEP_DESC, PARAM
    """
    ensure_tables()
    cond = "" if include_unused else "WHERE COALESCE(use_yn,'Y') <> 'N'"
    with _conn().cursor() as cur:
        cur.execute(f'''
            SELECT oper_id, lot_cd, step_id, step_desc, param
            FROM {table} {cond} ORDER BY oper_id, lot_cd, step_id, param
        ''')
        rows = cur.fetchall()
    kind = 'def' if table == T_DEFECT else 'resp'
    out = []
    for r in rows:
        step_id, desc, prm = r[2] or '', r[3] or '', r[4] or ''
        out.append({
            'OPER_ID': r[0], 'LOT_CD': r[1] or '', 'STEP_ID': step_id,
            'STEP_DESC': desc, 'PARAM': prm,
            # 스텝 이름이 있으면 그것, 없으면 STEP_ID 로 컬럼을 만든다
            'COLUMN': step_column(kind, desc or step_id, prm),
        })
    return pd.DataFrame(out, columns=['OPER_ID', 'LOT_CD', 'STEP_ID',
                                      'STEP_DESC', 'PARAM', 'COLUMN'])


def build_defect_config_df(include_unused=False):
    """defect 계측 스텝 목록 — defect_service 가 조회 조건을 만들 때 쓴다"""
    return _step_config_df(T_DEFECT, include_unused)


def build_response_config_df(include_unused=False):
    """response 계측 스텝 목록 — response 조회 조건을 만들 때 쓴다"""
    return _step_config_df(T_RESP, include_unused)


def response_steps(oper_id, lot_cd=None):
    """
    한 공정의 response 스텝·파라미터.
    반환: [{'step_id','step_desc','params':[...]}]  — 스텝별로 묶어 준다
    """
    df = build_response_config_df()
    df = df[df['OPER_ID'] == str(oper_id).upper()]
    if lot_cd:
        df = df[(df['LOT_CD'] == str(lot_cd).upper()) | (df['LOT_CD'] == '')]
    out = {}
    for _, r in df.iterrows():
        k = r['STEP_ID']
        o = out.setdefault(k, {'step_id': k, 'step_desc': r['STEP_DESC'],
                               'params': [], 'columns': []})
        if r['PARAM'] and r['PARAM'] not in o['params']:
            o['params'].append(r['PARAM'])
            o['columns'].append(r['COLUMN'])
    return list(out.values())


def defect_steps(oper_id, lot_cd=None):
    """한 공정(선택적으로 한 device)의 defect 스텝 ID 목록"""
    ensure_tables()
    with _conn().cursor() as cur:
        if lot_cd:
            cur.execute(f'''
                SELECT DISTINCT step_id FROM {T_DEFECT}
                WHERE upper(oper_id) = %s AND upper(lot_cd) = %s
                  AND COALESCE(use_yn,'Y') <> 'N' AND COALESCE(step_id,'') <> ''
                ORDER BY step_id
            ''', [_up(oper_id), _up(lot_cd)])
        else:
            cur.execute(f'''
                SELECT DISTINCT step_id FROM {T_DEFECT}
                WHERE upper(oper_id) = %s
                  AND COALESCE(use_yn,'Y') <> 'N' AND COALESCE(step_id,'') <> ''
                ORDER BY step_id
            ''', [_up(oper_id)])
        return [r[0] for r in cur.fetchall()]


# ══════════════════════════════════════════════════════════
# 구닥스에서 가져오기 (초기 이관용)
# ══════════════════════════════════════════════════════════
def import_from_gooddocs(overwrite=False):
    """
    구닥스 기준정보를 읽어 테이블에 채운다.

      overwrite=False  이미 등록된 공정은 건너뛴다 (웹에서 고친 값 보호)
      overwrite=True   구닥스 값으로 덮어쓴다

    ★ 구닥스가 불안정하므로 실패해도 예외를 던지지 않고 사유를 돌려준다.
    ★ dropna 를 쓰지 않는다 — 한 칸 비었다고 행을 통째로 버리면
      파라미터가 조용히 누락된다.
    ★ 평면 구닥스에는 (LOT_CD, RECIPE_ID) 짝 정보가 없을 수 있다.
      같은 행에 둘 다 있으면 그 짝을 쓰고, 아니면 곱집합으로 채운 뒤
      화면에서 정리하게 한다.
    """
    ensure_tables()
    try:
        from . import analysis_service as svc
        df = svc.goodDocsGetData()          # dropna 없이 원본 그대로
    except Exception as e:
        return {'ok': False,
                'error': f'구닥스 조회 실패: {e.__class__.__name__}: {e}',
                'imported': 0, 'skipped': 0}

    if df is None or len(df) == 0:
        return {'ok': False, 'error': '구닥스에서 받은 데이터가 비어 있습니다',
                'imported': 0, 'skipped': 0}

    df = df.copy()
    df.columns = [str(c).upper().strip() for c in df.columns]
    if 'OPER_ID' not in df.columns or 'PARAM' not in df.columns:
        return {'ok': False,
                'error': f'OPER_ID/PARAM 컬럼이 없습니다 (받은 컬럼: '
                         f'{", ".join(list(df.columns)[:10])})',
                'imported': 0, 'skipped': 0}

    have = {o['oper_id'] for o in list_opers()}
    imported, skipped, detail = 0, 0, []

    for oid, sub in df.groupby('OPER_ID'):
        oid = _up(oid)
        if not oid:
            continue
        if oid in have and not overwrite:
            skipped += 1
            continue

        def first(name):
            if name not in sub.columns:
                return ''
            vals = [str(v).strip() for v in sub[name].dropna().tolist()
                    if str(v).strip()]
            return vals[0] if vals else ''

        def uniq(name):
            if name not in sub.columns:
                return []
            return sorted({str(v).strip() for v in sub[name].dropna().tolist()
                           if str(v).strip()})

        # (LOT_CD, RECIPE_ID) 짝 — 같은 행에 둘 다 있으면 그대로 쓴다
        pairs = set()
        if 'LOT_CD' in sub.columns:
            for _, r in sub.iterrows():
                lot = _up(r.get('LOT_CD'))
                rec = _s(r.get('RECIPE_ID')) if 'RECIPE_ID' in sub.columns else ''
                if lot:
                    pairs.add((lot, rec))
        if not pairs:
            for lot in uniq('LOT_CD') or ['']:
                for rec in uniq('RECIPE_ID') or ['']:
                    pairs.add((lot, rec))
        lots = [{'lot_cd': l, 'recipe_id': r, 'use_yn': 'Y'}
                for l, r in sorted(pairs) if l]

        ptypes = {}
        if 'PARAM_TYPE' in sub.columns:
            for _, r in sub.iterrows():
                p = _up(r.get('PARAM'))
                t = _up(r.get('PARAM_TYPE'))
                if p and t:
                    ptypes[p] = t
        # 구닥스에 PARAM_TYPE 이 있으면 쓰고, 없으면 이름으로 자동 분류
        params = [{'param': p,
                   'param_type': pt.resolve(p, ptypes.get(p, '')),
                   'use_yn': 'Y'}
                  for p in uniq('PARAM')]

        try:
            save_oper({
                'oper_id': oid,
                'oper_desc': first('OPER_DESC'),
                'fab': first('FAB'),
                'eq_model': first('EQ_MODEL'),
                'pre_oper_id': first('PRE_OPER_ID'),
                'pre_oper_desc': first('PRE_OPER_DESC'),
                'pre_oper_param': first('PRE_OPER_PARAM'),
                'lots': lots, 'params': params, 'defects': [],
            }, user='gooddocs-import')
            imported += 1
            detail.append(f'{oid}: device {len(lots)} / 파라미터 {len(params)}')
        except Exception as e:
            detail.append(f'{oid}: 실패 ({e})')

    return {'ok': True, 'imported': imported, 'skipped': skipped,
            'detail': detail}


# ══════════════════════════════════════════════════════════
# 검증 — 적재 데이터와 대조
# ══════════════════════════════════════════════════════════
def validate(oper_id=None):
    """
    등록한 기준정보가 실제 적재 데이터와 맞는지 확인한다.

    여기서 잡히는 것들이 그동안 디버깅에 시간을 쓰게 만든 것들이다.
      · 적재 테이블 자체가 없음
      · PARAM 이 테이블 컬럼에 없음 (이름 오타 / 실제 측정명 불일치)
      · 컬럼은 있는데 값이 전부 비어 있음
      · 등록한 LOT_CD 가 실제 데이터에 없음
      · device / 파라미터 미등록
    """
    ensure_tables()
    targets = [oper_id] if oper_id else [o['oper_id'] for o in list_opers()]
    out = []

    with _conn().cursor() as cur:
        for oid in targets:
            cfg = get_oper(oid)
            if not cfg:
                continue
            table = _an_table(oid)
            item = {'oper_id': oid, 'oper_desc': cfg['oper_desc'],
                    'table': table, 'issues': [], 'ok': True}

            def bad(msg):
                item['issues'].append({'level': 'bad', 'msg': msg})
                item['ok'] = False

            def warn(msg):
                item['issues'].append({'level': 'warn', 'msg': msg})

            lots_on = [l for l in cfg['lots'] if l['use_yn'] != 'N']
            pars_on = [p for p in cfg['params'] if p['use_yn'] != 'N']

            if not lots_on:
                bad('등록된 device(LOT_CD)가 없습니다 — 조회 조건을 만들 수 없습니다')
            if not pars_on:
                bad('등록된 파라미터가 없습니다')

            if not _exists(cur, table):
                bad('적재 테이블이 없습니다 — 아직 배치가 돌지 않았거나 '
                    'OPER_ID 표기가 다릅니다')
                out.append(item)
                continue

            cur.execute("""
                SELECT upper(column_name) FROM information_schema.columns
                WHERE table_name = %s
            """, [table])
            cols = {r[0] for r in cur.fetchall()}

            missing = [p['param'] for p in pars_on if p['param'] not in cols]
            if missing:
                bad(f'테이블에 없는 파라미터 {len(missing)}개: '
                    f'{", ".join(missing[:6])}{" ..." if len(missing) > 6 else ""}')

            present = [p['param'] for p in pars_on if p['param'] in cols]
            if present:
                sel = ", ".join(f'COUNT("{p}")' for p in present)
                cur.execute(f'SELECT {sel} FROM {table}')
                counts = cur.fetchone()
                empty = [p for p, n in zip(present, counts) if not n]
                if empty:
                    warn(f'값이 전부 비어 있는 파라미터 {len(empty)}개: '
                         f'{", ".join(empty[:6])}'
                         f'{" ..." if len(empty) > 6 else ""}')

            if 'LOT_CD' in cols and lots_on:
                cur.execute(f'SELECT DISTINCT "LOT_CD" FROM {table} '
                            f'WHERE "LOT_CD" IS NOT NULL')
                have_lots = {str(r[0]) for r in cur.fetchall()}
                gone = sorted({l['lot_cd'] for l in lots_on
                               if l['lot_cd'] not in have_lots})
                if gone:
                    warn(f'적재 데이터에 없는 LOT_CD: {", ".join(gone)}')

            # defect 스텝을 등록했는데 defect 컬럼이 없으면 적재가 안 된 것
            defs_on = [d for d in cfg['defects'] if d['use_yn'] != 'N']
            if defs_on:
                has_def_col = any(c.startswith('DEF_') for c in cols)
                if not has_def_col:
                    warn(f'defect 스텝 {len(defs_on)}개를 등록했지만 '
                         f'DEF_* 컬럼이 적재되지 않았습니다')

            out.append(item)
    return out


def suggest_params(oper_id):
    """
    적재 테이블의 숫자 컬럼을 후보로 제안한다.
    기준정보를 처음 만들 때 이름을 일일이 타이핑하지 않게 한다.
    """
    table = _an_table(oper_id)
    meta = {'ID', 'DATE', 'PROCESS_ID', 'RECIPE_ID', 'EQP_ID', 'EQP_CH_ID',
            'EQP_MODEL', 'OPERATION_ID', 'LOT_CD', 'LOT_ID', 'SUBSTRATE_ID',
            'WF_ID', 'IDLE', 'PRE_LAYER', 'PRE_EQP_ID', 'PRE_EQP_CH', 'QTY'}
    num = {'smallint', 'integer', 'bigint', 'decimal',
           'numeric', 'real', 'double precision'}
    with _conn().cursor() as cur:
        if not _exists(cur, table):
            return []
        cur.execute("""
            SELECT column_name, data_type FROM information_schema.columns
            WHERE table_name = %s ORDER BY ordinal_position
        """, [table])
        rows = cur.fetchall()
    return [c.upper() for c, d in rows
            if d.lower() in num and c.upper() not in meta
            and not c.upper().endswith(('_OFFSET', '_FORMULA'))]


def classify_params(params):
    """
    파라미터 이름 목록 → [{param, param_type}] (자동 분류 결과)
    셋업 페이지의 '타입 자동 분류' 버튼이 쓴다.
    """
    out = []
    for p in (params or []):
        name = _up(p)
        if name:
            out.append({'param': name, 'param_type': pt.classify(name)})
    return out


def suggest_lots(oper_id):
    """적재 테이블에 실제로 있는 LOT_CD — device 등록 시 후보로 쓴다"""
    table = _an_table(oper_id)
    with _conn().cursor() as cur:
        if not _exists(cur, table):
            return []
        cur.execute("""
            SELECT upper(column_name) FROM information_schema.columns
            WHERE table_name = %s
        """, [table])
        if 'LOT_CD' not in {r[0] for r in cur.fetchall()}:
            return []
        cur.execute(f'SELECT DISTINCT "LOT_CD" FROM {table} '
                    f'WHERE "LOT_CD" IS NOT NULL ORDER BY 1')
        return [str(r[0]) for r in cur.fetchall()]


# ══════════════════════════════════════════════════════════
# 연계 공정 (cmp_cfg_link)
#
#   본공정에 붙일 다른 공정의 값을 등록한다.
#   사전공정 chamber, 사전공정 측정값, Response, Defect 를 한 표에서 다룬다.
#
#   ★ LOT_CD(device) 단위다.
#     5E2 와 5E9 가 서로 다른 사전공정을 쓰는 일이 흔한데,
#     예전 구조는 사전공정이 공정 단위라 그걸 표현할 수 없었다.
#     LOT_CD 를 비우면 그 공정의 모든 device 에 적용된다.
#
#   ★ 조회처는 타입이 아니라 PARAM 이 가른다.
#       param 이 chamber → apc_sk_wafer_hst_r2r_all_*  (장비·챔버 이력)
#       그 외             → 타입에 맞는 측정 테이블
#     한 공정이 chamber 와 측정값을 동시에 가져올 수 있다.
# ══════════════════════════════════════════════════════════
T_LINK = 'cmp_cfg_link'

# 연계 타입 — '어느 측정 테이블을 볼지' 만 정한다
LINK_KINDS = ['SRC', 'REP', 'DEF']
LINK_KIND_LABEL = {'SRC': 'SRC (측정값)', 'REP': 'REP (Response)',
                   'DEF': 'DEF (Defect)'}

# 용도 — 정기 적재는 mon/both 만 조회한다
LINK_SCOPES = [('both', '둘 다'), ('mon', '모니터링'), ('ana', '분석 전용')]

# chamber 로 볼 PARAM 이름
CHAMBER_PARAMS = {'CHAMBER', 'CH', 'CHM', 'EQP_CH', 'MODULE', 'MODULE_ID',
                  '챔버'}

# 한 공정에 등록할 수 있는 연계 공정 수 (별칭 기준)
MAX_LINKS = 5


def is_chamber_param(param):
    """빈 param 도 chamber 로 본다 — 사전공정은 대개 챔버가 목적이다"""
    return _slug_name(param).upper() in CHAMBER_PARAMS or not _slug_name(param)


def link_column(kind, alias, param):
    """
    연계 측정값이 들어갈 컬럼 이름.
      link_column('SRC', 'M1_POLY', 'THK') -> 'SRC_M1_POLY_THK'

    ★ chamber 는 여기서 다루지 않는다 — chm_columns() 가 맡는다.
    """
    k = _up(kind) if _up(kind) in LINK_KINDS else 'SRC'
    a, p = _slug_name(alias), _slug_name(param)
    if is_chamber_param(param) or not p:
        return ''
    return f'{k}_{a}_{p}' if a else f'{k}_{p}'


def chm_columns(alias):
    """chamber 가 만드는 컬럼 두 개 — 장비와 챔버"""
    a = _slug_name(alias)
    return [f'{a}_EQP', f'{a}_CH'] if a else []


# 웨이퍼 번호 자릿수 — 조인 키 형태를 여기서 고정한다
WF_PAD = 2


def wafer_key(alias_lot_id, wf_id):
    """
    병합 키. wf_id 는 반드시 0 패딩된 문자열로 만든다.

    ★ Lake 가 '01' 로 주더라도 중간에 정수로 바뀌면 '1' 이 되어
      다른 데이터와 안 붙는다. 양쪽 모두 이 함수를 거쳐야 한다.
    """
    w = _s(wf_id)
    if w.isdigit():
        w = w.zfill(WF_PAD)
    return f'{_s(alias_lot_id)}.{w}'


def lots_of(oper_id):
    """
    기준정보에 등록된 LOT_CD 목록.

    ★ 조회 조건은 반드시 여기서 나와야 한다.
      적재 테이블에서 긁어오면 샘플 랏처럼 등록하지 않은 device 가
      섞여 엉뚱한 쿼리가 나간다.
    """
    ensure_tables()
    with _conn().cursor() as cur:
        cur.execute(f"""
            SELECT DISTINCT lot_cd FROM {T_LOT}
            WHERE oper_id = %s AND COALESCE(use_yn,'Y') <> 'N'
              AND lot_cd IS NOT NULL AND lot_cd <> ''
            ORDER BY 1
        """, [_up(oper_id)])
        return [r[0] for r in cur.fetchall()]


def ensure_link_table():
    with _conn().cursor() as cur:
        cur.execute(f'''
            CREATE TABLE IF NOT EXISTS {T_LINK} (
              id BIGSERIAL PRIMARY KEY,
              oper_id  VARCHAR(100),
              lot_cd   VARCHAR(50),
              kind     VARCHAR(10) DEFAULT 'SRC',
              alias    VARCHAR(100),
              link_id  VARCHAR(100),
              param    VARCHAR(200),
              scope    VARCHAR(10) DEFAULT 'both',
              seq      INTEGER DEFAULT 0,
              use_yn   VARCHAR(1) DEFAULT 'Y'
            )
        ''')
        cur.execute(f'CREATE INDEX IF NOT EXISTS ix_{T_LINK}_oper '
                    f'ON {T_LINK}(oper_id)')


def links_of_oper(oper_id):
    """편집 화면용 — 등록된 연계 행 그대로"""
    ensure_link_table()
    with _conn().cursor() as cur:
        cur.execute(f'''
            SELECT lot_cd, kind, alias, link_id, param, scope, use_yn
            FROM {T_LINK} WHERE oper_id = %s
            ORDER BY seq, lot_cd, alias, id
        ''', [_up(oper_id)])
        return [{'lot_cd': r[0] or '', 'kind': r[1] or 'SRC',
                 'alias': r[2] or '', 'link_id': r[3] or '',
                 'param': r[4] or '', 'scope': r[5] or 'both',
                 'use_yn': r[6] or 'Y'} for r in cur.fetchall()]


def links_for_query(oper_id, lot_cd=None, scope=None):
    """
    조회용 — 같은 연계 공정끼리 묶는다.

    ★ lot_cd 를 주면 그 device 에 해당하는 것만 돌려준다.
      LOT_CD 가 빈 행은 모든 device 에 적용된다.
    ★ 한 묶음이 chamber 와 측정값을 함께 가질 수 있다 —
      want_chm 과 params 를 따로 담는다.
    """
    ensure_link_table()
    where = ["oper_id = %s", "COALESCE(use_yn,'Y') <> 'N'"]
    args = [_up(oper_id)]

    if lot_cd:
        where.append("(COALESCE(lot_cd,'') = '' OR lot_cd = %s)")
        args.append(_up(lot_cd))
    if scope:
        where.append("(COALESCE(scope,'both') = 'both' OR scope = %s)")
        args.append(scope)

    with _conn().cursor() as cur:
        cur.execute(f'''
            SELECT lot_cd, kind, alias, link_id, param, scope
            FROM {T_LINK} WHERE {" AND ".join(where)}
            ORDER BY seq, alias, id
        ''', args)
        rows = cur.fetchall()

    out = {}
    for lc, kind, alias, link_id, prm, sc in rows:
        if not str(alias or '').strip() or not str(link_id or '').strip():
            continue
        k = (_slug_name(alias), _up(link_id), _up(kind) or 'SRC')
        g = out.setdefault(k, {
            'lot_cd': lc or '', 'kind': k[2], 'alias': _slug_name(alias),
            'link_id': k[1], 'params': [], 'want_chm': False,
            'columns': [], 'scope': sc or 'both',
        })
        if is_chamber_param(prm):
            g['want_chm'] = True
        else:
            p = _slug_name(prm)
            if p and p not in g['params']:
                g['params'].append(p)

    for g in out.values():
        cols = chm_columns(g['alias']) if g['want_chm'] else []
        cols += [link_column(g['kind'], g['alias'], p) for p in g['params']]
        g['columns'] = [c for c in cols if c]

    return list(out.values())


def all_link_columns(oper_id):
    """이 공정이 만드는 연계 컬럼 전체 — 화면 미리보기용"""
    cols = []
    for g in links_for_query(oper_id):
        for c in g['columns']:
            if c not in cols:
                cols.append(c)
    return cols


def save_links(oper_id, rows):
    """
    연계 공정 저장 — 기존 행을 지우고 새로 넣는다.

    ★ 별칭과 공정·스텝이 모두 있어야 유효한 행이다.
      빈 행은 화면에서 '추가' 만 누르고 안 채운 경우라 조용히 버린다.
    """
    ensure_link_table()
    oper_id = _up(oper_id)
    keep = []
    for i, r in enumerate(rows or []):
        alias = _slug_name(r.get('alias'))
        link_id = _up(r.get('link_id'))
        if not alias or not link_id:
            continue
        kind = _up(r.get('kind'))
        keep.append((
            oper_id, _up(r.get('lot_cd')),
            kind if kind in LINK_KINDS else 'SRC',
            alias, link_id, _up(r.get('param')),
            (r.get('scope') or 'both'), i,
            'N' if str(r.get('use_yn', 'Y')).upper() == 'N' else 'Y',
        ))

    with _conn().cursor() as cur:
        cur.execute(f'DELETE FROM {T_LINK} WHERE oper_id = %s', [oper_id])
        for t in keep:
            cur.execute(f'''
                INSERT INTO {T_LINK}
                  (oper_id, lot_cd, kind, alias, link_id, param,
                   scope, seq, use_yn)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ''', list(t))
    return len(keep)


def validate_links(oper_id):
    """
    등록 상태 점검 — 저장 전에 문제를 알려 준다.

    ★ 컬럼 이름이 겹치면 나중 값이 앞 값을 덮어써 조용히 사라진다.
      그게 가장 찾기 어려운 문제라 먼저 본다.
    """
    rows = links_of_oper(oper_id)
    issues = []

    seen = {}
    aliases = {}
    for i, r in enumerate(rows, start=1):
        if str(r.get('use_yn', 'Y')).upper() == 'N':
            continue
        alias, link_id = _slug_name(r['alias']), _up(r['link_id'])
        if not alias:
            issues.append(f'{i}행: 공정명(별칭)이 없습니다')
            continue
        if not link_id:
            issues.append(f'{i}행: 공정·스텝이 없습니다')
            continue

        aliases.setdefault(alias, set()).add(link_id)

        if is_chamber_param(r['param']):
            cols = chm_columns(alias)
        else:
            c = link_column(r['kind'], alias, r['param'])
            cols = [c] if c else []
            if not c:
                issues.append(f'{i}행: PARAM 이 없어 컬럼을 만들 수 없습니다')

        for c in cols:
            if c in seen and seen[c] != (alias, link_id):
                issues.append(f'{i}행: 컬럼 {c} 이(가) 겹칩니다 — '
                              f'별칭을 다르게 지으세요')
            seen[c] = (alias, link_id)

    for alias, ids in aliases.items():
        if len(ids) > 1:
            issues.append(f'별칭 {alias} 이(가) 여러 공정에 쓰였습니다: '
                          f'{", ".join(sorted(ids))}')

    if len(aliases) > MAX_LINKS:
        issues.append(f'연계 공정이 {len(aliases)}개입니다 — '
                      f'{MAX_LINKS}개 이하를 권합니다 (조회가 느려집니다)')

    return {'ok': not issues, 'issues': issues,
            'n_alias': len(aliases), 'columns': sorted(seen.keys())}
