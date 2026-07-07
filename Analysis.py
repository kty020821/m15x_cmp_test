"""
분석 데이터 저장 (파일럿) - 최종 wide df를 PostgreSQL에 저장
"""

import re
import pandas as pd
from sqlalchemy import create_engine
from django.conf import settings
from django.db import connections


# ── 컬럼 타입 분류 ────────────────────────────────────────────
TEXT_COLS = {
    'PROCESS_ID', 'RECIPE_ID', 'EQP_ID', 'EQP_MODEL', 'OPERATION_ID',
    'LOT_CD', 'LOT_ID', 'SUBSTRATE_ID', 'WF_ID', 'IDLE',
}
TIME_COLS = {'DATE'}


def _pg_type(col: str) -> str:
    cu = col.upper()
    if cu in TIME_COLS:
        return 'TIMESTAMP'
    if cu in TEXT_COLS:
        return 'VARCHAR(100)'
    return 'DOUBLE PRECISION'


def _safe_table_name(oper_id: str) -> str:
    name = re.sub(r'[^0-9A-Za-z_]', '_', str(oper_id)).lower()
    return f"cmp_analysis_{name}"


def _get_pg_engine():
    """settings의 analysis_db 정보로 SQLAlchemy 엔진 생성"""
    db = settings.DATABASES['analysis_db']
    url = (f"postgresql+psycopg2://{db['USER']}:{db['PASSWORD']}"
           f"@{db['HOST']}:{db['PORT']}/{db['NAME']}")
    return create_engine(url)


def create_analysis_table(df: pd.DataFrame, oper_id: str) -> str:
    """df 컬럼 구조에 맞춰 테이블 생성 (Django 커넥션으로 DDL 실행)"""
    table = _safe_table_name(oper_id)
    django_conn = connections['analysis_db']

    col_defs = ["id BIGSERIAL PRIMARY KEY"]
    for col in df.columns:
        col_defs.append(f'"{col.upper()}" {_pg_type(col)}')
    create_sql = f'CREATE TABLE IF NOT EXISTS {table} (\n  ' + ",\n  ".join(col_defs) + "\n);"

    index_sql = [
        f'CREATE INDEX IF NOT EXISTS idx_{table}_lot  ON {table} ("LOT_CD");',
        f'CREATE INDEX IF NOT EXISTS idx_{table}_date ON {table} ("DATE");',
        f'CREATE INDEX IF NOT EXISTS idx_{table}_eqp  ON {table} ("EQP_ID");',
    ]

    with django_conn.cursor() as cur:
        cur.execute(create_sql)
        for sql in index_sql:
            try:
                cur.execute(sql)
            except Exception as e:
                print(f"  인덱스 스킵: {e}")

    print(f"[테이블 준비] {table}  (컬럼 {len(df.columns)}개)")
    return table


def save_analysis_df(df: pd.DataFrame, oper_id: str):
    """최종 wide df를 PG에 저장 (LOT_CD 단위 삭제 후 append)"""
    if df.empty:
        print("[저장 스킵] df 비어있음")
        return

    df = df.copy()
    df.columns = df.columns.str.upper()

    table = create_analysis_table(df, oper_id)

    # 기존 데이터 삭제 (Django 커넥션)
    django_conn = connections['analysis_db']
    lot_cds = df['LOT_CD'].dropna().unique().tolist()
    with django_conn.cursor() as cur:
        for lc in lot_cds:
            cur.execute(f'DELETE FROM {table} WHERE "LOT_CD" = %s', [lc])

    # 저장 (SQLAlchemy 엔진)
    engine = _get_pg_engine()
    try:
        df.to_sql(table, engine, if_exists="append", index=False, chunksize=1000)
    finally:
        engine.dispose()

    print(f"[저장 완료] {table}  {len(df):,}행  (lot_cd: {lot_cds})")


# ============================================================
# 수동 테스트
# ============================================================
if __name__ == "__main__":
    import os, django
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    django.setup()

    fake = pd.DataFrame([
        {'DATE': '2026-06-15 08:00:00', 'PROCESS_ID': 'PRC1', 'RECIPE_ID': 'R01',
         'EQP_ID': '5CMP1E21', 'EQP_MODEL': 'OPTA', 'OPERATION_ID': 'OP100',
         'LOT_CD': '5E2', 'LOT_ID': 'L001', 'SUBSTRATE_ID': 'S01', 'WF_ID': '01',
         'IDLE': 'idle1', 'P1_TIME': 55.2, 'P2_TIME': 60.1, 'AMAT_POST_OCD_AVG': 1203.5},
        {'DATE': '2026-06-15 08:20:00', 'PROCESS_ID': 'PRC1', 'RECIPE_ID': 'R01',
         'EQP_ID': '5CMP1E21', 'EQP_MODEL': 'OPTA', 'OPERATION_ID': 'OP100',
         'LOT_CD': '5E2', 'LOT_ID': 'L001', 'SUBSTRATE_ID': 'S02', 'WF_ID': '02',
         'IDLE': '', 'P1_TIME': 54.9, 'P2_TIME': 59.8, 'AMAT_POST_OCD_AVG': 1198.2},
        {'DATE': '2026-06-15 09:00:00', 'PROCESS_ID': 'PRC1', 'RECIPE_ID': 'R01',
         'EQP_ID': '5CMP1E22', 'EQP_MODEL': 'OPTA', 'OPERATION_ID': 'OP100',
         'LOT_CD': '5E9', 'LOT_ID': 'L050', 'SUBSTRATE_ID': 'S01', 'WF_ID': '01',
         'IDLE': 'PASS1 HDP CMP', 'P1_TIME': 56.0, 'P2_TIME': 61.2, 'AMAT_POST_OCD_AVG': 1210.7},
    ])
    fake['DATE'] = pd.to_datetime(fake['DATE'])

    save_analysis_df(fake, oper_id='OP100')
