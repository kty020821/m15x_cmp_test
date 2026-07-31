"""
equipment/db_inspect.py
════════════════════════════════════════════════════════════
적재된 분석 테이블 점검 도구 (읽기 전용)

  Django shell 에서 쓴다.

    python manage.py shell
    >>> from equipment import db_inspect as di
    >>> di.tables()              # 어떤 공정이 얼마나 적재됐나
    >>> di.describe('공정ID')     # 컬럼 · 타입 · 값이 있는 비율
    >>> di.params('공정ID')       # PARAMETER 목록에 뜨는/안 뜨는 이유
    >>> di.sample('공정ID')       # 실제 행 몇 개
    >>> di.diff('공정A', '공정B')  # 두 공정의 컬럼 차이

  ※ 조회만 한다. 데이터를 바꾸지 않는다.
════════════════════════════════════════════════════════════
"""

import re
from django.db import connections


# 측정값이 아닌 메타 컬럼 (views_analysis.META_COLS 와 같은 기준)
META_COLS = {
    'ID', 'DATE', 'PROCESS_ID', 'RECIPE_ID', 'EQP_ID', 'EQP_CH_ID',
    'EQP_MODEL', 'OPERATION_ID', 'LOT_CD', 'LOT_ID', 'SUBSTRATE_ID',
    'WF_ID', 'IDLE', 'PRE_LAYER', 'PRE_EQP_ID', 'PRE_EQP_CH', 'QTY',
}

# 화면이 PARAMETER 후보로 인정하는 타입
NUMERIC_TYPES = {
    'smallint', 'integer', 'bigint',
    'decimal', 'numeric', 'real', 'double precision',
}


def _table(oper_id):
    return f"cmp_analysis_{re.sub(r'[^0-9A-Za-z_]', '_', str(oper_id)).lower()}"


def _cur():
    return connections['analysis_db'].cursor()


def _cols(cur, table):
    cur.execute("""
        SELECT column_name, data_type FROM information_schema.columns
        WHERE table_name = %s ORDER BY ordinal_position
    """, [table])
    return cur.fetchall()


def _exists(cur, table):
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", [table])
    return bool(cur.fetchone()[0])


# ══════════════════════════════════════════════════════════
def tables():
    """적재된 공정 테이블 전체 — 행수 · LOT_CD · 최신 날짜"""
    with _cur() as cur:
        cur.execute(
            "SELECT tablename FROM pg_tables WHERE tablename LIKE %s "
            "ORDER BY tablename", ['cmp_analysis_%'])
        names = [r[0] for r in cur.fetchall()]

        if not names:
            print('적재된 테이블이 없습니다.')
            return []

        print(f'{"테이블":<34}{"행수":>10}  {"LOT_CD":<14}{"최근 DATE":<20}컬럼')
        print('-' * 96)

        out = []
        for t in names:
            cur.execute(f'SELECT COUNT(*) FROM {t}')
            n = cur.fetchone()[0]

            lots, last = [], None
            cols = _cols(cur, t)
            names_up = {c.upper() for c, _ in cols}

            if 'LOT_CD' in names_up:
                cur.execute(f'SELECT DISTINCT "LOT_CD" FROM {t} '
                            f'WHERE "LOT_CD" IS NOT NULL ORDER BY 1')
                lots = [str(r[0]) for r in cur.fetchall()]
            if 'DATE' in names_up:
                cur.execute(f'SELECT MAX("DATE") FROM {t}')
                last = cur.fetchone()[0]

            print(f'{t:<34}{n:>10,}  {",".join(lots):<14}'
                  f'{str(last)[:19]:<20}{len(cols)}')
            out.append({'table': t, 'rows': n, 'lots': lots,
                        'last': last, 'cols': len(cols)})
        return out


def describe(oper_id, only_empty=False):
    """
    컬럼별 타입과 '값이 있는 행 비율'.

      only_empty=True 면 값이 하나도 없는 컬럼만 본다.
      화면에서 조회했는데 점이 안 찍히는 파라미터를 찾을 때 쓴다.
    """
    table = _table(oper_id)
    with _cur() as cur:
        if not _exists(cur, table):
            print(f'{table} 없음 — 적재되지 않은 공정입니다.')
            return []

        cur.execute(f'SELECT COUNT(*) FROM {table}')
        total = cur.fetchone()[0]
        cols  = _cols(cur, table)

        if total == 0:
            print(f'{table} : 0행 (테이블은 있으나 데이터가 없음)')
            return []

        # 컬럼별 non-null 개수를 한 번에
        sel = ", ".join(f'COUNT("{c}")' for c, _ in cols)
        cur.execute(f'SELECT {sel} FROM {table}')
        counts = cur.fetchone()

        print(f'{table} : {total:,}행 / 컬럼 {len(cols)}개\n')
        print(f'{"컬럼":<34}{"타입":<20}{"값있음":>10}{"비율":>8}')
        print('-' * 74)

        out = []
        for (name, dtype), n in zip(cols, counts):
            pct = n / total * 100
            if only_empty and n:
                continue
            mark = '  ←비어있음' if n == 0 else ''
            print(f'{name:<34}{dtype:<20}{n:>10,}{pct:>7.1f}%{mark}')
            out.append({'col': name, 'type': dtype, 'n': n, 'pct': round(pct, 1)})
        return out


