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
              use_yn  VARCHAR(1) DEFAULT 'Y'
            )
        ''')
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
                   (SELECT COUNT(*) FROM {T_DEFECT} d WHERE d.oper_id = o.oper_id)
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
        'n_lot': r[6], 'n_param': r[7], 'n_defect': r[8],
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

        cur.execute(f'''
            SELECT lot_cd, step_id, step_desc, use_yn FROM {T_DEFECT}
            WHERE oper_id = %s ORDER BY lot_cd, step_id
        ''', [oper_id])
        defects = [{'lot_cd': r[0] or '', 'step_id': r[1] or '',
                    'step_desc': r[2] or '', 'use_yn': r[3] or 'Y'}
                   for r in cur.fetchall()]

    return {
        'oper_id': row[0], 'oper_desc': row[1] or '', 'fab': row[2] or '',
        'eq_model': row[3] or '', 'pre_oper_id': row[4] or '',
        'pre_oper_desc': row[5] or '', 'pre_oper_param': row[6] or '',
        'use_yn': row[7] or 'Y',
        'lots': lots, 'params': params, 'defects': defects,
    }


# ══════════════════════════════════════════════════════════
# 저장 / 삭제
# ══════════════════════════════════════════════════════════
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

    defects, dseen = [], set()
    for it in d.get('defects', []):
        lot  = _up(it.get('lot_cd'))
        step = _up(it.get('step_id'))
        if not step:
            continue
        key = (lot, step)
        if key in dseen:
            continue
        dseen.add(key)
        defects.append((lot, step, _s(it.get('step_desc')),
                        'N' if _up(it.get('use_yn')) == 'N' else 'Y'))

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

        for t in (T_LOT, T_PARAM, T_DEFECT):
            cur.execute(f'DELETE FROM {t} WHERE oper_id = %s', [oper_id])

        for lot, rec, use in lots:
            cur.execute(f'INSERT INTO {T_LOT} (oper_id, lot_cd, recipe_id, '
                        f'use_yn) VALUES (%s,%s,%s,%s)',
                        [oper_id, lot, rec, use])
        for name, t, use in params:
            cur.execute(f'INSERT INTO {T_PARAM} (oper_id, param, param_type, '
                        f'use_yn) VALUES (%s,%s,%s,%s)',
                        [oper_id, name, t, use])
        for lot, step, desc, use in defects:
            cur.execute(f'INSERT INTO {T_DEFECT} (oper_id, lot_cd, step_id, '
                        f'step_desc, use_yn) VALUES (%s,%s,%s,%s,%s)',
                        [oper_id, lot, step, desc, use])

    return {'oper_id': oper_id, 'lots': len(lots),
            'params': len(params), 'defects': len(defects)}


def delete_oper(oper_id):
    ensure_tables()
    with _conn().cursor() as cur:
        for t in (T_LOT, T_PARAM, T_DEFECT, T_OPER):
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


def build_defect_config_df(include_unused=False):
    """
    defect 계측 스텝 목록 — defect_service 가 조회 조건을 만들 때 쓴다.
    컬럼: OPER_ID, LOT_CD, STEP_ID, STEP_DESC
    """
    ensure_tables()
    cond = "" if include_unused else "WHERE COALESCE(use_yn,'Y') <> 'N'"
    with _conn().cursor() as cur:
        cur.execute(f'''
            SELECT oper_id, lot_cd, step_id, step_desc
            FROM {T_DEFECT} {cond} ORDER BY oper_id, lot_cd, step_id
        ''')
        rows = cur.fetchall()
    return pd.DataFrame(
        [{'OPER_ID': r[0], 'LOT_CD': r[1] or '', 'STEP_ID': r[2] or '',
          'STEP_DESC': r[3] or ''} for r in rows],
        columns=['OPER_ID', 'LOT_CD', 'STEP_ID', 'STEP_DESC'])


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
