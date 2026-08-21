"""
equipment/views_monitor.py
════════════════════════════════════════════════════════════
Inline Monitoring 페이지 + API

  화면
    · [점검 시작] → 공정 목록을 받아 한 공정씩 순차 호출
      (한 요청에 전 공정을 몰면 512MiB 웹서버에서 타임아웃 위험)
    · 결과 테이블 — 심각도순 정렬, 행마다 30일 추이 미니차트
    · 행 클릭 → 웨이퍼 단위 상세 차트

  API
    monitor/                 페이지
    api/monitor/opers/       점검 대상 공정 목록
    api/monitor/run/         공정 1건 점검 (POST oper_id)
    api/monitor/results/     저장된 최근 결과
    api/monitor/clear/       저장된 결과 초기화
    api/monitor/diag/        적재·점검 상태 진단
    monitor/report/          점검 결과를 한 파일 HTML 리포트로
    api/monitor/detail/      웨이퍼 상세
    
    [★ 추가 기능: 백그라운드 DB 구축 API]
    api/monitor/build_db/    비동기 스레드를 활용해 백그라운드에서 DB 적재 진행
    api/monitor/build_status/ 적재 진행률 확인(폴링용)
════════════════════════════════════════════════════════════
"""

import json
import re
import traceback
import threading

from django.shortcuts import render
from django.http import JsonResponse, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.db import connections
from django.core.cache import cache

from . import tech_map
from . import monitor_service as ms
from . import param_types as pt
from equipment import analysis_service as svc


def _fail(msg, payload=None, exc=None):
    print(f'[monitor] {msg}')
    if exc is not None:
        traceback.print_exc()
    out = dict(payload or {})
    out['error'] = msg
    return JsonResponse(out, status=200)

def _safe(v):
    return bool(v) and bool(re.match(r'^[0-9A-Za-z_\-]+$', str(v)))

# ══════════════════════════════════════════════════════════
def monitor_page(request):
    return render(request, 'equipment/monitor.html',
                  {'type_options': pt.options()})

@csrf_exempt
def monitor_opers(request):
    try:
        with connections['analysis_db'].cursor() as cur:
            cur.execute(
                "SELECT tablename FROM pg_tables WHERE tablename LIKE %s",
                ['cmp_analysis_%'])
            tables = {r[0] for r in cur.fetchall()}

        out, source = [], ''
        try:
            from . import config_service as cs
            for o in cs.list_opers():
                if str(o.get('use_yn') or 'Y').upper() == 'N': continue
                oid = o['oper_id']
                if ms._table(oid) in tables:
                    desc = o.get('oper_desc') or ''
                    out.append({'oper_id': oid, 'label': f'{desc} ({oid})' if desc else oid})
            if out: source = '기준정보'
        except Exception as e:
            pass

        if not out:
            names = {}
            try: names = tech_map.oper_names()
            except Exception: pass
            for oid, desc in names.items():
                if ms._table(oid) in tables:
                    out.append({'oper_id': oid, 'label': f'{desc} ({oid})' if desc else oid})
            if out: source = 'tech_map'

        if out:
            out.sort(key=lambda o: o['label'])
            res = {'opers': out, 'source': source}
            known = {ms._table(o['oper_id']) for o in out}
            orphan = sorted(t for t in tables if t not in known)
            if orphan:
                res['note'] = (f'{source} 에 없어 점검에서 제외된 테이블 {len(orphan)}개')
            return JsonResponse(res)

        return JsonResponse({
            'opers': [{'oper_id': t.replace('cmp_analysis_', '').upper(),
                       'label':   t.replace('cmp_analysis_', '').upper()}
                      for t in sorted(tables)],
            'source': '적재테이블',
            'note': '기준정보에 등록된 공정이 없어 적재 테이블을 그대로 표시합니다',
        })
    except Exception as e:
        return _fail(f'공정 목록 조회 실패: {e}', {'opers': []}, exc=e)

@csrf_exempt
def monitor_run(request):
    if request.method != 'POST': return JsonResponse({'error': 'POST only'}, status=405)
    try: body = json.loads(request.body)
    except Exception: return _fail('요청 형식 오류', {'results': []})

    oper_id = body.get('oper_id')
    label   = body.get('label', '')
    if not _safe(oper_id): return _fail('oper_id 형식 오류', {'results': []})

    try: return JsonResponse(ms.run_check(oper_id, label))
    except Exception as e: return _fail(f'{oper_id} 점검 실패: {e}', {'oper_id': oper_id, 'results': []}, exc=e)

@csrf_exempt
def monitor_results(request):
    try: return JsonResponse(ms.load_results())
    except Exception as e: return _fail(f'결과 조회 실패: {e}', {'run_ts': None, 'results': []}, exc=e)

@csrf_exempt
def monitor_clear(request):
    if request.method != 'POST': return JsonResponse({'error': 'POST only'}, status=405)
    try: body = json.loads(request.body) if request.body else {}
    except Exception: body = {}
    oper_id = body.get('oper_id')
    if oper_id and not _safe(oper_id): return _fail('oper_id 형식 오류', {'deleted': 0})
    try:
        n = ms.clear_results(oper_id or None)
        return JsonResponse({'deleted': n})
    except Exception as e: return _fail(f'초기화 실패: {e}', {'deleted': 0}, exc=e)

