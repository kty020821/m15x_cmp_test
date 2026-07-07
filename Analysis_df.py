"""
Analysis 웹 개발용 가짜 데이터 생성 → PG 저장
- 실제 데이터 갈아끼우기 전, 차트/드롭박스 확인용
- 여러 날짜 × 장비 × lot_cd 로 산포 있는 데이터 생성
"""
import os, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from analysis_save import save_analysis_df   # v3 저장 함수 재사용

np.random.seed(42)

# ── 생성 파라미터 ────────────────────────────────
OPER_ID   = 'OP100'
LOT_CDS   = ['5E2', '5E9']
EQP_IDS   = ['5CMP1E21', '5CMP1E22', '5CMP1E23']
CH_IDS    = ['AB', 'CD']
RECIPES   = ['R01', 'R02']
MODELS    = ['OPTA']
IDLE_OPTS = ['', '', '', 'idle1', 'idle2', 'PASS1 HDP CMP', 'M1 CU CMP']  # 대부분 빈칸

N_DAYS    = 30
WF_PER_LOT = 25

rows = []
base_date = datetime.now() - timedelta(days=N_DAYS)

for day in range(N_DAYS):
    cur_day = base_date + timedelta(days=day)
    for lot_cd in LOT_CDS:
        # 하루에 lot 1~2개
        for lot_seq in range(np.random.randint(1, 3)):
            lot_id = f"{lot_cd}_{cur_day.strftime('%m%d')}_{lot_seq}"
            eqp    = np.random.choice(EQP_IDS)
            ch     = np.random.choice(CH_IDS)
            recipe = np.random.choice(RECIPES)
            # 장비별로 살짝 다른 평균 (산포 차이 재현)
            eqp_bias = {'5CMP1E21': 0, '5CMP1E22': 3, '5CMP1E23': -2}[eqp]

            for wf in range(WF_PER_LOT):
                end_tm = cur_day + timedelta(hours=8, minutes=wf*3 + lot_seq*90)
                idle_val = np.random.choice(IDLE_OPTS) if wf == 0 else \
                           (np.random.choice(['', '', 'idle1']) )

                rows.append({
                    'DATE': end_tm,
                    'PROCESS_ID': 'PRC1',
                    'RECIPE_ID': recipe,
                    'EQP_ID': eqp,
                    'EQP_CH_ID': ch,
                    'EQP_MODEL': 'OPTA',
                    'OPERATION_ID': OPER_ID,
                    'LOT_CD': lot_cd,
                    'LOT_ID': lot_id,
                    'SUBSTRATE_ID': f"S{wf:02d}",
                    'WF_ID': f"{wf+1:02d}",
                    'IDLE': idle_val,
                    # 측정값들 (산포 있게)
                    'P1_TIME':  np.random.normal(55 + eqp_bias, 2),
                    'P2_TIME':  np.random.normal(60 + eqp_bias, 2.5),
                    'P3_TIME':  np.random.normal(48 + eqp_bias, 1.8),
                    'AMAT_POST_OCD_AVG': np.random.normal(1200 + eqp_bias*5, 15),
                    'PAD_LIFE_TIME': np.random.uniform(0, 200),
                    'S7_P3_04_TIME': np.random.normal(12, 0.5),
                })

df = pd.DataFrame(rows)
print(f"생성된 행: {len(df):,}")
print(f"컬럼: {df.columns.tolist()}")

save_analysis_df(df, oper_id=OPER_ID)
