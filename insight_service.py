"""
equipment/insight_service.py
════════════════════════════════════════════════════════════
분석 엔진 — 이슈 구간이 왜 다른지 찾는다

  LLM 이 호출할 '도구' 들이다. 각 함수는 압축된 근거표를 돌려주고,
  LLM 은 그 표를 읽어 우선순위를 매기고 공정 맥락으로 해석한다.

────────────────────────────────────────────────────────────
설계 원칙

★ 계산은 여기서, 해석은 LLM 이.
  원시 데이터를 LLM 에 넘기지 않는다 — 웨이퍼 수천 장 × 컬럼 수백 개는
  컨텍스트에 안 들어가고, 들어가도 LLM 은 숫자 계산을 틀린다.

★ 다중비교 보정을 반드시 한다.
  파라미터 200개를 훑으면 우연히 유의한 게 10개는 나온다.
  FDR(Benjamini-Hochberg) 보정 없이는 '유의한 변수 15개 발견' 같은
  헛소리가 나온다.

★ 표본 크기를 함께 돌려준다.
  구간이 웨이퍼 20장이면 통계가 못 미덥다. LLM 은 그걸 무시하고
  단정하기 쉬우므로 신뢰도를 출력에 넣어 함께 읽게 한다.

★ 효과크기를 우선한다.
  p값은 표본이 크면 사소한 차이도 유의해진다. 실제로 중요한 것은
  '얼마나 다른가'(Cohen's d)이므로 그걸 기준으로 정렬한다.
════════════════════════════════════════════════════════════
"""

import re
import math
import traceback

import numpy as np
import pandas as pd
from django.db import connections

# 값이 아니라 식별·구분용인 컬럼
META_COLS = {
    'ID', 'DATE', 'PROCESS_ID', 'RECIPE_ID', 'EQP_ID', 'EQP_CH_ID',
    'EQP_MODEL', 'OPERATION_ID', 'LOT_CD', 'LOT_ID', 'SUBSTRATE_ID',
    'WF_ID', 'IDLE', 'PRE_LAYER', 'PRE_EQP_ID', 'PRE_EQP_CH', 'QTY',
    'REWORK_N', 'MEAS_N',
}

# 범주형으로 다룰 컬럼 (장비·챔버 등) — 이름 규칙으로 추가 판정
CAT_HINTS = ('_EQP', '_CH', 'EQP_ID', 'EQP_CH_ID', 'RECIPE_ID',
             'PRE_EQP_ID', 'PRE_EQP_CH', 'PRE_LAYER', 'IDLE')

NUMERIC_TYPES = {
    'smallint', 'integer', 'bigint', 'decimal', 'numeric',
    'real', 'double precision',
}

# 표본이 이보다 적으면 결과를 믿기 어렵다고 표시한다
MIN_SAMPLE = 30
WEAK_SAMPLE = 100


def _conn():
    return connections['analysis_db']


def _span_case_local(spans, names, args):
    """
    이슈 구간 → SQL CASE. views_analysis2._span_case 의 사본.

    ★ 규칙이 갈리면 차트에서 고른 구간과 분석 대상이 달라진다.
      원본이 있으면 그쪽을 쓰고, 이건 없을 때만 쓰는 대비책이다.
    """
    parts = []
    for i, sp in enumerate(spans or []):
        c = _span_clause_local(sp, names, args)
        if c:
            parts.append(f'WHEN {c} THEN {i}')
    if not parts:
        return 'CAST(NULL AS INTEGER)'
    return 'CASE ' + ' '.join(parts) + ' ELSE NULL END'


def _span_clause_local(span, names, args):
    """구간 하나 → 조건절 (기간 / 랏 / 웨이퍼)"""
    mode = (span or {}).get('mode', 'range')

    if mode == 'range':
        d1, d2 = str(span.get('date_from') or ''), str(span.get('date_to') or '')
        if not d1 or not d2:
            return None
        if d1 > d2:
            d1, d2 = d2, d1
        if len(d1) == 10:
            d1 += ' 00:00:00'
        if len(d2) == 10:
            d2 += ' 23:59:59'
        args += [d1, d2]
        return '("DATE" >= %s AND "DATE" <= %s)'

    if mode == 'lots':
        vals = sorted({str(v).strip().upper()[:7]
                       for v in (span.get('lot_ids') or []) if str(v).strip()})
        if not vals or 'LOT_ID' not in names:
            return None
        args += vals
        ph = ",".join(['%s'] * len(vals))
        return f'(left(CAST("LOT_ID" AS VARCHAR), 7) IN ({ph}))'

    if mode == 'wafers':
        vals = []
        for v in (span.get('wafers') or []):
            t = str(v).strip().upper()
            if not t:
                continue
            lot, _, wf = t.rpartition('.')
            if not lot:
                continue
            vals.append(f'{lot[:7]}.{wf.zfill(2)}')
        vals = sorted(set(vals))
        if not vals or 'LOT_ID' not in names or 'WF_ID' not in names:
            return None
        args += vals
        ph = ",".join(['%s'] * len(vals))
        return (f"(left(CAST(\"LOT_ID\" AS VARCHAR), 7) || '.' || "
                f"lpad(CAST(\"WF_ID\" AS VARCHAR), 2, '0') IN ({ph}))")

    return None