def params(oper_id):
    """
    PARAMETER 드롭박스 기준으로 컬럼을 분류한다.

      목록에 뜸        : 숫자 타입 + 메타 아님
      값 없음          : 목록엔 뜨지만 전부 NULL → 조회해도 점이 안 찍힘
      타입때문에 빠짐  : 텍스트로 저장돼 목록에서 제외됨 (500 의 옛 원인)
    """
    table = _table(oper_id)
    with _cur() as cur:
        if not _exists(cur, table):
            print(f'{table} 없음 — 적재되지 않은 공정입니다.')
            return {}

        cur.execute(f'SELECT COUNT(*) FROM {table}')
        total = cur.fetchone()[0] or 1
        cols  = _cols(cur, table)

        cand = [(c, d) for c, d in cols if c.upper() not in META_COLS]
        if not cand:
            print('측정값 컬럼이 없습니다.')
            return {}

        sel = ", ".join(f'COUNT("{c}")' for c, _ in cand)
        cur.execute(f'SELECT {sel} FROM {table}')
        counts = cur.fetchone()

        shown, empty, wrong_type = [], [], []
        for (name, dtype), n in zip(cand, counts):
            if dtype.lower() not in NUMERIC_TYPES:
                wrong_type.append((name, dtype, n))
            elif n == 0:
                empty.append(name)
            else:
                shown.append((name, n))

        print(f'{table} : {total:,}행\n')
        print(f'[목록에 뜨고 값도 있음] {len(shown)}개')
        for name, n in shown[:20]:
            print(f'  {name:<34}{n:>9,}행 ({n/total*100:.0f}%)')
        if len(shown) > 20:
            print(f'  ... 외 {len(shown) - 20}개')

        if empty:
            print(f'\n[목록엔 뜨지만 값이 전부 없음] {len(empty)}개 '
                  f'— 조회해도 점이 안 찍힙니다')
            print(f'  {", ".join(empty[:15])}'
                  f'{" ..." if len(empty) > 15 else ""}')

        if wrong_type:
            print(f'\n[타입 때문에 목록에서 빠짐] {len(wrong_type)}개 '
                  f'— 텍스트로 저장된 측정값')
            for name, dtype, n in wrong_type[:15]:
                print(f'  {name:<34}{dtype:<20}{n:>9,}행')
            print('  → analysis_service.repair_numeric_columns() 로 복구 가능')

        return {'shown': shown, 'empty': empty, 'wrong_type': wrong_type}


def sample(oper_id, n=5, cols=None):
    """실제 행 몇 개를 본다. cols 를 주면 그 컬럼만"""
    table = _table(oper_id)
    with _cur() as cur:
        if not _exists(cur, table):
            print(f'{table} 없음')
            return []

        if cols:
            sel = ", ".join(f'"{c}"' for c in cols)
            head = list(cols)
        else:
            base = ['DATE', 'LOT_ID', 'WF_ID', 'EQP_ID', 'EQP_CH_ID',
                    'IDLE', 'PRE_EQP_ID', 'PRE_EQP_CH', 'PRE_LAYER']
            have = {c.upper() for c, _ in _cols(cur, table)}
            head = [c for c in base if c in have]
            sel  = ", ".join(f'"{c}"' for c in head)

        cur.execute(f'SELECT {sel} FROM {table} ORDER BY id DESC LIMIT {int(n)}')
        rows = cur.fetchall()

        print(" | ".join(head))
        print('-' * 100)
        for r in rows:
            print(" | ".join(str(v)[:18] for v in r))
        return rows


def diff(oper_a, oper_b):
    """두 공정 테이블의 컬럼 차이 — 한쪽만 조회가 되는 이유를 찾을 때"""
    ta, tb = _table(oper_a), _table(oper_b)
    with _cur() as cur:
        for t in (ta, tb):
            if not _exists(cur, t):
                print(f'{t} 없음')
                return
        ca = {c.upper(): d for c, d in _cols(cur, ta)}
        cb = {c.upper(): d for c, d in _cols(cur, tb)}

    only_a = sorted(set(ca) - set(cb))
    only_b = sorted(set(cb) - set(ca))
    diff_t = sorted(c for c in set(ca) & set(cb) if ca[c] != cb[c])

    print(f'{oper_a}: 컬럼 {len(ca)}개 / {oper_b}: 컬럼 {len(cb)}개\n')
    if only_a:
        print(f'[{oper_a} 에만] {", ".join(only_a[:20])}'
              f'{" ..." if len(only_a) > 20 else ""}')
    if only_b:
        print(f'[{oper_b} 에만] {", ".join(only_b[:20])}'
              f'{" ..." if len(only_b) > 20 else ""}')
    if diff_t:
        print('\n[타입이 다름]')
        for c in diff_t:
            print(f'  {c:<34}{oper_a}={ca[c]:<20}{oper_b}={cb[c]}')
    if not (only_a or only_b or diff_t):
        print('컬럼 구성이 같습니다.')
