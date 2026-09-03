#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rag_audit.py — RAG/챗봇 품질 진단 도구

문서 Q&A/RAG 시스템을 **블랙박스로 물려** 평가셋을 돌리고, 품질을 축별로 진단한다.

진단 4축 (블랙박스 — 내부 인덱스를 열지 않고 응답만 본다):
  ① 검색: 답변의 근거 출처가 기대 문서와 일치하는가 (hit@1)
  ② 정답: 답변에 기대 키워드가 있는가 / ★오답(답을 냈는데 틀림)은 몇 건인가
  ③ 환각 방지: 문서에 없는 질문을 거절하는가 (없으면 지어내는 시스템)
  ④ 일관성: 같은 질문 2회 = 같은 답인가

처방(규칙 기반·결정적):
  - 범위밖 거절 미달 → 환각 축 (거절 임계·근거 검증 도입)
  - 실패가 1~2개 문서에 집중 → 커버리지 축 (해당 문서 인덱싱 확인)
  - 실패가 여러 영역에 분산 + 근거 오선택 → 검색층 축 (청킹·검색 개선)

대상 어댑터: ask(question) -> {found, answer, source, score}
  - 함수(파이썬 객체) 또는 HTTP JSON 엔드포인트(rag_qa --serve 형식) 1회 맞춤.

