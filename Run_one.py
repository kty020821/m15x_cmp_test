"""
run_one.py  (프로젝트 루트)
════════════════════════════════════════════════════════════
공정 하나만 적재 — 개발·확인용

    python run_one.py V5071000B                 최근 45일
    python run_one.py V5071000B --days 7        최근 7일 (시험할 때 권장)
    python run_one.py V5071000B --from 2025-08-01 --to 2025-08-07
    python run_one.py V5071000B --links         연계 공정 등록 상태 확인
    python run_one.py V5071000B --sql           던질 쿼리만 보기 (실행 안 함)
    python run_one.py --list                    등록된 공정 목록만 보기
    python run_one.py --status                  공정별 적재 상태

  ★ load_service 를 거치므로 웹에서 누가 같은 공정을 돌리는 중이면
    거절된다. 같은 테이블에 두 프로세스가 쓰는 사고를 막기 위한 것.

  ★ 처음 시험할 때는 --days 7 로 짧게 돌려라.
    45일을 바로 돌리면 문제가 있어도 몇 분 뒤에야 알게 된다.
════════════════════════════════════════════════════════════
"""

import os
import sys
import argparse
import traceback
from datetime import datetime


# ══════════════════════════════════════════════════════════
# 부트스트랩
#
#   ★ 프로젝트 루트를 sys.path 맨 앞에 넣는다.
#     이게 없으면 실행 위치에 따라 equipment 가 '패키지' 로 잡히지 않아
#     그 안의 `from . import param_types` 가
#     'attempted relative import with no known parent package' 로 실패한다.
#     python 은 실행한 스크립트의 폴더를 sys.path 에 넣지, 현재 폴더를
#     넣는 게 아니라서 어디서 부르느냐에 따라 결과가 달라진다.
#
#   ★ manage.py 가 있는 폴더를 루트로 본다. 이 파일이 어디에 있든
#     위로 올라가며 찾으므로 배치에 덜 민감하다.
# ══════════════════════════════════════════════════════════
def _project_root():
    here = os.path.dirname(os.path.abspath(__file__))
    cur = here
    for _ in range(5):
        if os.path.exists(os.path.join(cur, 'manage.py')):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return here


ROOT = _project_root()
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# ★ 사내 프로젝트의 settings 모듈 경로로 맞출 것
#   run_analysis_load.py 에 있는 값과 같아야 한다.
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

try:
    import django
    django.setup()
    from equipment import load_service as ls        # noqa: E402
except Exception as e:
    # 여기서 실패하면 원인을 바로 알 수 있게 상황을 함께 찍는다
    traceback.print_exc()
    print('\n' + '=' * 62)
    print('초기화 실패 — 아래를 확인하세요')
    print('=' * 62)
    print(f'  프로젝트 루트 추정 : {ROOT}')
    print(f'  manage.py 존재     : '
          f'{os.path.exists(os.path.join(ROOT, "manage.py"))}')
    print(f'  equipment/ 존재    : '
          f'{os.path.isdir(os.path.join(ROOT, "equipment"))}')
    print(f'  equipment/__init__ : '
          f'{os.path.exists(os.path.join(ROOT, "equipment", "__init__.py"))}')
    print(f'  SETTINGS           : {os.environ.get("DJANGO_SETTINGS_MODULE")}')
    print(f'  실행 위치(CWD)     : {os.getcwd()}')
    print(f'  이 파일            : {os.path.abspath(__file__)}')
    print('\n  · equipment/__init__.py 가 없으면 만들어 주세요 (빈 파일)')
    print('  · SETTINGS 가 실제 settings 모듈 경로와 다르면 위 줄을 고치세요')
    sys.exit(1)


def show_list():
    from equipment import config_service as cfg
    rows = cfg.list_opers()
    if not rows:
        print('등록된 공정이 없습니다. 셋업 페이지에서 먼저 등록하세요.')
        return
    print(f'{"OPER_ID":<16}{"공정명":<22}{"device":>7}{"param":>7}'
          f'{"resp":>6}{"defect":>7}  사용')
    print('-' * 74)
    for o in rows:
        print(f'{o["oper_id"]:<16}{(o.get("oper_desc") or "")[:20]:<22}'
              f'{o.get("n_lot", 0):>7}{o.get("n_param", 0):>7}'
              f'{o.get("n_resp", 0):>6}{o.get("n_defect", 0):>7}'
              f'  {o.get("use_yn", "Y")}')