def _real_names(cur, table):
    """
    대문자 이름 → 실제 컬럼 이름.

    ★ 비교는 대문자로 하지만 SQL 에는 실제 이름을 써야 한다.
      테이블을 만들 때 id 만 따옴표 없이 써서 소문자로 저장됐고,
      나머지는 "CU_THK_AVG" 처럼 따옴표라 대문자다.
      그래서 "ID" 로 조회하면 column "ID" does not exist 가 난다.

    ★ views_analysis2 에도 같은 함수가 있지만 여기 따로 둔다 —
      한쪽 파일만 배포됐을 때 AttributeError 로 죽지 않게.
    """
    cur.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = %s ORDER BY ordinal_position
    """, [table])
    return {c.upper(): c for (c,) in cur.fetchall()}


def _table(oper_id):
    return f"cmp_analysis_{re.sub(r'[^0-9A-Za-z_]', '_', str(oper_id)).lower()}"


def _safe(v):
    return bool(v) and bool(re.match(r'^[0-9A-Za-z_\-]+$', str(v)))


# ══════════════════════════════════════════════════════════
# 데이터 적재
# ══════════════════════════════════════════════════════════
def load_frame(oper_id, spans, lot_cd=None, max_cols=400):
    """
    분석용 DataFrame 을 만든다.

    각 행에 __span 열이 붙는다 — 0,1,2… 는 이슈 구간, -1 은 나머지.
    (views_analysis2 의 구간 판정과 같은 규칙을 쓴다)
    """
    # 구간 판정은 차트와 같은 규칙을 쓰는 게 원칙이라 원본을 먼저 찾는다.
    #   그 모듈이 없거나 예전 버전이어도 죽지 않게 감싼다.
    try:
        from . import views_analysis2 as v2
    except Exception:
        v2 = None

    table = _table(oper_id)
    with _conn().cursor() as cur:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", [table])
        if not cur.fetchone()[0]:
            raise ValueError(f'{table} 이 없습니다 — 먼저 적재하세요')

        cur.execute("""
            SELECT column_name, data_type FROM information_schema.columns
            WHERE table_name = %s ORDER BY ordinal_position
        """, [table])
        cols = [(c.upper(), d.lower()) for c, d in cur.fetchall()]
        names = {c for c, _ in cols}

        # ★ SQL 에는 실제 컬럼 이름을 써야 한다.
        #   비교는 대문자로 하지만, 테이블에 소문자 id 로 들어 있는데
        #   "ID" 로 조회하면 column "ID" does not exist 가 난다.
        real = _real_names(cur, table)

        num = [c for c, d in cols if d in NUMERIC_TYPES and c not in META_COLS
               and not c.endswith(('_OFFSET', '_FORMULA'))][:max_cols]
        cat = [c for c, _ in cols
               if c in names and (c in CAT_HINTS or c.endswith(('_EQP', '_CH')))]

        # ID 가 없는 테이블도 있으므로 있을 때만 넣는다
        keep = [c for c in ('ID', 'DATE', 'LOT_ID', 'WF_ID', 'LOT_CD')
                if c in names] + cat + num

        args = []
        # ★ 구간 판정은 차트와 같은 규칙을 써야 한다 (다르면 화면에서 본 것과
        #   분석 대상이 어긋난다). 다만 그 모듈이 예전 버전이면 함수가 없어
        #   죽으므로, 없을 때는 여기 사본을 쓴다.
        span_fn = (getattr(v2, '_span_case', None) if v2 else None) \
                  or _span_case_local
        span_sql = span_fn(spans, names, args)
        where, wargs = '1=1', []
        if lot_cd and 'LOT_CD' in names:
            where, wargs = f'"{real.get("LOT_CD", "LOT_CD")}" = %s', [lot_cd]

        sel = ", ".join(f'"{real.get(c, c)}"' for c in dict.fromkeys(keep))
        cur.execute(f'''
            SELECT {sel}, COALESCE({span_sql}, -1) AS __span
            FROM {table} WHERE {where} ORDER BY "{real.get('DATE', 'DATE')}"
        ''', args + wargs)
        rows = cur.fetchall()
        colnames = [d[0].upper() for d in cur.description]

    df = pd.DataFrame(rows, columns=colnames)
    df.rename(columns={'__SPAN': '__span'}, inplace=True)
    for c in num:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors='coerce')
    return df, num, [c for c in cat if c in df.columns]


def _fdr(pvals):
    """
    Benjamini-Hochberg FDR 보정.
    ★ 파라미터를 수백 개 훑으면 우연히 유의한 게 반드시 나온다.
      보정 없이 'p<0.05 인 것 20개' 를 보고하면 대부분 거짓이다.
    """
    p = np.asarray(pvals, dtype=float)
    n = len(p)
    if n == 0:
        return p
    order = np.argsort(p)
    ranked = p[order] * n / (np.arange(n) + 1)
    # 단조 감소 보정
    ranked = np.minimum.accumulate(ranked[::-1])[::-1]
    out = np.empty(n)
    out[order] = np.clip(ranked, 0, 1)
    return out


def _confidence(n_in, n_out):
    """표본 크기로 신뢰도를 매긴다 — LLM 이 단정하지 않도록"""
    small = min(n_in, n_out)
    if small < MIN_SAMPLE:
        return 'low', f'표본이 적습니다 (구간 {n_in}장 / 나머지 {n_out}장)'
    if small < WEAK_SAMPLE:
        return 'medium', f'표본이 다소 적습니다 (구간 {n_in}장)'
    return 'high', ''


def _welch_p_normal(x, y):
    """
    scipy 없이 Welch 검정의 p 값 — 정규근사.

    ★ 표본이 30 이상이면 t분포와 정규분포가 거의 같아
      실용적으로 충분하다. 표본이 아주 작으면 p 값이 조금 낙관적이지만,
      그런 경우는 신뢰도(_confidence)가 이미 'low' 로 표시된다.
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    nx, ny = len(x), len(y)
    if nx < 2 or ny < 2:
        return 1.0
    se = np.sqrt(x.var(ddof=1) / nx + y.var(ddof=1) / ny)
    if se == 0:
        return 1.0
    z = abs(x.mean() - y.mean()) / se
    # 양측 p = 2 * (1 - Φ(z)) — 오차함수로 계산
    return float(2.0 * (1.0 - 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))))


