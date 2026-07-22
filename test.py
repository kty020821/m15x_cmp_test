with conn.cursor() as cur:
        cur.execute(f"CREATE TABLE IF NOT EXISTS {table} (...)")
        cur.execute(f'CREATE INDEX IF NOT EXISTS idx_{table}_lot  ...')
        cur.execute(f'CREATE INDEX IF NOT EXISTS idx_{table}_date ...')

        # ↓ 여기부터 추가
        # CREATE TABLE IF NOT EXISTS 는 기존 테이블 구조를 바꾸지 않으므로
        # 값 형식이 달라졌으면 컬럼 타입을 넓혀준다
        cur.execute("""
            SELECT upper(column_name), lower(data_type)
            FROM information_schema.columns WHERE table_name = %s
        """, [table])
        existing = {r[0]: r[1] for r in cur.fetchall()}

        for c in df.columns:
            cur_t = existing.get(c)
            if cur_t is None:
                cur.execute(f'ALTER TABLE {table} ADD COLUMN "{c}" {col_types[c]}')
                print(f"   + 컬럼 추가: {c} {col_types[c]}")
            elif col_types[c].startswith('VARCHAR') and cur_t != 'character varying':
                cur.execute(f'ALTER TABLE {table} ALTER COLUMN "{c}" '
                            f'TYPE VARCHAR(200) USING "{c}"::VARCHAR')
                print(f"   ~ 타입 변경: {c} {cur_t} → VARCHAR")
