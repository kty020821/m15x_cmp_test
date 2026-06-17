import json
import pandas as pd
import requests
from django.shortcuts import render
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings
from .services import get_equipment_data, get_wip_data, get_rtd_data
from .models import ProcessGroup, UPH, Product, ProductionPlan, EquipmentSchedule, ProcessTAT

def home(request):
    return render(request, 'home.html')

def equipment_page(request):
    result = get_equipment_data()
    if result['success']:
        result['opta_grouped']  = {k: v for k, v in result['grouped'].items() if k in result['opta_groups']}
        result['ch_grouped']    = {k: v for k, v in result['grouped'].items() if k in result['ch_groups']}
        result['other_grouped'] = {k: v for k, v in result['grouped'].items() if k in result['other_groups']}
    return render(request, 'equipment/status.html', {'result': result})

def wip_page(request):
    result = get_wip_data()
    return render(request, 'equipment/wip.html', {'result': result})

def llm_page(request):
    return render(request, 'equipment/llm.html')

def rtd_page(request):
    import pandas as pd
    df = pd.DataFrame([
        {'FAB': 'M15', 'LOT_CD': '5E2', 'OPER_DESC': 'CMP_STI_01', 'FLOW_ID': 'FLOW_A', 'EQP_ID': 'CMP01',    'EQP_MODEL_NM': 'OPTA-X',      'EQP_OPER_GRP_CD': 'GRP_STI', 'RTD': 'N', 'RTD_USER_NM': '',     'RTD_TM': '', 'RTD_DESC': ''},
        {'FAB': 'M15', 'LOT_CD': '5E2', 'OPER_DESC': 'CMP_STI_01', 'FLOW_ID': 'FLOW_A', 'EQP_ID': 'CMP01_P1', 'EQP_MODEL_NM': 'OPTA-X',      'EQP_OPER_GRP_CD': 'GRP_STI', 'RTD': 'N', 'RTD_USER_NM': '',     'RTD_TM': '', 'RTD_DESC': ''},
        {'FAB': 'M15', 'LOT_CD': '5E2', 'OPER_DESC': 'CMP_STI_01', 'FLOW_ID': 'FLOW_A', 'EQP_ID': 'CMP01_P2', 'EQP_MODEL_NM': 'OPTA-X',      'EQP_OPER_GRP_CD': 'GRP_STI', 'RTD': 'Y', 'RTD_USER_NM': '홍길동', 'RTD_TM': '2026-06-17 09:00', 'RTD_DESC': '파티클 이슈'},
        {'FAB': 'M15', 'LOT_CD': '5E2', 'OPER_DESC': 'CMP_STI_01', 'FLOW_ID': 'FLOW_A', 'EQP_ID': 'CMP02',    'EQP_MODEL_NM': 'OPTA-X',      'EQP_OPER_GRP_CD': 'GRP_STI', 'RTD': 'N', 'RTD_USER_NM': '',     'RTD_TM': '', 'RTD_DESC': ''},
        {'FAB': 'M15', 'LOT_CD': '5E2', 'OPER_DESC': 'CMP_STI_01', 'FLOW_ID': 'FLOW_A', 'EQP_ID': 'CMP02_P1', 'EQP_MODEL_NM': 'OPTA-X',      'EQP_OPER_GRP_CD': 'GRP_STI', 'RTD': 'N', 'RTD_USER_NM': '',     'RTD_TM': '', 'RTD_DESC': ''},
        {'FAB': 'M15', 'LOT_CD': '5E9', 'OPER_DESC': 'CMP_ILD_01', 'FLOW_ID': 'FLOW_B', 'EQP_ID': 'ELS01_AB', 'EQP_MODEL_NM': 'ELASTIC_NTH', 'EQP_OPER_GRP_CD': 'GRP_ILD', 'RTD': 'Y', 'RTD_USER_NM': '김철수', 'RTD_TM': '2026-06-17 08:30', 'RTD_DESC': 'PM 진행중'},
        {'FAB': 'M15', 'LOT_CD': '5E9', 'OPER_DESC': 'CMP_ILD_01', 'FLOW_ID': 'FLOW_B', 'EQP_ID': 'ELS01_CD', 'EQP_MODEL_NM': 'ELASTIC_NTH', 'EQP_OPER_GRP_CD': 'GRP_ILD', 'RTD': 'N', 'RTD_USER_NM': '',     'RTD_TM': '', 'RTD_DESC': ''},
        {'FAB': 'M15', 'LOT_CD': '5E9', 'OPER_DESC': 'CMP_ILD_01', 'FLOW_ID': 'FLOW_B', 'EQP_ID': 'ELS02_AB', 'EQP_MODEL_NM': 'ELASTIC_NTH', 'EQP_OPER_GRP_CD': 'GRP_ILD', 'RTD': 'N', 'RTD_USER_NM': '',     'RTD_TM': '', 'RTD_DESC': ''},
        {'FAB': 'M15', 'LOT_CD': '5E9', 'OPER_DESC': 'CMP_ILD_01', 'FLOW_ID': 'FLOW_B', 'EQP_ID': 'ELS02_CD', 'EQP_MODEL_NM': 'ELASTIC_NTH', 'EQP_OPER_GRP_CD': 'GRP_ILD', 'RTD': 'N', 'RTD_USER_NM': '',     'RTD_TM': '', 'RTD_DESC': ''},
        {'FAB': 'M15', 'LOT_CD': '5E2', 'OPER_DESC': 'CMP_W2W_01', 'FLOW_ID': 'FLOW_C', 'EQP_ID': 'REX01',    'EQP_MODEL_NM': 'NORMAL',      'EQP_OPER_GRP_CD': 'GRP_W2W', 'RTD': 'N', 'RTD_USER_NM': '',     'RTD_TM': '', 'RTD_DESC': ''},
        {'FAB': 'M15', 'LOT_CD': '5E2', 'OPER_DESC': 'CMP_W2W_01', 'FLOW_ID': 'FLOW_C', 'EQP_ID': 'REX02',    'EQP_MODEL_NM': 'NORMAL',      'EQP_OPER_GRP_CD': 'GRP_W2W', 'RTD': 'Y', 'RTD_USER_NM': '이영희', 'RTD_TM': '2026-06-17 07:00', 'RTD_DESC': '스크래치 발생'},
        {'FAB': 'M15', 'LOT_CD': '5E2', 'OPER_DESC': 'CMP_W2W_01', 'FLOW_ID': 'FLOW_C', 'EQP_ID': 'REX03',    'EQP_MODEL_NM': 'NORMAL',      'EQP_OPER_GRP_CD': 'GRP_W2W', 'RTD': 'Y', 'RTD_USER_NM': '이영희', 'RTD_TM': '2026-06-17 07:10', 'RTD_DESC': '스크래치 발생'},
    ])
    result = get_rtd_data(df)
    result_json = json.dumps(result, ensure_ascii=False, default=str)
    return render(request, 'equipment/rtd.html', {'result': result, 'result_json': result_json})
    