# ══════════════════════════════════════════════════════════
# 도구 1. 구간 비교
# ══════════════════════════════════════════════════════════
def compare_groups(oper_id, spans, lot_cd=None, span_a=0, span_b=None,
                   top=20):
    """
    구간 A 와 B(없으면 나머지 전체)를 전 파라미터에 대해 비교한다.

    반환: 효과크기 순 상위 목록 + 표본 정보
      diff        평균 차이
      cohens_d    효과크기 (0.2 작음 / 0.5 중간 / 0.8 큼)
      p, q        t검정 p값과 FDR 보정값
    """
    # ★ scipy 가 없어도 이 도구는 돌아야 한다 — 가장 기본이 되는 분석이다.
    #   없으면 정규근사로 p 값을 구한다(표본이 크면 t검정과 거의 같다).
    try:
        from scipy import stats
        _ttest = lambda x, y: stats.ttest_ind(x, y, equal_var=False)[1]
    except ImportError:
        _ttest = _welch_p_normal

    df, num, cat = load_frame(oper_id, spans, lot_cd)
    a = df[df['__span'] == span_a]
    b = df[df['__span'] == span_b] if span_b is not None else df[df['__span'] != span_a]

    if a.empty or b.empty:
        return {'ok': False,
                'error': f'비교할 데이터가 없습니다 (A {len(a)}행 / B {len(b)}행)'}

    rows, pvals = [], []
    for c in num:
        x, y = a[c].dropna(), b[c].dropna()
        if len(x) < 3 or len(y) < 3:
            continue
        mx, my = float(x.mean()), float(y.mean())
        sx, sy = float(x.std(ddof=1)), float(y.std(ddof=1))
        # 합동 표준편차 (Cohen's d)
        pooled = np.sqrt(((len(x) - 1) * sx**2 + (len(y) - 1) * sy**2) /
                         max(1, len(x) + len(y) - 2))
        d = (mx - my) / pooled if pooled > 0 else 0.0
        try:
            p = _ttest(x, y)
        except Exception:
            p = 1.0
        rows.append({'param': c, 'n_a': len(x), 'n_b': len(y),
                     'mean_a': round(mx, 4), 'mean_b': round(my, 4),
                     'std_a': round(sx, 4), 'std_b': round(sy, 4),
                     'diff': round(mx - my, 4),
                     'cohens_d': round(float(d), 3)})
        pvals.append(float(p) if np.isfinite(p) else 1.0)

    if not rows:
        return {'ok': False, 'error': '비교 가능한 숫자 파라미터가 없습니다'}

    q = _fdr(pvals)
    for r, p, qq in zip(rows, pvals, q):
        r['p'] = round(float(p), 6)
        r['q'] = round(float(qq), 6)
        r['significant'] = bool(qq < 0.05 and abs(r['cohens_d']) >= 0.2)

    rows.sort(key=lambda r: abs(r['cohens_d']), reverse=True)
    conf, note = _confidence(len(a), len(b))

    return {
        'ok': True, 'tool': 'compare_groups',
        'n_a': len(a), 'n_b': len(b),
        'n_param': len(rows),
        'n_significant': sum(1 for r in rows if r['significant']),
        'confidence': conf, 'note': note,
        'items': rows[:top],
        'method': 'Welch t검정 + BH FDR 보정, 효과크기(Cohen\'s d) 순 정렬',
    }


# ══════════════════════════════════════════════════════════
# 도구 2. 요인 중요도
# ══════════════════════════════════════════════════════════
def feature_importance(oper_id, spans, lot_cd=None, span_a=0, top=20):
    """
    '이슈 구간인가' 를 맞히는 모델을 세워 변수 중요도를 본다.

    ★ 단변량 비교(도구 1)는 변수를 하나씩만 본다. 트리 모델은
      변수들이 함께 작용하는 경우를 잡아낸다.
    ★ permutation importance 를 쓴다 — 트리의 기본 중요도는
      값 종류가 많은 변수를 과대평가한다.
    """
    try:
        from sklearn.ensemble import RandomForestClassifier
        from sklearn.inspection import permutation_importance
        from sklearn.model_selection import train_test_split
        from sklearn.metrics import roc_auc_score
    except ImportError:
        # ★ scikit-learn 이 없는 서버가 있다. 그렇다고 이 도구가
        #   통째로 죽으면 안 되므로 단변량 대체 방식으로 답한다.
        #   변수 간 상호작용은 못 잡지만 순위는 낼 수 있다.
        return _importance_fallback(oper_id, spans, lot_cd, span_a, top)

    df, num, cat = load_frame(oper_id, spans, lot_cd)
    y = (df['__span'] == span_a).astype(int)
    if y.sum() < 10 or (1 - y).sum() < 10:
        return {'ok': False,
                'error': f'표본이 부족합니다 (구간 {int(y.sum())}장 / '
                         f'나머지 {int((1-y).sum())}장 · 각각 10장 이상 필요)'}

    # 범주형은 원-핫으로 (값 종류가 많으면 상위만)
    X = df[num].copy()
    for c in cat:
        vc = df[c].astype(str).value_counts()
        for v in vc.index[:8]:
            X[f'{c}={v}'] = (df[c].astype(str) == v).astype(int)

    # 결측은 중앙값으로 — 트리는 결측을 못 받는다
    X = X.fillna(X.median(numeric_only=True)).fillna(0)
    X = X.loc[:, X.std() > 0]           # 값이 하나뿐인 열은 제외
    if X.empty:
        return {'ok': False, 'error': '변동이 있는 변수가 없습니다'}

    Xtr, Xte, ytr, yte = train_test_split(
        X, y, test_size=0.3, random_state=0,
        stratify=y if min(y.sum(), (1 - y).sum()) >= 2 else None)

    m = RandomForestClassifier(n_estimators=200, max_depth=8,
                               min_samples_leaf=5, random_state=0,
                               class_weight='balanced', n_jobs=-1)
    m.fit(Xtr, ytr)

    try:
        auc = float(roc_auc_score(yte, m.predict_proba(Xte)[:, 1]))
    except Exception:
        auc = float('nan')

    imp = permutation_importance(m, Xte, yte, n_repeats=10,
                                 random_state=0, n_jobs=-1)
    items = sorted(
        [{'feature': c, 'importance': round(float(v), 5),
          'std': round(float(s), 5)}
         for c, v, s in zip(X.columns, imp.importances_mean,
                            imp.importances_std) if v > 0],
        key=lambda r: r['importance'], reverse=True)[:top]

    conf, note = _confidence(int(y.sum()), int((1 - y).sum()))
    # 모델이 못 맞히면 중요도도 의미가 없다
    if not np.isnan(auc) and auc < 0.6:
        conf = 'low'
        note = (note + ' · ' if note else '') + \
               f'모델이 구간을 잘 구분하지 못합니다 (AUC {auc:.2f}) — ' \
               f'요인 순위를 신뢰하기 어렵습니다'

    return {
        'ok': True, 'tool': 'feature_importance',
        'n_in': int(y.sum()), 'n_out': int((1 - y).sum()),
        'auc': None if np.isnan(auc) else round(auc, 3),
        'confidence': conf, 'note': note,
        'items': items,
        'method': 'RandomForest + permutation importance (검증셋 30%)',
    }


