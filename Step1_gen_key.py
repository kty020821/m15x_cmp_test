"""
gen_fake_data.py  (프로젝트 루트, manage.py 옆)
개발용 가짜 데이터 → PG 적재
실행: python gen_fake_data.py
"""
import os, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

import re
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from django.db import connections
from psycopg2.extras import execute_values

np.random.seed(42)

OPER_ID = 'OP100'

# ── 컬럼 타입 분류 (문자 컬럼만 여기 등록, 나머지는 숫자) ──
TEXT_COLS = {
    'PROCESS_ID', 'RECIPE_ID', 'EQP_ID', 'EQP_CH_ID', 'EQP_MODEL',
    'OPERATION_ID', 'LOT_CD', 'LOT_ID', 'SUBSTRATE_ID', 'WF_ID',
    'IDLE', 'PRE_LAYER',
}
TIME_COLS = {'DATE'}


def pg_type(col):
    cu = col.upper()
    if cu in TIME_COLS: return 'TIMESTAMP'
    if cu in TEXT_COLS: return 'VARCHAR(100)'
    return 'DOUBLE PRECISION'


def table_name(oper_id):
    return f"cmp_analysis_{re.sub(r'[^0-9A-Za-z_]', '_', str(oper_id)).lower()}"


# ── 가짜 데이터 생성 ─────────────────────────────────────
EQP_IDS = ['5CMP1E21', '5CMP1E22', '5CMP1E23']
CH_IDS  = ['AB', 'CD']
RECIPES = ['R01', 'R02']
LOT_CDS = ['5E2', '5E9']
PRE_LAYERS = ['', '', '', 'PASS1 HDP CMP', 'M1 CU CMP', 'STI CMP']

rows = []
base = datetime.now() - timedelta(days=30)

for day in range(30):
    d = base + timedelta(days=day)
    for lot_cd in LOT_CDS:
        for seq in range(np.random.randint(1, 3)):
            lot_id = f"{lot_cd}{d.strftime('%m%d')}{seq}"
            eqp    = np.random.choice(EQP_IDS)
            ch     = np.random.choice(CH_IDS)
            recipe = np.random.choice(RECIPES)
            bias   = {'5CMP1E21': 0, '5CMP1E22': 3, '5CMP1E23': -2}[eqp]

            # 이 lot이 idle 후 첫 진행인지 / layer change인지
            is_idle  = np.random.rand() < 0.25
            is_layer = (not is_idle) and np.random.rand() < 0.20
            pre_layer = np.random.choice(PRE_LAYERS[3:]) if is_layer else ''

            pad_life_start = np.random.uniform(0, 180)

            for wf in range(25):
                # IDLE 컬럼: idle 후 1~4번째 wafer만 표기, layer change는 첫 wf만
                if is_idle and wf < 4:
                    idle_val = f"idle_{wf+1}"
                elif is_layer and wf == 0:
                    idle_val = "layer_change"
                else:
                    idle_val = ""

                # idle 직후 wafer는 산포가 큼 (분석 재현용)
                extra = np.random.normal(0, 4) if (is_idle and wf < 2) else 0

                rows.append({
                    'DATE': d + timedelta(hours=8, minutes=wf * 3 + seq * 90),
                    'PROCESS_ID':   'PRC1',
                    'RECIPE_ID':    recipe,
                    'EQP_ID':       eqp,
                    'EQP_CH_ID':    ch,
                    'EQP_MODEL':    'OPTA',
                    'OPERATION_ID': OPER_ID,
                    'LOT_CD':       lot_cd,
                    'LOT_ID':       lot_id,
                    'SUBSTRATE_ID': f"{lot_id}.{wf+1:02d}",
                    'WF_ID':        f"{wf+1:02d}",
                    'IDLE':         idle_val,
                    'PRE_LAYER':    pre_layer if wf == 0 else '',
                    # 측정값
                    'THK_AVG':       np.random.normal(1200 + bias * 5, 15) + extra * 3,
                    'THK_RANGE':     abs(np.random.normal(12, 3)) + abs(extra),
                    'POLISH_TIME':   np.random.normal(55 + bias, 2) + extra,
                    'P1_PRESSURE':   np.random.normal(3.2, 0.15),
                    'P2_PRESSURE':   np.random.normal(3.5, 0.15),
                    'PAD_LIFE_TIME': pad_life_start + wf * 0.4,
                    'DISK_LIFE':     np.random.uniform(10, 300),
                    'HEAD_PRESSURE': np.random.normal(4.1, 0.2),
                })

df = pd.DataFrame(rows)
print(f"생성: {len(df):,}행 / 컬럼 {len(df.columns)}개")

# ── PG 적재 ──────────────────────────────────────────────
table = table_name(OPER_ID)
conn  = connections['analysis_db']

col_defs = ["id BIGSERIAL PRIMARY KEY"] + [f'"{c}" {pg_type(c)}' for c in df.columns]
with conn.cursor() as cur:
    cur.execute(f"DROP TABLE IF EXISTS {table}")
    cur.execute(f"CREATE TABLE {table} (\n  " + ",\n  ".join(col_defs) + "\n)")
    cur.execute(f'CREATE INDEX idx_{table}_lot  ON {table} ("LOT_CD")')
    cur.execute(f'CREATE INDEX idx_{table}_date ON {table} ("DATE")')

df = df.where(pd.notnull(df), None)
cols    = list(df.columns)
col_str = ", ".join(f'"{c}"' for c in cols)
data    = [tuple(r) for r in df.itertuples(index=False, name=None)]

with conn.cursor() as cur:
    execute_values(cur.cursor, f'INSERT INTO {table} ({col_str}) VALUES %s',
                   data, page_size=1000)

print(f"[완료] {table} 에 {len(df):,}행 적재")