def llm_api(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    body     = json.loads(request.body)
    question = body.get('question', '')
    eq_result  = get_equipment_data()
    wip_result = get_wip_data()
    prompt = f"""
당신은 반도체 FAB CMP 공정 전문 어시스턴트입니다.
[장비 현황]
{json.dumps(eq_result.get('grouped', {}), ensure_ascii=False)}
[WIP 현황]
{json.dumps(wip_result.get('grouped', []), ensure_ascii=False)}
사용자 질문: {question}
데이터를 바탕으로 간결하게 한국어로 답변해줘.
"""
    try:
        resp = requests.post(
            settings.LLM_URL + '/chat/completions',
            json={'model': settings.LLM_MODEL, 'messages': [{'role': 'user', 'content': prompt}]},
            headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {settings.LLM_API_KEY}'},
            timeout=60
        )
        answer = resp.json()['choices'][0]['message']['content']
    except Exception as e:
        answer = f'LLM 연결 오류: {str(e)}'
    return JsonResponse({'answer': answer})

def equipment_api(request):
    return JsonResponse(get_equipment_data(), json_dumps_params={'ensure_ascii': False})

def capa_page(request):
    return render(request, 'equipment/capa.html')

def capa_settings(request):
    process_groups = ProcessGroup.objects.all()
    products       = Product.objects.filter(active=True)
    eq_schedules   = EquipmentSchedule.objects.all()
    uphs           = UPH.objects.select_related('process_group').all().order_by('process_group__name', 'lot_cd')
    tats           = ProcessTAT.objects.select_related('process_group').all().order_by('lot_cd', 'process_group__name')
    lot_cds        = list(UPH.objects.values_list('lot_cd', flat=True).distinct())
    process_nms    = uphs.values_list('process_nm', flat=True).distinct()
    efficiency_map = {pg.name: round(pg.efficiency * 100, 1) for pg in process_groups}
    for u in uphs:
        u.efficiency_pct = efficiency_map.get(u.process_group.name, 0)
    plans_raw         = ProductionPlan.objects.select_related('product').all().order_by('year', 'month', 'product__name')
    plan_months       = sorted(set((p.year, p.month) for p in plans_raw))
    plan_products     = sorted(set(p.product.name for p in plans_raw))
    plan_dict = {}
    for p in plans_raw:
        plan_dict.setdefault(p.product.name, {})[(p.year, p.month)] = p.qty
    plan_table_months = [{'year': y, 'month': m, 'label': f"{str(y)[2:]}.{str(m).zfill(2)}"} for y, m in plan_months]
    plan_table_rows   = [{'product': prod, 'qtys': [plan_dict.get(prod, {}).get((ym['year'], ym['month']), 0) for ym in plan_table_months]} for prod in plan_products]
    return render(request, 'equipment/capa_settings.html', {
        'process_groups': process_groups, 'products': products, 'eq_schedules': eq_schedules,
        'uphs': uphs, 'tats': tats, 'lot_cds': lot_cds, 'process_nms': process_nms,
        'plans': plans_raw, 'years': list(range(2025, 2030)), 'months': list(range(1, 13)),
        'days': list(range(1, 32)), 'plan_table_months': plan_table_months, 'plan_table_rows': plan_table_rows,
    })

@csrf_exempt
def capa_save_api(request):
    if request.method != 'POST': return JsonResponse({'error': 'POST only'}, status=405)
    body = json.loads(request.body)
    data_type = body.get('type')
    if data_type == 'process_group':
        for row in body.get('rows', []):
            ProcessGroup.objects.update_or_create(name=row['name'], defaults={'efficiency': float(row['efficiency']), 'eq_count': int(row['eq_count'])})
    elif data_type == 'uph':
        for row in body.get('rows', []):
            pg, _ = ProcessGroup.objects.get_or_create(name=row['process_group'])
            pg.efficiency = float(row['efficiency']) / 100; pg.save()
            UPH.objects.update_or_create(lot_cd=row['lot_cd'], process_group=pg, process_nm=row['process_nm'], defaults={'apw': float(row['apw'])})
    elif data_type == 'product':
        for row in body.get('rows', []):
            Product.objects.update_or_create(name=row['name'], defaults={'active': True})
    elif data_type == 'production_plan':
        for row in body.get('rows', []):
            product, _ = Product.objects.get_or_create(name=str(row['product']))
            ProductionPlan.objects.update_or_create(year=int(row['year']), month=int(row['month']), product=product, defaults={'qty': int(row['qty'])})
    elif data_type == 'eq_schedule':
        for row in body.get('rows', []):
            pg, _ = ProcessGroup.objects.get_or_create(name=row['process_group'])
            EquipmentSchedule.objects.update_or_create(process_group=pg, eq_id=row['eq_id'], arrive_date=row['arrive_date'], defaults={'arrive_date': row['arrive_date'], 'apply_date': row['apply_date'], 'note': row.get('note', '')})
    elif data_type == 'tat':
        for row in body.get('rows', []):
            pg, _ = ProcessGroup.objects.get_or_create(name=row['process_group'])
            ProcessTAT.objects.update_or_create(lot_cd=row['lot_cd'], process_nm=row['process_nm'], defaults={'process_group': pg, 'days_to_fabout': float(row['days_to_fabout'])})
    return JsonResponse({'success': True})

@csrf_exempt
def capa_update_row(request):
    if request.method != 'POST': return JsonResponse({'error': 'POST only'}, status=405)
    try:
        body = json.loads(request.body); uph = UPH.objects.get(id=body['id'])
        uph.apw = body['apw']; uph.save()
        pg = uph.process_group; pg.efficiency = body['efficiency'] / 100; pg.save()
        return JsonResponse({'success': True})
    except Exception as e: return JsonResponse({'success': False, 'error': str(e)})

@csrf_exempt
def capa_delete_api(request):
    if request.method != 'POST': return JsonResponse({'error': 'POST only'}, status=405)
    try:
        body = json.loads(request.body); data_type = body.get('type')
        if data_type == 'uph': UPH.objects.all().delete()
        elif data_type == 'tat': ProcessTAT.objects.all().delete()
        elif data_type == 'production_plan': ProductionPlan.objects.all().delete()
        elif data_type == 'eq_schedule': EquipmentSchedule.objects.all().delete()
        return JsonResponse({'success': True})
    except Exception as e: return JsonResponse({'success': False, 'error': str(e)})

@csrf_exempt
def capa_update_tat(request):
    if request.method != 'POST': return JsonResponse({'error': 'POST only'}, status=405)
    try:
        body = json.loads(request.body); tat = ProcessTAT.objects.get(id=body['id'])
        tat.days_to_fabout = body['days_to_fabout']; tat.save()
        return JsonResponse({'success': True})
    except Exception as e: return JsonResponse({'success': False, 'error': str(e)})

@csrf_exempt
def capa_update_eq(request):
    if request.method != 'POST': return JsonResponse({'error': 'POST only'}, status=405)
    try:
        body = json.loads(request.body); eq = EquipmentSchedule.objects.get(id=body['id'])
        pg, _ = ProcessGroup.objects.get_or_create(name=body['process_group'])
        eq.process_group = pg; eq.eq_id = body['eq_id']
        eq.arrive_date = body['arrive_date']; eq.apply_date = body['apply_date']
        eq.note = body.get('note', ''); eq.save()
        return JsonResponse({'success': True})
    except Exception as e: return JsonResponse({'success': False, 'error': str(e)})

@csrf_exempt
def capa_delete_eq(request):
    if request.method != 'POST': return JsonResponse({'error': 'POST only'}, status=405)
    try:
        body = json.loads(request.body)
        EquipmentSchedule.objects.filter(id__in=body.get('ids', [])).delete()
        return JsonResponse({'success': True})
    except Exception as e: return JsonResponse({'success': False, 'error': str(e)})