def _importance_fallback(oper_id, spans, lot_cd=None, span_a=0, top=20):
    """
    scikit-learn 이 없을 때의 요인 순위.

    각 변수가 '구간 여부' 를 얼마나 가르는지를 AUC 로 잰다.
    한 변수만 쓰는 분류기의 성능과 같아서, 트리 모델의 중요도와
    비슷한 순위를 준다.

    ★ 한계: 변수를 하나씩만 보므로 '둘이 함께일 때만 문제' 인 경우를
      못 잡는다. 그 사실을 note 에 적어 LLM 이 오해하지 않게 한다.
    """
    df, num, cat = load_frame(oper_id, spans, lot_cd)
    y = (df['__span'] == span_a).astype(int).to_numpy()
    if y.sum() < 10 or (1 - y).sum() < 10:
        return {'ok': False,
                'error': f'표본이 부족합니다 (구간 {int(y.sum())}장 / '
                         f'나머지 {int((1 - y).sum())}장)'}

    def auc_of(v):
        """순위 기반 AUC — 정규분포를 가정하지 않는다"""
        m = ~pd.isna(v)
        if m.sum() < 20:
            return None
        vv, yy = np.asarray(v)[m], y[m]
        n1, n0 = yy.sum(), (1 - yy).sum()
        if n1 < 5 or n0 < 5:
            return None
        r = pd.Series(vv).rank().to_numpy()
        a = (r[yy == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0)
        return float(a)

    items = []
    for c in num:
        a = auc_of(df[c].to_numpy())
        if a is None:
            continue
        # 0.5 에서 얼마나 벗어났나 — 방향은 무관
        items.append({'feature': c, 'importance': round(abs(a - 0.5) * 2, 4),
                      'auc': round(a, 3)})

    # 범주형은 수준별 구간 비율의 퍼짐으로 본다
    for c in cat:
        g = pd.DataFrame({'v': df[c].astype(str), 'y': y})
        rate = g.groupby('v')['y'].agg(['mean', 'count'])
        rate = rate[rate['count'] >= MIN_SAMPLE]
        if len(rate) < 2:
            continue
        items.append({'feature': c,
                      'importance': round(float(rate['mean'].max()
                                                - rate['mean'].min()), 4),
                      'levels': int(len(rate))})

    if not items:
        return {'ok': False, 'error': '평가 가능한 변수가 없습니다'}

    items.sort(key=lambda r: r['importance'], reverse=True)
    conf, note = _confidence(int(y.sum()), int((1 - y).sum()))
    note = ((note + ' · ') if note else '') + \
           ('scikit-learn 이 없어 단변량 방식으로 계산했습니다 — '
            '변수 하나씩만 보므로 여러 변수가 함께 작용하는 경우는 '
            '잡지 못합니다')

    return {
        'ok': True, 'tool': 'feature_importance',
        'n_in': int(y.sum()), 'n_out': int((1 - y).sum()),
        'auc': None, 'confidence': conf, 'note': note,
        'items': items[:top],
        'method': '변수별 단변량 AUC (sklearn 미설치 대체)',
    }


# ══════════════════════════════════════════════════════════
# 도구 3. 교호작용
# ══════════════════════════════════════════════════════════
def interaction(oper_id, spans, lot_cd=None, span_a=0, target=None, top=12):
    """
    범주형(장비·챔버) × 연속형 조합에서 기울기가 달라지는지 본다.

    ★ CMP 에서 실제로 의미 있는 교호작용은 대체로 이 형태다 —
      '장비마다 압력의 영향이 다르다' 같은 것.
    ★ 연속×연속은 조합이 폭발하므로 여기서 다루지 않는다.
      필요하면 요인 중요도 상위끼리 따로 본다.

    target 을 주면 그 값을 종속변수로, 없으면 '구간 여부' 를 쓴다.
    """
    df, num, cat = load_frame(oper_id, spans, lot_cd)
    if not cat:
        return {'ok': False,
                'error': '장비·챔버 같은 범주형 컬럼이 없어 교호작용을 볼 수 없습니다'}

    if target and target.upper() in df.columns:
        yv = pd.to_numeric(df[target.upper()], errors='coerce')
        ylabel = target.upper()
    else:
        yv = (df['__span'] == span_a).astype(float)
        ylabel = '구간 여부'

    rows = []
    for cc in cat:
        groups = df[cc].astype(str)
        vc = groups.value_counts()
        levels = [v for v in vc.index[:6] if vc[v] >= MIN_SAMPLE]
        if len(levels) < 2:
            continue

        for nc in num:
            xv = pd.to_numeric(df[nc], errors='coerce')
            slopes = []
            for lv in levels:
                m = (groups == lv) & xv.notna() & yv.notna()
                if m.sum() < MIN_SAMPLE:
                    continue
                x, y = xv[m].to_numpy(), yv[m].to_numpy()
                if np.std(x) == 0:
                    continue
                # 단순 회귀 기울기
                b = float(np.polyfit(x, y, 1)[0])
                slopes.append({'level': lv, 'n': int(m.sum()),
                               'slope': round(b, 6)})
            if len(slopes) < 2:
                continue

            vals = [s['slope'] for s in slopes]
            spread = max(vals) - min(vals)

            # ★ 기울기 차이를 '표준화' 한다.
            #   비율(차이/최대기울기)만 쓰면 기울기가 0에 가까울 때 비율이
            #   폭발해 잡음이 상위로 올라온다. 실제로 그 현상을 확인했다.
            #   y 의 산포 대비 x 한 단위가 y 를 얼마나 움직이는지로 바꾼다.
            sx = float(np.nanstd(xv)) or 1e-12
            sy = float(np.nanstd(yv)) or 1e-12
            std_spread = spread * sx / sy      # 무차원 — 변수 간 비교 가능

            flip = (max(vals) > 0 > min(vals))
            # 부호가 갈려도 크기가 미미하면 의미 없다 — 표준화 값에만 가중
            score = std_spread * (1.5 if flip else 1.0)

            rows.append({
                'category': cc, 'param': nc,
                'spread': round(spread, 6),
                'std_spread': round(float(std_spread), 4),
                'sign_flip': bool(flip),
                'levels': sorted(slopes, key=lambda s: s['slope']),
                'score': round(float(score), 4),
            })

    # 표준화 기울기 차이가 0.1 미만이면 실질적 의미가 없다
    rows = [r for r in rows if r['std_spread'] >= 0.1]
    rows.sort(key=lambda r: r['score'], reverse=True)
    n_in = int((df['__span'] == span_a).sum())
    conf, note = _confidence(n_in, len(df) - n_in)

    return {
        'ok': True, 'tool': 'interaction',
        'target': ylabel, 'n_pair': len(rows),
        'confidence': conf, 'note': note,
        'items': rows[:top],
        'method': '범주 수준별 회귀 기울기 비교 — 기울기 차이를 x·y 산포로 '
                  '표준화(무차원), 부호가 갈리면 1.5배 가중, 0.1 미만 제외',
    }


# ══════════════════════════════════════════════════════════
# 도구 4. 범주별 분포
# ══════════════════════════════════════════════════════════
def distribution(oper_id, spans, param, lot_cd=None, by=None, span_a=0):
    """특정 파라미터를 장비·챔버별로 쪼개 본다"""
    df, num, cat = load_frame(oper_id, spans, lot_cd)
    p = str(param).upper()
    if p not in df.columns:
        return {'ok': False, 'error': f'{p} 컬럼이 없습니다'}

    keys = [by.upper()] if by and by.upper() in df.columns else cat
    if not keys:
        return {'ok': False, 'error': '구분할 범주형 컬럼이 없습니다'}

    out = []
    for k in keys[:3]:
        grp = []
        for lv, g in df.groupby(df[k].astype(str)):
            v = pd.to_numeric(g[p], errors='coerce').dropna()
            if len(v) < 5:
                continue
            inn = g['__span'] == span_a
            grp.append({
                'level': lv, 'n': len(v),
                'mean': round(float(v.mean()), 4),
                'std': round(float(v.std(ddof=1)), 4) if len(v) > 1 else 0.0,
                'median': round(float(v.median()), 4),
                'n_in_span': int(inn.sum()),
                'span_ratio': round(float(inn.mean()), 3),
            })
        if grp:
            grp.sort(key=lambda r: r['mean'], reverse=True)
            out.append({'by': k, 'levels': grp})

    return {'ok': True, 'tool': 'distribution', 'param': p, 'groups': out,
            'method': '범주별 평균·표준편차와 구간 포함 비율'}


# ══════════════════════════════════════════════════════════
# 도구 5. 시계열 변화
# ══════════════════════════════════════════════════════════
def timeline(oper_id, spans, param, lot_cd=None, bins=40):
    """파라미터가 시간에 따라 어떻게 변했는지 — 구간이 어디에 있는지"""
    df, num, cat = load_frame(oper_id, spans, lot_cd)
    p = str(param).upper()
    if p not in df.columns:
        return {'ok': False, 'error': f'{p} 컬럼이 없습니다'}

    d = df[['DATE', p, '__span']].dropna(subset=[p]).copy()
    if d.empty:
        return {'ok': False, 'error': f'{p} 에 값이 없습니다'}

    d['DATE'] = pd.to_datetime(d['DATE'], errors='coerce')
    d = d.dropna(subset=['DATE']).sort_values('DATE')
    if len(d) < 4:
        return {'ok': False, 'error': '점이 너무 적습니다'}

    # 시간을 균등 구간으로 나눠 요약 — 원시 점을 다 넘기지 않는다
    d['__bin'] = pd.cut(d['DATE'].astype('int64'), bins=min(bins, len(d)),
                        labels=False)
    out = []
    for b, g in d.groupby('__bin'):
        v = g[p]
        out.append({
            'from': str(g['DATE'].min())[:19], 'to': str(g['DATE'].max())[:19],
            'n': len(v), 'mean': round(float(v.mean()), 4),
            'std': round(float(v.std(ddof=1)), 4) if len(v) > 1 else 0.0,
            'in_span': int((g['__span'] >= 0).sum()),
        })

    return {'ok': True, 'tool': 'timeline', 'param': p,
            'n': len(d), 'bins': out,
            'method': f'시간을 {len(out)}구간으로 나눈 요약'}


# ══════════════════════════════════════════════════════════
# 도구 목록 (LLM function calling 스펙)
# ══════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════
# 소표본 도구 — 이상 웨이퍼가 몇 장뿐일 때
#
#   ★ 반도체 공정의 이상은 대개 산발적이다. 30일치에서 몇 장만
#     튀는 일이 흔하고, 그 몇 장이 분석 대상이다.
#     평균 비교(compare_groups)나 트리 모델(feature_importance)은
#     '집단 대 집단' 을 보므로 이런 경우엔 쓸 수 없다.
#
#   ★ 접근을 바꾼다.
#       ① 웨이퍼 한 장씩 — 정상 분포 대비 어디서 얼마나 벗어났나
#       ② 공통점 — 그 몇 장이 같은 챔버·같은 시간대인가
#     5장이 통계적으로 무의미해도, 그 5장이 전부 챔버3이면
#     그건 강한 신호다. 초기하 검정으로 그 정도는 말할 수 있다.
# ══════════════════════════════════════════════════════════

# 소표본 도구가 요구하는 최소 장수 — 1장이어도 지문은 낼 수 있다
TINY_MIN = 1

# 공통점 판정에 필요한 최소 장수 (2장은 있어야 '공통' 이 성립)
COMMON_MIN = 2


def _num(v, nd=4):
    """JSON 에 담기 좋게 — NaN/inf 는 None"""
    try:
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return None
        return round(x, nd)
    except (TypeError, ValueError):
        return None


def _robust_center_scale(v):
    """
    중앙값과 MAD 기반 산포.

    ★ 평균·표준편차를 쓰면 이상치 자신이 기준을 오염시킨다.
      몇 장이 크게 튀면 표준편차가 부풀어 '별로 안 벗어남' 으로 나온다.
      중앙값과 MAD 는 그 영향을 거의 안 받는다.
    """
    v = np.asarray(v, dtype=float)
    v = v[~np.isnan(v)]
    if len(v) < 5:
        return None, None
    med = float(np.median(v))
    mad = float(np.median(np.abs(v - med)))
    # 정규분포에서 MAD × 1.4826 ≈ 표준편차
    scale = mad * 1.4826
    if scale <= 0:
        # 값이 거의 같아 MAD 가 0이면 사분위로 대체
        q1, q3 = np.percentile(v, [25, 75])
        scale = (q3 - q1) / 1.349
    return med, (scale if scale > 0 else None)


def wafer_fingerprint(oper_id, spans, lot_cd=None, span_a=0, top=12,
                      min_z=2.0):
    """
    이상 웨이퍼 한 장씩의 '지문' — 어느 파라미터에서 얼마나 벗어났나.

    ★ 집단 비교가 아니라 개별 판정이므로 1장이어도 답이 나온다.
    ★ 기준은 나머지 웨이퍼의 중앙값·MAD 다. 이상치가 기준을
      오염시키지 않게 하기 위한 것.
    ★ robust z 는 '나머지 분포에서 몇 배 벗어났나' 를 뜻한다.
      |z| >= 3 이면 뚜렷하고, 2~3 은 참고 수준이다.
    """
    df, num, cat = load_frame(oper_id, spans, lot_cd)
    a = df[df['__span'] == span_a]
    b = df[df['__span'] != span_a]

    if a.empty:
        return {'ok': False, 'error': '지정한 구간에 웨이퍼가 없습니다'}
    if len(b) < 20:
        return {'ok': False,
                'error': f'비교 기준이 될 정상 웨이퍼가 적습니다 ({len(b)}장) '
                         f'— 조회 기간을 늘려 보세요'}

    # 파라미터별 기준선 (구간 밖 웨이퍼로만)
    base = {}
    for c in num:
        med, scale = _robust_center_scale(b[c])
        if med is not None and scale:
            base[c] = (med, scale)

    if not base:
        return {'ok': False, 'error': '기준선을 만들 수 있는 파라미터가 없습니다'}

    wafers = []
    for _, row in a.iterrows():
        hits = []
        for c, (med, scale) in base.items():
            v = row.get(c)
            if v is None or pd.isna(v):
                continue
            z = (float(v) - med) / scale
            if abs(z) >= min_z:
                hits.append({'param': c, 'value': _num(v), 'z': _num(z, 2),
                             'normal': _num(med), 'dir': '높음' if z > 0 else '낮음'})
        hits.sort(key=lambda h: abs(h['z']), reverse=True)

        wafers.append({
            'wafer': _wafer_key(row),
            'date': str(row.get('DATE') or '')[:19],
            'n_out': len(hits),
            'top': hits[:6],
            'context': {c: str(row.get(c)) for c in cat if row.get(c) is not None},
        })

    wafers.sort(key=lambda w: w['n_out'], reverse=True)

    # 여러 장에서 반복해 튀는 파라미터 — 그게 공통 원인 후보다
    tally = {}
    for w in wafers:
        for h in w['top']:
            t = tally.setdefault(h['param'], {'param': h['param'], 'n': 0,
                                              'z_sum': 0.0, 'dirs': set()})
            t['n'] += 1
            t['z_sum'] += abs(h['z'])
            t['dirs'].add(h['dir'])
    shared = [{'param': t['param'], 'n_wafer': t['n'],
               'ratio': _num(t['n'] / len(wafers), 2),
               'avg_z': _num(t['z_sum'] / t['n'], 2),
               'direction': '/'.join(sorted(t['dirs']))}
              for t in tally.values()]
    shared.sort(key=lambda r: (r['n_wafer'], r['avg_z'] or 0), reverse=True)

    return {
        'ok': True, 'tool': 'wafer_fingerprint',
        'n_wafer': len(wafers), 'n_base': len(b), 'n_param': len(base),
        'min_z': min_z,
        'confidence': 'high' if len(b) >= 200 else 'medium',
        'wafers': wafers[:top],
        'shared': shared[:12],
        'method': '나머지 웨이퍼의 중앙값·MAD 기준 robust z',
        'note': ('웨이퍼 한 장씩 본 결과입니다 — 표본이 적어도 유효합니다. '
                 'shared 는 여러 장에서 함께 벗어난 파라미터로, '
                 'n_wafer 가 클수록 공통 원인일 가능성이 높습니다.'),
    }


def _wafer_key(row):
    lot = row.get('LOT_ID')
    wf = row.get('WF_ID')
    if lot is None:
        return str(row.get('SUBSTRATE_ID') or '')
    w = str(wf or '')
    if w.isdigit():
        w = w.zfill(2)
    return f'{str(lot)[:7]}.{w}' if w else str(lot)[:7]


def _hyper_p(k, n, K, N):
    """
    초기하 상한꼬리 확률 — 'n장 중 k장이 우연히 이 조건일 확률'.

    ★ 표본이 작아도 쓸 수 있는 검정이다.
      전체에서 챔버3 비중이 20%인데 이상 5장이 전부 챔버3이면
      p = 0.0003 수준으로 나온다. 평균 비교로는 절대 못 잡는다.
    """
    if k <= 0 or n <= 0 or K <= 0 or N <= 0 or n > N or K > N:
        return 1.0
    tot = 0.0
    for i in range(k, min(n, K) + 1):
        try:
            tot += (math.comb(K, i) * math.comb(N - K, n - i)) / math.comb(N, n)
        except (ValueError, ZeroDivisionError):
            break
    return float(min(1.0, max(0.0, tot)))


def common_traits(oper_id, spans, lot_cd=None, span_a=0, top=15):
    """
    이상 웨이퍼들이 공유하는 조건 — 같은 챔버? 같은 시간대? 같은 랏?

    ★ 이것이 소표본 분석의 핵심이다.
      5장이 통계적으로 무의미해도 그 5장이 전부 같은 챔버면 강한 신호다.
      초기하 검정으로 '우연일 확률' 을 계산해 그 강도를 수치화한다.
    """
    df, num, cat = load_frame(oper_id, spans, lot_cd)
    a = df[df['__span'] == span_a]
    n, N = len(a), len(df)

    if n < COMMON_MIN:
        return {'ok': False,
                'error': f'공통점을 찾으려면 최소 {COMMON_MIN}장이 필요합니다 '
                         f'(현재 {n}장) — 웨이퍼 지문(wafer_fingerprint)을 '
                         f'대신 쓰세요'}

    items = []

    # ── 범주형 (장비·챔버·레시피·랏) ──────────────────
    for c in cat:
        whole = df[c].astype(str)
        pick = a[c].astype(str)
        for lv, k in pick.value_counts().items():
            K = int((whole == lv).sum())
            if K == 0:
                continue
            p = _hyper_p(int(k), n, K, N)
            items.append({
                'kind': '범주', 'key': c, 'level': lv,
                'k': int(k), 'n': n, 'K': K, 'N': N,
                'in_ratio': _num(k / n, 3),
                'base_ratio': _num(K / N, 3),
                'lift': _num((k / n) / (K / N), 2) if K else None,
                'p': _num(p, 6),
            })

    # ── 시간대·요일 ─────────────────────────────────
    if 'DATE' in df.columns:
        dt = pd.to_datetime(df['DATE'], errors='coerce')
        dt_a = pd.to_datetime(a['DATE'], errors='coerce')
        for name, fn in (('시간대', lambda s: s.dt.hour // 4 * 4),
                         ('요일', lambda s: s.dt.dayofweek)):
            try:
                whole = fn(dt).dropna().astype(int)
                pick = fn(dt_a).dropna().astype(int)
            except Exception:
                continue
            if not len(pick):
                continue
            for lv, k in pick.value_counts().items():
                K = int((whole == lv).sum())
                if K == 0:
                    continue
                label = (f'{lv:02d}~{lv + 4:02d}시' if name == '시간대'
                         else ['월', '화', '수', '목', '금', '토', '일'][int(lv)])
                items.append({
                    'kind': '시간', 'key': name, 'level': label,
                    'k': int(k), 'n': n, 'K': K, 'N': int(len(whole)),
                    'in_ratio': _num(k / n, 3),
                    'base_ratio': _num(K / len(whole), 3),
                    'lift': _num((k / n) / (K / len(whole)), 2),
                    'p': _num(_hyper_p(int(k), n, K, int(len(whole))), 6),
                })

    if not items:
        return {'ok': False, 'error': '비교할 범주형 컬럼이 없습니다'}

    # 다중비교 보정 — 후보가 많으므로 필요하다
    q = _fdr([r['p'] for r in items])
    for r, qq in zip(items, q):
        r['q'] = _num(qq, 5)
        # 우연이 아니고, 실제로 몰려 있어야 의미가 있다
        r['significant'] = bool(qq < 0.05 and (r['lift'] or 0) >= 1.5
                                and r['k'] >= 2)

    items.sort(key=lambda r: (r['significant'], -(r['p'] or 1)), reverse=True)
    n_sig = sum(1 for r in items if r['significant'])

    return {
        'ok': True, 'tool': 'common_traits',
        'n_wafer': n, 'n_total': N, 'n_candidate': len(items),
        'n_significant': n_sig,
        # 표본이 아니라 '몰림' 을 보므로 장수가 적어도 신뢰할 수 있다
        'confidence': 'high' if n_sig else 'medium',
        'items': items[:top],
        'method': '초기하 검정 (우연히 이만큼 몰릴 확률) + FDR 보정',
        'note': ('표본이 적어도 유효한 분석입니다 — 평균 차이가 아니라 '
                 "'우연히 이만큼 몰릴 확률' 을 봅니다. "
                 'lift 는 전체 비중 대비 몇 배로 몰렸는지이고, '
                 'k/n 은 이상 웨이퍼 중 해당 조건의 장수입니다.'),
    }


def rule_search(oper_id, spans, lot_cd=None, span_a=0, top=10,
                max_depth=2):
    """
    조건 조합 탐색 — "챔버3 + 압력 상위 20%" 같은 규칙에서 이상률이 튀는 곳.

    ★ 단일 조건으로 안 갈리는 경우를 잡는다.
      챔버3 전체는 멀쩡한데 '챔버3이면서 압력이 높을 때만' 이상이면
      common_traits 로는 안 보인다.
    ★ 연속형은 사분위로 나눠 범주처럼 다룬다 — 소표본에서 임계값을
      정밀하게 찾으려 하면 과적합된다.
    """
    df, num, cat = load_frame(oper_id, spans, lot_cd)
    y = (df['__span'] == span_a).to_numpy()
    n, N = int(y.sum()), len(df)

    if n < COMMON_MIN:
        return {'ok': False,
                'error': f'규칙 탐색은 최소 {COMMON_MIN}장이 필요합니다 (현재 {n}장)'}

    # 조건 후보 만들기
    conds = []
    for c in cat:
        v = df[c].astype(str)
        for lv in v.value_counts().index[:8]:
            conds.append((f'{c}={lv}', (v == lv).to_numpy()))

    for c in num[:120]:               # 너무 많으면 조합이 폭발한다
        v = pd.to_numeric(df[c], errors='coerce')
        if v.notna().sum() < 30:
            continue
        try:
            q1, q3 = np.nanpercentile(v, [25, 75])
        except Exception:
            continue
        if q1 == q3:
            continue
        conds.append((f'{c}<=Q1({_num(q1)})', (v <= q1).to_numpy()))
        conds.append((f'{c}>=Q3({_num(q3)})', (v >= q3).to_numpy()))

    if not conds:
        return {'ok': False, 'error': '조건을 만들 수 있는 컬럼이 없습니다'}

    base_rate = n / N
    found = []

    def score(mask, label):
        K = int(mask.sum())
        if K < 5:
            return None
        k = int((mask & y).sum())
        if k < 2:
            return None
        rate = k / K
        if rate <= base_rate:
            return None
        return {'rule': label, 'k': k, 'K': K,
                'rate': _num(rate, 3), 'base_rate': _num(base_rate, 4),
                'lift': _num(rate / base_rate, 2),
                'cover': _num(k / n, 2),
                'p': _num(_hyper_p(k, K, n, N), 6)}

    # 1단계
    singles = []
    for label, mask in conds:
        r = score(mask, label)
        if r:
            r['depth'] = 1
            singles.append((label, mask, r))
            found.append(r)

    # 2단계 — 1단계 상위끼리만 조합 (전부 조합하면 너무 느리다)
    if max_depth >= 2:
        singles.sort(key=lambda x: x[2]['lift'] or 0, reverse=True)
        head = singles[:14]
        for i in range(len(head)):
            for j in range(i + 1, len(head)):
                la, ma, _ = head[i]
                lb, mb, _ = head[j]
                if la.split('=')[0].split('<')[0] == lb.split('=')[0].split('<')[0]:
                    continue          # 같은 컬럼끼리는 조합하지 않는다
                r = score(ma & mb, f'{la} AND {lb}')
                if r:
                    r['depth'] = 2
                    found.append(r)

    if not found:
        return {'ok': True, 'tool': 'rule_search', 'items': [],
                'n_wafer': n, 'n_total': N,
                'note': '이상률이 뚜렷하게 높아지는 조건 조합을 찾지 못했습니다.'}

    q = _fdr([r['p'] for r in found])
    for r, qq in zip(found, q):
        r['q'] = _num(qq, 5)
        r['significant'] = bool(qq < 0.05 and (r['lift'] or 0) >= 2)

    found.sort(key=lambda r: (r['significant'], r['lift'] or 0), reverse=True)

    return {
        'ok': True, 'tool': 'rule_search',
        'n_wafer': n, 'n_total': N, 'base_rate': _num(base_rate, 4),
        'n_rule': len(found),
        'confidence': 'medium' if n < 10 else 'high',
        'items': found[:top],
        'method': '조건 조합별 이상률 (초기하 검정 + FDR)',
        'note': ('rate 는 그 조건을 만족하는 웨이퍼 중 이상 비율이고, '
                 'lift 는 전체 이상률 대비 몇 배인지입니다. '
                 'cover 는 전체 이상 웨이퍼 중 이 규칙이 설명하는 비율입니다. '
                 '표본이 적어도 유효하지만, 조건이 많을수록 우연히 맞을 수 '
                 '있으므로 significant 인 것만 신뢰하세요.'),
    }


TOOLS = {
    'compare_groups': compare_groups,
    'feature_importance': feature_importance,
    'interaction': interaction,
    'distribution': distribution,
    'timeline': timeline,
    # 소표본 전용 — 이상 웨이퍼가 몇 장뿐일 때
    'wafer_fingerprint': wafer_fingerprint,
    'common_traits': common_traits,
    'rule_search': rule_search,
}


def tool_specs():
    """OpenAI function calling 형식의 도구 정의"""
    common = {
        'oper_id': {'type': 'string', 'description': '데이터 소스 키'},
        'lot_cd': {'type': 'string', 'description': 'LOT_CD 로 좁힐 때'},
        'span_a': {'type': 'integer',
                   'description': '분석할 이슈 구간 번호 (0부터)'},
    }
    return [
        # ── 소표본 우선 ──────────────────────────────
        #   이상 웨이퍼가 적을 때는 이 셋을 먼저 쓴다.
        {'type': 'function', 'function': {
            'name': 'common_traits',
            'description': (
                '이상 웨이퍼들이 공유하는 조건을 찾는다 — 같은 챔버인가, '
                '같은 시간대인가, 같은 랏인가. '
                '★ 표본이 적을 때(웨이퍼 2~30장) 가장 먼저 쓸 도구다. '
                '평균 차이가 아니라 "우연히 이만큼 몰릴 확률" 을 보므로 '
                '몇 장뿐이어도 유효하다.'),
            'parameters': {'type': 'object', 'properties': {
                **common,
                'top': {'type': 'integer', 'description': '상위 몇 개 (기본 15)'},
            }, 'required': ['oper_id']}}},

        {'type': 'function', 'function': {
            'name': 'wafer_fingerprint',
            'description': (
                '이상 웨이퍼를 한 장씩 보고 어느 파라미터에서 얼마나 '
                '벗어났는지 낸다. ★ 1장이어도 쓸 수 있다. '
                '나머지 웨이퍼의 중앙값·MAD 를 기준으로 하므로 '
                '이상치가 기준을 오염시키지 않는다. '
                'shared 항목은 여러 장에서 함께 벗어난 파라미터다.'),
            'parameters': {'type': 'object', 'properties': {
                **common,
                'min_z': {'type': 'number',
                          'description': '이상으로 볼 기준 (기본 2.0)'},
                'top': {'type': 'integer', 'description': '웨이퍼 몇 장까지'},
            }, 'required': ['oper_id']}}},

        {'type': 'function', 'function': {
            'name': 'rule_search',
            'description': (
                '조건 조합에서 이상률이 튀는 지점을 찾는다 — '
                '"챔버3 + 압력 상위 25%" 같은 규칙. '
                '단일 조건으로는 안 갈리는데 조합에서만 나타나는 경우에 쓴다. '
                '표본이 적어도 유효하다.'),
            'parameters': {'type': 'object', 'properties': {
                **common,
                'top': {'type': 'integer', 'description': '상위 몇 개'},
            }, 'required': ['oper_id']}}},

        # ── 집단 비교 (표본이 충분할 때) ─────────────
        {'type': 'function', 'function': {
            'name': 'compare_groups',
            'description': '이슈 구간과 나머지(또는 다른 구간)를 전 파라미터에 '
                           '대해 비교한다. 효과크기 순으로 후보를 좁힐 때 먼저 쓴다.',
            'parameters': {'type': 'object', 'properties': {
                **common,
                'span_b': {'type': 'integer',
                           'description': '비교 대상 구간. 생략하면 나머지 전체'},
                'top': {'type': 'integer', 'description': '상위 몇 개 (기본 20)'},
            }, 'required': ['oper_id']}}},
        {'type': 'function', 'function': {
            'name': 'feature_importance',
            'description': '구간 여부를 맞히는 모델을 세워 변수 중요도를 매긴다. '
                           '변수들이 함께 작용하는 경우를 잡는다.',
            'parameters': {'type': 'object', 'properties': {
                **common, 'top': {'type': 'integer'}}, 'required': ['oper_id']}}},
        {'type': 'function', 'function': {
            'name': 'interaction',
            'description': '장비·챔버 같은 범주형과 연속 파라미터의 교호작용을 '
                           '찾는다. 범주마다 기울기가 다른지 본다.',
            'parameters': {'type': 'object', 'properties': {
                **common,
                'target': {'type': 'string',
                           'description': '종속변수. 생략하면 구간 여부'},
                'top': {'type': 'integer'}}, 'required': ['oper_id']}}},
        {'type': 'function', 'function': {
            'name': 'distribution',
            'description': '특정 파라미터를 장비·챔버별로 쪼개 평균·산포를 본다.',
            'parameters': {'type': 'object', 'properties': {
                **common,
                'param': {'type': 'string', 'description': '볼 파라미터'},
                'by': {'type': 'string', 'description': '구분 기준 컬럼'}},
                'required': ['oper_id', 'param']}}},
        {'type': 'function', 'function': {
            'name': 'timeline',
            'description': '파라미터의 시간 변화를 요약한다. 언제부터 달라졌는지 볼 때.',
            'parameters': {'type': 'object', 'properties': {
                **common, 'param': {'type': 'string'}},
                'required': ['oper_id', 'param']}}},
    ]


def run_tool(name, args, spans):
    """
    도구 실행. spans 는 화면이 정의한 이슈 구간이라 LLM 이 못 바꾼다.

    ★ LLM 이 넘긴 인자를 그대로 믿지 않는다 — 컬럼 이름이나 공정 ID 를
      지어내는 경우가 있어 형식을 검사한다.
    """
    fn = TOOLS.get(name)
    if not fn:
        return {'ok': False, 'error': f'모르는 도구입니다: {name}'}

    kw = dict(args or {})
    oper_id = kw.pop('oper_id', None)
    if not _safe(oper_id):
        return {'ok': False, 'error': 'oper_id 형식 오류'}
    for k in ('param', 'by', 'target', 'lot_cd'):
        if k in kw and kw[k] is not None and not _safe(str(kw[k])):
            return {'ok': False, 'error': f'{k} 형식 오류'}

    try:
        return fn(oper_id, spans, **kw)
    except TypeError as e:
        return {'ok': False, 'error': f'인자 오류: {e}'}
    except Exception as e:
        traceback.print_exc()
        return {'ok': False, 'error': f'{e.__class__.__name__}: {e}'}
