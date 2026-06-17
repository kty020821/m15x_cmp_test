import os
import django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

import pandas as pd
from equipment.services import get_rtd_data

df = pd.DataFrame([
    {'FAB': 'M15', 'LOT_CD': '5E2', 'OPER_DESC': 'CMP_STI_01', 'FLOW_ID': 'FLOW_A', 'EQP_ID': 'CMP01',     'EQP_MODEL_NM': 'OPTA-X', 'EQP_OPER_GRP_CD': 'GRP_STI', 'RTD': 'N', 'RTD_USER_NM': '',     'RTD_TM': '', 'RTD_DESC': ''},
    {'FAB': 'M15', 'LOT_CD': '5E2', 'OPER_DESC': 'CMP_STI_01', 'FLOW_ID': 'FLOW_A', 'EQP_ID': 'CMP01_P1',  'EQP_MODEL_NM': 'OPTA-X', 'EQP_OPER_GRP_CD': 'GRP_STI', 'RTD': 'N', 'RTD_USER_NM': '',     'RTD_TM': '', 'RTD_DESC': ''},
    {'FAB': 'M15', 'LOT_CD': '5E2', 'OPER_DESC': 'CMP_STI_01', 'FLOW_ID': 'FLOW_A', 'EQP_ID': 'CMP01_P2',  'EQP_MODEL_NM': 'OPTA-X', 'EQP_OPER_GRP_CD': 'GRP_STI', 'RTD': 'Y', 'RTD_USER_NM': '홍길동', 'RTD_TM': '2026-06-17 09:00', 'RTD_DESC': '파티클 이슈'},
    {'FAB': 'M15', 'LOT_CD': '5E2', 'OPER_DESC': 'CMP_STI_01', 'FLOW_ID': 'FLOW_A', 'EQP_ID': 'CMP02',     'EQP_MODEL_NM': 'OPTA-X', 'EQP_OPER_GRP_CD': 'GRP_STI', 'RTD': 'N', 'RTD_USER_NM': '',     'RTD_TM': '', 'RTD_DESC': ''},
    {'FAB': 'M15', 'LOT_CD': '5E2', 'OPER_DESC': 'CMP_STI_01', 'FLOW_ID': 'FLOW_A', 'EQP_ID': 'CMP02_P1',  'EQP_MODEL_NM': 'OPTA-X', 'EQP_OPER_GRP_CD': 'GRP_STI', 'RTD': 'N', 'RTD_USER_NM': '',     'RTD_TM': '', 'RTD_DESC': ''},
    {'FAB': 'M15', 'LOT_CD': '5E9', 'OPER_DESC': 'CMP_ILD_01', 'FLOW_ID': 'FLOW_B', 'EQP_ID': 'ELS01_AB',  'EQP_MODEL_NM': 'ELASTIC_NTH', 'EQP_OPER_GRP_CD': 'GRP_ILD', 'RTD': 'Y', 'RTD_USER_NM': '김철수', 'RTD_TM': '2026-06-17 08:30', 'RTD_DESC': 'PM 진행중'},
    {'FAB': 'M15', 'LOT_CD': '5E9', 'OPER_DESC': 'CMP_ILD_01', 'FLOW_ID': 'FLOW_B', 'EQP_ID': 'ELS01_CD',  'EQP_MODEL_NM': 'ELASTIC_NTH', 'EQP_OPER_GRP_CD': 'GRP_ILD', 'RTD': 'N', 'RTD_USER_NM': '',     'RTD_TM': '', 'RTD_DESC': ''},
    {'FAB': 'M15', 'LOT_CD': '5E9', 'OPER_DESC': 'CMP_ILD_01', 'FLOW_ID': 'FLOW_B', 'EQP_ID': 'ELS02_AB',  'EQP_MODEL_NM': 'ELASTIC_NTH', 'EQP_OPER_GRP_CD': 'GRP_ILD', 'RTD': 'N', 'RTD_USER_NM': '',     'RTD_TM': '', 'RTD_DESC': ''},
    {'FAB': 'M15', 'LOT_CD': '5E9', 'OPER_DESC': 'CMP_ILD_01', 'FLOW_ID': 'FLOW_B', 'EQP_ID': 'ELS02_CD',  'EQP_MODEL_NM': 'ELASTIC_NTH', 'EQP_OPER_GRP_CD': 'GRP_ILD', 'RTD': 'N', 'RTD_USER_NM': '',     'RTD_TM': '', 'RTD_DESC': ''},
    {'FAB': 'M15', 'LOT_CD': '5E2', 'OPER_DESC': 'CMP_W2W_01', 'FLOW_ID': 'FLOW_C', 'EQP_ID': 'REX01',     'EQP_MODEL_NM': 'NORMAL',  'EQP_OPER_GRP_CD': 'GRP_W2W', 'RTD': 'N', 'RTD_USER_NM': '',     'RTD_TM': '', 'RTD_DESC': ''},
    {'FAB': 'M15', 'LOT_CD': '5E2', 'OPER_DESC': 'CMP_W2W_01', 'FLOW_ID': 'FLOW_C', 'EQP_ID': 'REX02',     'EQP_MODEL_NM': 'NORMAL',  'EQP_OPER_GRP_CD': 'GRP_W2W', 'RTD': 'Y', 'RTD_USER_NM': '이영희', 'RTD_TM': '2026-06-17 07:00', 'RTD_DESC': '스크래치 발생'},
    {'FAB': 'M15', 'LOT_CD': '5E2', 'OPER_DESC': 'CMP_W2W_01', 'FLOW_ID': 'FLOW_C', 'EQP_ID': 'REX03',     'EQP_MODEL_NM': 'NORMAL',  'EQP_OPER_GRP_CD': 'GRP_W2W', 'RTD': 'Y', 'RTD_USER_NM': '이영희', 'RTD_TM': '2026-06-17 07:10', 'RTD_DESC': '스크래치 발생'},
])

result = get_rtd_data(df)
print('success:', result['success'])
print('cards수:', len(result['cards']))
for c in result['cards']:
    print(f"  {c['oper_desc']} / {c['lot_cd']} | 전체:{c['total']} 가능:{c['avail']} RTD:{c['rtd']}")
