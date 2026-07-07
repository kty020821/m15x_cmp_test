"""
분석 데이터 저장 모듈 (파일럿)
- 3개 DB(APC/SRC/MES) 합친 최종 wide df를 PostgreSQL에 저장
- 테이블은 공정별 1개 (컬럼은 df 보고 자동 판단)
- df 컬럼 예:
    DATE, PROCESS_ID, RECIPE_ID, EQP_ID, EQP_MODEL, OPERATION_ID,
    LOT_CD, LOT_ID, SUBSTRATE_ID, WF_ID, IDLE,
    S7_P3_04_TIME, AMAT_POST_OCD_AVG, P1_TIME, ... (측정값들)
"""

import pandas as pd
from django.db import connections


# ── 컬럼 타입 분류 ────────────────────────────────────────────
# 텍스트로 저장할 컬럼 (나머지는 전부 측정값 = DOUBLE)
TEXT_COLS = {
    'PROCESS_ID', 'RECIPE_ID', 'EQP_ID', 'EQP_MODEL', 'OPERATION_ID',
    'LOT_CD', 'LOT_ID', 'SUBSTRATE_ID', 'WF_ID', 'IDLE',
}
TIME_COLS = {'DATE'}   # TIMESTAMP로 저장


def _pg_type(col: str) -> str:
    """컬럼명 → PostgreSQL 타입"""
    cu = col.upper()
    if cu in TIME_COLS:
        return 'TIMESTAMP'
    if cu in TEXT_COLS:
        return 'VARCHAR(100)'
    return 'DOUBLE PRECISION'   # 나머지는 측정값


def _safe_table_name(oper_id: str) -> str:
    """oper_id → 안전한 테이블명 (영문/숫자/언더스코어만)"""
    import re
    name = re.sub(r'[^0-9A-Za-z_]', '_', str(oper_id)).lower()
    return f"cmp_analysis_{name}"


def create_analysis_table(df: pd.DataFrame, oper_id: str):
    """df 컬럼 구조에 맞춰 테이블 생성 (이미 있으면 스킵)"""
    table = _safe_table_name(oper_id)
    conn  = connections['analysis_db']

    # 컬럼 정의 생성
    col_defs = ["id BIGSERIAL PRIMARY KEY"]
    for col in df.columns:
        col_defs.append(f'"{col.upper()}" {_pg_type(col)}')

    create_sql = f'CREATE TABLE IF NOT EXISTS {table} (\n  ' + ",\n  ".join(col_defs) + "\n);"

    # 조회 성능용 인덱스 (자주 필터하는 컬럼)
    index_sql = [
        f'CREATE INDEX IF NOT EXISTS idx_{table}_lot   ON {table} ("LOT_CD");',
        f'CREATE INDEX IF NOT EXISTS idx_{table}_date  ON {table} ("DATE");',
        f'CREATE INDEX IF NOT EXISTS idx_{table}_eqp   ON {table} ("EQP_ID");',
    ]

    with conn.cursor() as cur:
        cur.execute(create_sql)
        for sql in index_sql:
            try:
                cur.execute(sql)
            except Exception as e:
                print(f"  인덱스 스킵({e})")

    print(f"[테이블 준비] {table}  (컬럼 {len(df.columns)}개)")
    return table


def save_analysis_df(df: pd.DataFrame, oper_id: str):
    """
    최종 wide df를 PG에 저장.
    - 테이블 없으면 생성
    - LOT_CD 단위로 기존 데이터 삭제 후 append (재실행 시 중복 방지)
    """
    if df.empty:
        print("[저장 스킵] df 비어있음")
        return

    df = df.copy()
    df.columns = df.columns.str.upper()   # 컬럼명 대문자 통일

    table = create_analysis_table(df, oper_id)
    conn  = connections['analysis_db']

    # 이 df에 들어있는 lot_cd들 → 기존 데이터 삭제 후 재적재
    lot_cds = df['LOT_CD'].dropna().unique().tolist()
    with conn.cursor() as cur:
        for lc in lot_cds:
            cur.execute(f'DELETE FROM {table} WHERE "LOT_CD" = %s', [lc])

    df.to_sql(table, conn.connection,
              if_exists="append", index=False, chunksize=1000)

    print(f"[저장 완료] {table}  {len(df):,}행  (lot_cd: {lot_cds})")


# ============================================================
# 수동 테스트 (데이터 있다고 가정)
# ============================================================
if __name__ == "__main__":
    import os, django
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    django.setup()

    # ── 가짜 데이터 (실제로는 네 3-DB 합친 최종 df 넣기) ──
    fake = pd.DataFrame([
        {
            'DATE': '2026-06-15 08:00:00', 'PROCESS_ID': 'PRC1', 'RECIPE_ID': 'R01',
            'EQP_ID': '5CMP1E21', 'EQP_MODEL': 'OPTA', 'OPERATION_ID': 'OP100',
            'LOT_CD': '5E2', 'LOT_ID': 'L001', 'SUBSTRATE_ID': 'S01', 'WF_ID': '01',
            'IDLE': 'idle1',
            'P1_TIME': 55.2, 'P2_TIME': 60.1, 'P3_TIME': 48.7,
            'AMAT_POST_OCD_AVG': 1203.5, 'S7_P3_04_TIME': 12.3,
        },
        {
            'DATE': '2026-06-15 08:20:00', 'PROCESS_ID': 'PRC1', 'RECIPE_ID': 'R01',
            'EQP_ID': '5CMP1E21', 'EQP_MODEL': 'OPTA', 'OPERATION_ID': 'OP100',
            'LOT_CD': '5E2', 'LOT_ID': 'L001', 'SUBSTRATE_ID': 'S02', 'WF_ID': '02',
            'IDLE': '',
            'P1_TIME': 54.9, 'P2_TIME': 59.8, 'P3_TIME': 49.1,
            'AMAT_POST_OCD_AVG': 1198.2, 'S7_P3_04_TIME': 12.1,
        },
        {
            'DATE': '2026-06-15 09:00:00', 'PROCESS_ID': 'PRC1', 'RECIPE_ID': 'R01',
            'EQP_ID': '5CMP1E22', 'EQP_MODEL': 'OPTA', 'OPERATION_ID': 'OP100',
            'LOT_CD': '5E9', 'LOT_ID': 'L050', 'SUBSTRATE_ID': 'S01', 'WF_ID': '01',
            'IDLE': 'PASS1 HDP CMP',   # layer change 정보
            'P1_TIME': 56.0, 'P2_TIME': 61.2, 'P3_TIME': 47.9,
            'AMAT_POST_OCD_AVG': 1210.7, 'S7_P3_04_TIME': 12.8,
        },
    ])
    fake['DATE'] = pd.to_datetime(fake['DATE'])

    save_analysis_df(fake, oper_id='OP100')