def show_status():
    st = ls.status()
    if not st:
        print('적재 이력이 없습니다.')
        return
    print(f'{"OPER_ID":<16}{"최근 데이터":<21}{"행수":>10}  상태')
    print('-' * 68)
    for oper_id, d in sorted(st.items()):
        run = d.get('running')
        state = (f'실행중 ({run["by"]}, {run["elapsed_min"]}분 경과)' if run
                 else f'{d.get("last_status", "-")} '
                      f'{d.get("last_at", "")}'.strip())
        print(f'{oper_id:<16}{str(d.get("data_max") or "-"):<21}'
              f'{d.get("rows", 0):>10,}  {state}')


def show_links(oper_id):
    """
    기준정보 v2 에 등록된 연계 공정을 그대로 출력한다.

    ★ '챔버가 SRC 로 나간다' 같은 문제는 대부분 등록 상태가 원인이다.
      DB 에 저장된 행을 그대로 보여 주면 코드를 뒤질 필요가 없다.
    """
    from equipment import config2_service as cfg2

    d = cfg2.get_oper(oper_id)
    if not d:
        print(f'{oper_id} 가 기준정보 v2 에 없습니다 '
              f'(config2 페이지에서 등록하거나 가져오기를 하세요)')
        return

    print(f'\n{"=" * 66}\n저장된 연계 공정 행 — {oper_id}\n{"=" * 66}')
    if not d['links']:
        print('  등록된 연계 공정이 없습니다')
    for r in d['links']:
        print(f"  {r['kind']:<4} {r['alias']:<12} {r['link_id']:<12} "
              f"lot_cd={r['lot_cd'] or '(본공정)':<8} "
              f"param={r['param'] or '(없음)':<14} "
              f"{r['scope']:<5} {r['use_yn']}")

    reg_lots = cfg2.lots_of(oper_id)
    print(f'\n등록된 LOT_CD: {reg_lots or "(없음 — 연계 조회가 건너뛰어집니다)"}')
    print('  ※ 연계 조회는 이 목록으로만 나갑니다. '
          '적재 데이터의 LOT_CD 는 쓰지 않습니다.')

    print(f'\n{"=" * 66}\n조회 단위로 묶은 결과 (실제 쿼리가 나가는 기준)\n{"=" * 66}')

    # ★ 어느 테이블로 나가는지는 kind 가 아니라 want_chm / params 가 정한다.
    #   chamber 는 wafer-history, 나머지 PARAM 은 타입별 측정 테이블.
    #   한 연계 공정이 둘 다 가져갈 수 있다.
    tbl = {'SRC': 'tas_src_wf_metr_inf',
           'REP': 'tas_rep_wf_metr_inf',
           'DEF': 'tas_dft_wf_inf'}

    for scope in (None, 'mon', 'ana'):
        gs = cfg2.links_of(oper_id, scope)
        tag = {None: '전체(1회성)', 'mon': '정기 적재', 'ana': '분석 전용'}[scope]
        print(f'\n  [{tag}] {len(gs)}건')
        for g in gs:
            if not g.get('want_chm') and not g.get('params'):
                print(f"    {g['alias']:<12} 조회할 것이 없습니다 "
                      f"(PARAM 을 확인하세요)")
                continue
            lots = g.get('lot_cds') or reg_lots
            print(f"    {g['alias']:<12} ({g['kind']}, {g['link_id']}) "
                  f"lot_cd={lots}")
            if g.get('want_chm'):
                print(f"       chamber -> apc_sk_wafer_hst_r2r_all_* "
                      f"(장비·챔버)")
            if g.get('params'):
                print(f"       {g['params']} -> "
                      f"{tbl.get(g['kind'], '?')}")
            print(f"       columns={g['columns']}")

        # 구버전이 배포돼 있으면 want_chm 키 자체가 없다 — 바로 알려 준다
        if gs and 'want_chm' not in gs[0]:
            print('\n  ※ 배포된 config2_service.py 가 구버전입니다 — '
                  'chamber 분기가 없는 버전이라 최신본으로 교체하세요')