@csrf_exempt
def monitor_diag(request):
    try: return JsonResponse(dict(ms.diagnose(), ok=True))
    except Exception as e: return _fail(f'진단 실패: {e}', {'items': [], 'orphans': []}, exc=e)

@csrf_exempt
def monitor_detail(request):
    if request.method != 'POST': return JsonResponse({'error': 'POST only'}, status=405)
    try: body = json.loads(request.body)
    except Exception: return _fail('요청 형식 오류', {'points': []})
    oper_id = body.get('oper_id')
    lot_cd  = body.get('lot_cd')
    param   = body.get('param')
    if not (_safe(oper_id) and _safe(param)): return _fail('요청 값 형식 오류', {'points': []})
    try: return JsonResponse(ms.wafer_detail(oper_id, lot_cd, param))
    except Exception as e: return _fail(f'상세 조회 실패: {e}', {'points': []}, exc=e)

@csrf_exempt
def monitor_report(request):
    from datetime import datetime
    from . import monitor_report as mr

    g = request.GET if request.method == 'GET' else _body(request)
    only = str(g.get('only_issue', '1')).lower() not in ('0', 'false', 'no')
    download = str(g.get('download', '')).lower() in ('1', 'true', 'yes')
    oper_id = g.get('oper_id') or None

    try:
        html = mr.build_report(only_issue=only, oper_id=oper_id, title=g.get('title') or '')
    except Exception as e:
        traceback.print_exc()
        html = f'<h3>리포트 생성 실패</h3><p>{e.__class__.__name__}: {e}</p>'

    resp = HttpResponse(html, content_type='text/html; charset=utf-8')
    if download:
        name = f'monitor_report_{datetime.now():%Y%m%d_%H%M}.html'
        resp['Content-Disposition'] = f'attachment; filename="{name}"'
    return resp

# ══════════════════════════════════════════════════════════
# 백그라운드 DB 구축 및 진행률 확인 (신규 추가)
# ══════════════════════════════════════════════════════════
@csrf_exempt
def monitor_build_db(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    
    try:
        body = json.loads(request.body)
    except Exception:
        return JsonResponse({'ok': False, 'error': '요청 형식이 올바르지 않습니다.'})

    raw_oper_id = body.get('oper_id', '')
    is_cfg2 = bool(body.get('is_cfg2', False))
    
    # 순수 공정 ID 추출
    oper_id = re.sub(r'_(CFG2|cfg2)$', '', str(raw_oper_id).strip(), flags=re.IGNORECASE).upper()
    
    if not oper_id or not _safe(oper_id):
        return JsonResponse({'ok': False, 'error': f'유효하지 않은 공정 ID입니다: {raw_oper_id}'})

    task_id = f"db_build_{oper_id}_{'cfg2' if is_cfg2 else 'cfg1'}"
    
    cache.set(task_id, {'status': 'starting', 'progress': 0, 'msg': '적재 준비 중...'}, timeout=3600)

    def background_task():
        try:
            lake = svc.get_lake()
            df_info = svc.get_config(is_cfg2=is_cfg2)
            
            # 기준정보에 해당 공정이 있는지 검증
            if df_info is None or df_info.empty:
                raise ValueError("기준정보 DB를 불러올 수 없습니다.")
            
            opers_in_cfg = set(df_info['OPER_ID'].astype(str).str.upper().unique())
            if oper_id not in opers_in_cfg:
                raise ValueError(f"기준정보(Config{'2' if is_cfg2 else '1'})에 등록되지 않은 공정({oper_id})입니다.")

            def update_progress(done, total, msg):
                pct = int((done / total) * 100) if total else 0
                cache.set(task_id, {'status': 'running', 'progress': pct, 'msg': msg}, timeout=3600)
            
            update_progress(1, 10, f'{oper_id} Lake 조회 시작')
            df = svc.build_analysis_df(lake, df_info, oper_id, on_progress=update_progress, is_cfg2=is_cfg2)
            
            if df is None or df.empty:
                cache.set(task_id, {'status': 'done', 'progress': 100, 'msg': '조회 결과가 없습니다 (0행).'}, timeout=3600)
                return

            update_progress(9, 10, f'{len(df):,}행 저장 중...')
            svc.save_analysis_df(df, oper_id, is_cfg2=is_cfg2)
            cache.set(task_id, {'status': 'done', 'progress': 100, 'msg': f'{len(df):,}행 적재 완료'}, timeout=3600)
        except Exception as e:
            traceback.print_exc()
            cache.set(task_id, {'status': 'error', 'progress': 0, 'msg': f'에러: {e}'}, timeout=3600)
        finally:
            connections.close_all()

    threading.Thread(target=background_task, daemon=True).start()
    return JsonResponse({'ok': True, 'task_id': task_id})

@csrf_exempt
def monitor_build_status(request):
    """현재 DB 구축 진행률 조회 API (프론트엔드 폴링용)"""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'})
    body = json.loads(request.body)
    task_id = body.get('task_id')
    
    state = cache.get(task_id) or {'status': 'none', 'msg': '진행 상태를 찾을 수 없습니다.'}
    return JsonResponse({'ok': True, 'state': state})
  