검증(--make-demo) = planted-degradation: 건강판(A) + 열화를 알고 심은 3판(B 문서삭제,
C 거절제거=환각, D 검색셔플)을 같은 평가셋으로 진단 → **진단기가 각 열화를 그 축에서만 잡는지** 대조.
"""
import os, sys, json, shutil, random, zlib, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'rag_qa'))


# ── 평가셋 파일 규격 (고객 평가셋도 같은 JSON) ─────────────────────
def load_evalset(path):
    d = json.load(open(path, encoding='utf-8'))
    return [(r['q'], r['doc'], r['keywords']) for r in d['eval']], d['oos']


# ── 대상 어댑터 ─────────────────────────────────────────────────────
def http_ask(url):
    """rag_qa --serve 형식(/ask?q=) 어댑터. 고객 API는 이 함수만 1회 맞춤."""
    import urllib.request, urllib.parse
    def ask(q):
        with urllib.request.urlopen(f'{url.rstrip("/")}/ask?q={urllib.parse.quote(q)}', timeout=10) as r:
            return json.load(r)
    return ask


# ── 진단 엔진 ───────────────────────────────────────────────────────
def audit(ask, eval_rows, oos, name):
    rows = []
    for q, doc, kws in eval_rows:
        r1, r2 = ask(q), ask(q)
        src = (r1.get('source') or '').split('#')[0]
        found = bool(r1.get('found'))
        kw = found and any(k in (r1.get('answer') or '') for k in kws)
        rows.append(dict(q=q, doc=doc, top1=src if found else '-', found=found,
                         h1=found and src == doc, kw=kw, wrong=found and not kw,
                         refused=not found, score=r1.get('score'),
                         consistent=(r1.get('answer') == r2.get('answer') and r1.get('found') == r2.get('found')),
                         ans=(r1.get('answer') or '')[:40]))
    oos_rows = []
    for q in oos:
        r1, r2 = ask(q), ask(q)
        oos_rows.append(dict(q=q, refused=not r1.get('found'), score=r1.get('score'),
                             consistent=(r1.get('answer') == r2.get('answer'))))
    n, n_oos = len(rows), len(oos_rows)
    res = dict(name=name, rows=rows, oos=oos_rows, n=n, n_oos=n_oos,
               hit1=sum(r['h1'] for r in rows), kw=sum(r['kw'] for r in rows),
               wrong=sum(r['wrong'] for r in rows), refused_in=sum(r['refused'] for r in rows),
               oos_refuse=sum(r['refused'] for r in oos_rows),
               consistent=all(r['consistent'] for r in rows) and all(r['consistent'] for r in oos_rows))
    res['diagnosis'] = diagnose(res)
    return res


def diagnose(res):
    """규칙 기반 처방 — 결정적이라 검증 가능. (실패 = 정답 키워드를 못 낸 인스코프 질문)
    ★설계 교훈(1차 검증 검거): 기저 노이즈(임계 부근 정직 거절 1~2건)가 섞이면 '문서 수 ≤2' 같은
    순진한 집중도 판정이 무너진다 → 집중도 = 상위 2개 문서 점유율 80% + 최소 실패 3건 기준."""
    fails = [r for r in res['rows'] if not r['kw']]
    if res['oos_refuse'] < res['n_oos']:
        made_up = res['n_oos'] - res['oos_refuse']
        return ('환각 방지', f'범위밖 질문 {made_up}/{res["n_oos"]}건에 답을 만들어냄 — '
                f'거절 임계·근거 검증(문서에 없으면 거절)을 도입해야 합니다.')
    if not res['consistent']:
        return ('일관성', '같은 질문에 다른 답 — 랜덤 요소·상태 의존을 제거해야 합니다.')
    if len(fails) >= 3:
        cnt = {}
        for r in fails:
            cnt[r['doc']] = cnt.get(r['doc'], 0) + 1
        top2 = sorted(cnt.values(), reverse=True)[:2]
        top2_docs = sorted(cnt, key=cnt.get, reverse=True)[:2]
        if sum(top2) / len(fails) >= 0.8:
            return ('커버리지', f'실패 {len(fails)}건 중 {sum(top2)}건이 {", ".join(top2_docs)} 영역에 집중 — '
                    f'해당 문서가 인덱스에 실제로 들어갔는지(존재·인코딩·파싱)부터 확인해야 합니다.')
        return ('검색층', f'실패 {len(fails)}건이 {len(cnt)}개 영역에 분산'
                + (f' + 오답 {res["wrong"]}건(근거 오선택)' if res['wrong'] else '')
                + ' — 검색(청킹·질의 확장·랭킹) 개선이 필요합니다.')
    if fails:
        return ('경미', f'잔여 실패 {len(fails)}건(임계 부근 정직 거절 등) — 구조 결함 신호 없음. '
                f'질의 확장 사전·임계 재조정 검토 대상으로만 기록합니다.')
    return ('건강', '4축 모두 기준 통과 — 현 수준 유지, 평가셋을 주기 회귀로 돌리길 권합니다.')


def write_report(res, path):
    """고객용 진단 리포트 (단일 시스템)."""
    ax, msg = res['diagnosis']
    L = [f'# 챗봇 품질 진단 리포트 — {res["name"]} ({dt.datetime.now():%Y-%m-%d %H:%M})',
         '',
         f'| 축 | 측정 | 결과 |', '|---|---|---|',
         f'| ① 검색(근거 문서 적중) | hit@1 | **{res["hit1"]}/{res["n"]}** |',
         f'| ② 정답(키워드 포함) | 정답/오답/거절 | **{res["kw"]}/{res["n"]}** · ★오답 {res["wrong"]} · 거절 {res["refused_in"]} |',
         f'| ③ 환각 방지(범위밖 거절) | 거절률 | **{res["oos_refuse"]}/{res["n_oos"]}** |',
         f'| ④ 일관성(2회 동일) | 동일 여부 | **{"OK" if res["consistent"] else "★불일치"}** |',
         '',
         f'## 진단: ★{ax}',
         f'- {msg}',
         '', '## 질문별 상세', '| 질문 | 기대문서 | 응답근거 | 적중 | 정답 | 답변 |', '|---|---|---|---|---|---|']
    for r in res['rows']:
        L.append(f'| {r["q"]} | {r["doc"]} | {r["top1"]} | {"O" if r["h1"] else "X"} | '
                 f'{"O" if r["kw"] else ("거절" if r["refused"] else "★오답")} | {r["ans"]} |')
    L += ['', '| 범위밖 질문 | 거절? |', '|---|---|']
    L += [f'| {r["q"]} | {"O" if r["refused"] else "★답 만들어냄"} |' for r in res['oos']]
    L += ['', '※ 블랙박스 진단 — 시스템 내부를 열지 않고 응답(답변·근거·거절)만으로 측정. '
          '평가셋은 고객 문서 기준으로 함께 작성합니다.']
    open(path, 'w', encoding='utf-8').write('\n'.join(L))
    return path


# ── 데모: 열화를 알고 심은 3판 + 건강판 ────────────────────────────
def make_demo():
    import rag_qa

    # 데모 문서 + 평가셋 파일 규격 실증 (rag_qa 데모 재사용 → JSON으로 저장→로드)
    docs_a = os.path.join(HERE, 'demo_target_A')
    os.makedirs(docs_a, exist_ok=True)
    for name, text in rag_qa.DEMO_DOCS.items():
        open(os.path.join(docs_a, name + '.md'), 'w', encoding='utf-8').write(text)
    ev_path = os.path.join(HERE, 'eval_demo.json')
    json.dump({'eval': [{'q': q, 'doc': d, 'keywords': k} for q, d, k in rag_qa.EVAL],
               'oos': rag_qa.EVAL_OOS}, open(ev_path, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    eval_rows, oos = load_evalset(ev_path)

    # A 건강판
    idx_a = rag_qa.RagIndex(docs_a)

    # B 문서 누락판: 2개 문서 삭제(운영보안·경비지출) → 그 영역 질문 5건이 기지 실패 대상
    DEL = ['운영보안매뉴얼', '경비지출규정']
    docs_b = os.path.join(HERE, 'demo_target_B')
    if os.path.isdir(docs_b):
        shutil.rmtree(docs_b)
    os.makedirs(docs_b)
    for name in rag_qa.DEMO_DOCS:
        if name not in DEL:
            shutil.copy(os.path.join(docs_a, name + '.md'), docs_b)
    idx_b = rag_qa.RagIndex(docs_b)
    planted_b = [q for q, d, _ in eval_rows if d in DEL]          # 기대 실패 목록(기지)

    # C 환각판: 거절 임계 제거 → 범위밖에도 답을 만들어냄
    def ask_c(q):
        old = rag_qa.REFUSE_TH
        rag_qa.REFUSE_TH = -1.0
        try:
            return idx_a.ask(q)
        finally:
            rag_qa.REFUSE_TH = old

    # D 검색 열화판: 점수는 실제, 순위만 셔플(질문별 고정 시드=재현) → 근거 오선택
    class ShuffledIndex(rag_qa.RagIndex):
        def search(self, query, k=3):
            real = super().search(query, k=10)
            sims = [s for s, _ in real]
            chunks = [c for _, c in real]
            random.Random(zlib.crc32(query.encode())).shuffle(chunks)
            return list(zip(sims, chunks))[:k]
    idx_d = ShuffledIndex(docs_a)

    A = audit(idx_a.ask, eval_rows, oos, 'A 건강판(원본)')
    B = audit(idx_b.ask, eval_rows, oos, 'B 문서 누락판(2문서 삭제)')
    C = audit(ask_c, eval_rows, oos, 'C 환각판(거절 제거)')
    D = audit(idx_d.ask, eval_rows, oos, 'D 검색 열화판(순위 셔플)')
    A2 = audit(idx_a.ask, eval_rows, oos, 'A 재실행')             # 진단기 자체 재현성

    # ── 검증 대조 (심은 열화 vs 진단) ──
    a_failed = set(r['q'] for r in A['rows'] if not r['kw'])       # 기저 실패(임계 부근 정직 거절)
    b_failed = set(r['q'] for r in B['rows'] if not r['kw'])
    b_expected_ok = all(r['kw'] == a['kw'] and r['h1'] == a['h1']
                        for r, a in zip(B['rows'], A['rows']) if r['doc'] not in DEL)
    checks = [
        ('① A 기준선 = rag_qa 자체 검증과 일치 + 처방 무결(오진 없음)',
         A['wrong'] == 0 and A['oos_refuse'] == 4 and A['kw'] == 14 and A['refused_in'] == 1
         and A['diagnosis'][0] in ('건강', '경미'),
         f"오답 {A['wrong']}·거절 {A['oos_refuse']}/4·정답 {A['kw']}/15·정직거절 {A['refused_in']}·처방 {A['diagnosis'][0]} "
         f"(h@1 {A['hit1']}/15 — 거절 1건은 근거 미표시라 블랙박스에선 미적중 집계)"),
        ('② B 실패 = 심은 5문항(삭제 문서) ∪ 기저 실패 + 비삭제 영역은 A와 동일',
         b_failed == set(planted_b) | a_failed and b_expected_ok,
         f"실패 {len(b_failed)}건 = 심은 {len(planted_b)} ∪ 기저 {len(a_failed)} · 비삭제 영역 무변화 {b_expected_ok}"),
        ('③ B 처방 = 커버리지 (기저 노이즈 섞여도 집중도 80%로 판별)',
         B['diagnosis'][0] == '커버리지', B['diagnosis'][0]),
        ('④ C: 범위밖 거절 0/4 검출 + 정상 질문 축은 A와 동일',
         C['oos_refuse'] == 0 and C['hit1'] >= A['hit1'] and C['kw'] >= A['kw'],
         f"거절 {C['oos_refuse']}/4 · hit@1 {C['hit1']} · 정답 {C['kw']}"),
        ('⑤ C 처방 = 환각 방지', C['diagnosis'][0] == '환각 방지', C['diagnosis'][0]),
        ('⑥ D: 검색 축 붕괴 검출(오답 발생·적중 급락) + 실패 분산',
         D['hit1'] <= A['hit1'] - 5 and D['wrong'] >= 1,
         f"hit@1 {D['hit1']}/15 (A {A['hit1']}) · 오답 {D['wrong']}"),
        ('⑦ D 처방 = 검색층', D['diagnosis'][0] == '검색층', D['diagnosis'][0]),
        ('⑧ 진단기 재현성(A 2회 동일)',
         (A['hit1'], A['kw'], A['wrong'], A['oos_refuse']) == (A2['hit1'], A2['kw'], A2['wrong'], A2['oos_refuse']),
         '2회 측정 동일'),
    ]
    ok_all = all(ok for _, ok, _ in checks)

    # 고객용 샘플 리포트(문제 있는 시스템 D — 처방까지 나가는 실물)
    write_report(D, os.path.join(HERE, 'audit_sample_report.md'))

    L = [f'# RAG 진단 도구 검증 리포트 ({dt.datetime.now():%Y-%m-%d %H:%M})',
         '- 방식 = **planted-degradation**: 열화를 알고 심은 시스템 3판 + 건강판을 같은 평가셋(15문+범위밖 4)으로 진단 →'
         ' 진단기가 각 열화를 **그 축에서만** 잡고 **처방까지 정확**한지 대조',
         '', '## 4개 시스템 진단 매트릭스',
         '| 시스템 | hit@1 | 정답 | ★오답 | 범위밖 거절 | 진단(처방 축) |', '|---|---|---|---|---|---|']
    for r in (A, B, C, D):
        L.append(f'| {r["name"]} | {r["hit1"]}/{r["n"]} | {r["kw"]}/{r["n"]} | {r["wrong"]} | '
                 f'{r["oos_refuse"]}/{r["n_oos"]} | ★{r["diagnosis"][0]} |')
    L += ['', '## 검증 대조', '| 검증 | 결과 | 상세 |', '|---|---|---|']
    for name, ok, detail in checks:
        L.append(f'| {name} | {"PASS" if ok else "★FAIL"} | {detail} |')
    L += ['', f'- 산출: `audit_sample_report.md` (D판 고객용 리포트 실물) · `eval_demo.json` (평가셋 파일 규격)',
          '- ※ 정직선: 블랙박스 진단(내부 미열람·응답만 측정) · 일관성 열화판은 미포함(4판 전부 결정적=OK가 기대값) ·'
          ' 고객 시스템에는 고객 문서 기준 평가셋을 함께 작성해 같은 리포트를 냅니다.']
    rep = os.path.join(HERE, 'rag_audit_verify.md')
    open(rep, 'w', encoding='utf-8').write('\n'.join(L))
    print('\n'.join(L))
    return ok_all


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    if '--target' in sys.argv:      # 실대상 진단: --target http://127.0.0.1:8765 --evalset eval.json
        url = sys.argv[sys.argv.index('--target') + 1]
        ev = sys.argv[sys.argv.index('--evalset') + 1]
        eval_rows, oos = load_evalset(ev)
        res = audit(http_ask(url), eval_rows, oos, url)
        out = os.path.join(HERE, 'audit_report.md')
        write_report(res, out)
        print(f'진단 완료 → {out} (★{res["diagnosis"][0]})')
    else:
        ok = make_demo()
        sys.exit(0 if ok else 1)
