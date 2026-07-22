"""
analysis_service.py 의 save_analysis_df 를 아래 두 함수로 통째로 교체.
(기존 _pg_type / TEXT_COLS 는 지워도 되고 남겨둬도 무방)
"""


def _pg_type_from_series(s):
    """
    실제 데이터로 PG 타입 판정.
    하드코딩 목록(TEXT_COLS)은 새 컬럼이 생길 때마다 누락되므로 값으로 판단한다.
    """
    if pd.api.types.is_datetime64_any_dtype(s):
        return 'TIMESTAMP'
    if pd.api.types.is_numeric_dtype(s):
        return 'DOUBLE PRECISION'

    nonnull = s.dropna()
    if len(nonnull) == 0:
        return 'VARCHAR(200)'
    # 전부 숫자로 변환되면 숫자 컬럼, 하나라도 실패하면 텍스트
    if pd.to_numeric(nonnull, errors='coerce').notna().all():
        return 'DOUBLE PRECISION'
    return 'VARCHAR(200)'


def save_analysis_df(df, oper_id):
    """
    최종 wide df → PostgreSQL 저장 (LOT_CD 단위 삭제 후 재적재)

    [주의] CREATE TABLE IF NOT EXISTS 는 기존 테이블 구조를 바꾸지 않는다.
           컬럼이나 타입이 바뀌었으면 먼저 DROP TABLE 할 것.
    """
    if df is None or df.empty:
        print(f"[{oper_id}] 저장 스킵 (빈 df)")
        return

    df = df.copy()
    df.columns = df.columns.str.upper()

    # 중복 컬럼 방어 (앞의 것만 유지)
    df = df.loc[:, ~df.columns.duplicated()]

    table = _table_name(oper_id)
    conn  = connections['analysis_db']

    # ── 컬럼 타입 판정 + 값 캐스팅 ──────────────────────
    #    Lake 에서 숫자가 문자열로 넘어오는 경우가 있어 값도 함께 변환한다
    col_types = {}
    for c in df.columns:
        if c in TIME_COLS:
            t = 'TIMESTAMP'
        else:
            t = _pg_type_from_series(df[c])
        col_types[c] = t

        if t == 'DOUBLE PRECISION':
            df[c] = pd.to_numeric(df[c], errors='coerce')
        elif t == 'TIMESTAMP':
            df[c] = pd.to_datetime(df[c], errors='coerce')

    # ── 테이블 생성 ────────────────────────────────────
    col_defs = ["id BIGSERIAL PRIMARY KEY"] + \
               [f'"{c}" {col_types[c]}' for c in df.columns]
    with conn.cursor() as cur:
        cur.execute(f"CREATE TABLE IF NOT EXISTS {table} (\n  "
                    + ",\n  ".join(col_defs) + "\n)")
        cur.execute(f'CREATE INDEX IF NOT EXISTS idx_{table}_lot  ON {table} ("LOT_CD")')
        cur.execute(f'CREATE INDEX IF NOT EXISTS idx_{table}_date ON {table} ("DATE")')

    # ── 적재 ───────────────────────────────────────────
    df = df.astype(object).where(pd.notnull(df), None)

    cols    = list(df.columns)
    col_str = ", ".join(f'"{c}"' for c in cols)
    data    = [tuple(r) for r in df.itertuples(index=False, name=None)]
    lot_cds = df['LOT_CD'].dropna().unique().tolist() if 'LOT_CD' in df.columns else []

    with conn.cursor() as cur:
        for lc in lot_cds:
            cur.execute(f'DELETE FROM {table} WHERE "LOT_CD" = %s', [lc])
        execute_values(cur.cursor, f'INSERT INTO {table} ({col_str}) VALUES %s',
                       data, page_size=1000)

    print(f"[{oper_id}] 저장 완료 {len(df):,}행 (lot_cd: {lot_cds})")