def show_sql(oper_id, days, date_from, date_to):
    """실행하지 않고 던질 쿼리만 출력 — Lake 에 그대로 붙여넣어 확인용"""
    from equipment import analysis_service as svc

    # ── 기준정보 v2 의 연계 공정 ─────────────────────────
    try:
        from equipment import link_service as lks
        from equipment import config2_service as cfg2
        d = cfg2.get_oper(oper_id)
        lots = [l['lot_cd'] for l in (d or {}).get('lots', [])
                if l.get('use_yn') != 'N'] if d else []
        qs = lks.preview_sql(oper_id, days=days, date_from=date_from,
                             date_to=date_to, lot_cds=lots)
        print(f'\n{"=" * 62}\n연계 공정 (기준정보 v2) — {len(qs)}건\n{"=" * 62}')
        if not qs:
            print('  등록된 연계 공정이 없습니다 (config2 에서 등록하세요)')
        for q in qs[:8]:
            print(f'\n─── {q["kind"]}:{q["alias"]} · {q["link_id"]} '
                  f'· lot_cd={q["lot_cd"]} ' + '─' * 18)
            print(q['query'])
    except Exception as e:
        print(f'\n[연계 공정] 조회 생략: {e.__class__.__name__}: {e}')

    df_info = svc.get_config()
    for kind, label in (('resp', 'Response'), ('def', 'Defect')):
        qs = svc.preview_step_sql(df_info, oper_id, kind, days=days,
                                  date_from=date_from, date_to=date_to)
        print(f'\n{"=" * 62}\n{label} — {len(qs)}건 (최대 6건만 표시)\n{"=" * 62}')
        if not qs:
            print('  등록된 계측 스텝이 없습니다 (셋업 페이지에서 등록하세요)')
            continue
        for q in qs:
            print(f'\n─── {q["label"]} · step={q["step_id"]} '
                  f'· lot_cd={q["lot_cd"]} ' + '─' * 20)
            print(q['query'])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('oper_id', nargs='?', help='적재할 공정 (예: V5071000B)')
    ap.add_argument('--days', type=int, default=ls.DEFAULT_DAYS,
                    help=f'최근 며칠 (기본 {ls.DEFAULT_DAYS})')
    ap.add_argument('--from', dest='date_from', help='시작일 YYYY-MM-DD')
    ap.add_argument('--to', dest='date_to', help='종료일 YYYY-MM-DD')
    ap.add_argument('--user', default='terminal', help='요청자 표기')
    ap.add_argument('--list', action='store_true', help='공정 목록만 보기')
    ap.add_argument('--status', action='store_true', help='적재 상태만 보기')
    ap.add_argument('--unlock', action='store_true',
                    help="멈춘 '실행중' 잠금 해제")
    ap.add_argument('--sql', action='store_true',
                    help='연계 공정 조회 쿼리만 출력 (실행 안 함)')
    ap.add_argument('--links', action='store_true',
                    help='기준정보 v2 의 연계 공정 등록 상태 확인')
    args = ap.parse_args()

    if args.list:
        show_list(); return 0
    if args.status:
        show_status(); return 0
    if args.unlock:
        ids = ls.reset_stale()
        print(f'잠금 해제: {ids or "없음"}')
        return 0
    if args.links:
        if not args.oper_id:
            print('공정 ID 가 필요합니다. 예: python run_one.py V5071000B --links')
            return 1
        show_links(args.oper_id.upper())
        return 0
    if args.sql:
        if not args.oper_id:
            print('공정 ID 가 필요합니다. 예: python run_one.py V5071000B --sql')
            return 1
        show_sql(args.oper_id.upper(), args.days,
                 args.date_from, args.date_to)
        return 0
    if not args.oper_id:
        ap.print_help()
        print('\n먼저 --list 로 공정 ID 를 확인하세요.')
        return 1

    oper_id = args.oper_id.upper()
    t0 = datetime.now()
    print(f'[{oper_id}] 적재 시작 — 최근 {args.days}일 '
          f'({t0:%Y-%m-%d %H:%M:%S})')

    res = ls.run_oper(oper_id, days=args.days, user=args.user,
                      date_from=args.date_from, date_to=args.date_to)

    sec = (datetime.now() - t0).seconds
    if res.get('ok'):
        print(f'[{oper_id}] 완료 — {res.get("rows", 0):,}행 · '
              f'{sec // 60}분 {sec % 60}초')
        return 0

    print(f'[{oper_id}] 실패 — {res.get("error")}')
    if res.get('running'):
        r = res['running']
        print(f'  현재 {r.get("by")} 님이 {r.get("started_at")} 부터 '
              f'적재 중입니다 ({r.get("message")})')
        print('  멈춘 것으로 보이면: python run_one.py --unlock')
    return 1


if __name__ == '__main__':
    sys.exit(main())
