"""
run_one.py  (프로젝트 루트)
════════════════════════════════════════════════════════════
공정 하나만 적재 — 개발·확인용

    python run_one.py V5071000B                 최근 45일
    python run_one.py V5071000B --days 7        최근 7일 (시험할 때 권장)
    python run_one.py V5071000B --from 2025-08-01 --to 2025-08-07
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
from datetime import datetime

import django

# ★ 사내 프로젝트의 settings 모듈 경로로 맞출 것
#   run_analysis_load.py 에 있는 값과 같아야 한다.
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from equipment import load_service as ls        # noqa: E402


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
                    help='Response·Defect 조회 쿼리만 출력 (실행 안 함)')
    args = ap.parse_args()

    if args.list:
        show_list(); return 0
    if args.status:
        show_status(); return 0
    if args.unlock:
        ids = ls.reset_stale()
        print(f'잠금 해제: {ids or "없음"}')
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
