def llm_api(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)

    body     = json.loads(request.body)
    question = body.get('question', '')

    # 장비 + WIP 데이터 둘 다 가져오기
    from .services import get_equipment_data, get_wip_data
    eq_result  = get_equipment_data()
    wip_result = get_wip_data()

    prompt = f"""
    당신은 반도체 FAB CMP 공정 전문 어시스턴트입니다.
    아래는 현재 CMP 장비 현황 및 WIP 데이터입니다.

    [장비 현황]
    {json.dumps(eq_result.get('grouped', {}), ensure_ascii=False)}

    [WIP 현황]
    {json.dumps(wip_result.get('grouped', []), ensure_ascii=False)}

    데이터 설명:
    - 장비: EQP_ID(장비ID), MES_STAT_TYP(Up/Down), EQP_STAT_CD(세부상태), PARTS(소모품 파트 정보)
    - WIP: SUB_GRP(공정그룹), BOH_QTY(시작재공), EOH_QTY(현재재공), WIP_TARGET(목표재공), MOVE_QTY(오늘MOVE), L_MOVE_QTY(어제MOVE), MOVE_TARGET(MOVE목표), LOSS_MONTH_QTY(이달RJ)

    사용자 질문: {question}
    데이터를 바탕으로 간결하게 한국어로 답변해줘.
    """

    resp = requests.post(
        settings.LLM_URL + '/chat/completions',
        json={
            'model': settings.LLM_MODEL,
            'messages': [{'role': 'user', 'content': prompt}]
        },
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {settings.LLM_API_KEY}'
        },
        timeout=60
    )
    answer = resp.json()['choices'][0]['message']['content']
    return JsonResponse({'answer': answer})
