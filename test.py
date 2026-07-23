
def _call_company_llm(system, user):
    """분석 페이지용 LLM 호출 (llm_api 와 동일한 사내 엔드포인트)"""
    resp = requests.post(
        settings.LLM_URL + '/chat/completions',
        json={
            'model': settings.LLM_MODEL,
            'messages': [
                {'role': 'system', 'content': system},
                {'role': 'user',   'content': user},
            ],
            'temperature': 0.2,      # 분석이므로 낮게 — 같은 데이터면 같은 답
        },
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {settings.LLM_API_KEY}',
        },
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()['choices'][0]['message']['content']
