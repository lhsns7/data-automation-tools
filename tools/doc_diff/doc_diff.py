#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""doc_diff.py — 문서 버전 비교 리포트 (조항 단위·수치 변경 추출·이동 오보 방지)

계약서·규정·약관 개정 시 "뭐가 바뀌었나"를 **사람이 읽는 리포트**로 만든다. 개발자용 diff는
비개발자가 못 읽고, 눈 대조는 수치 하나(위약금 3%→5%)를 놓친다 — 그 간극이 이 도구의 자리다.

설계:
  - 조항 단위 분할: '제N조(제목)' 헤더 기준 블록화(패턴 설정식)
  - 분류 5종: 추가 / 삭제 / 수정 / ★이동 / 동일
    ★이동 = 내용은 그대로, 위치만 바뀐 조항 — 이걸 '수정'이나 '삭제+추가'로 오보하면
    검토자가 없는 변경을 찾느라 시간을 태운다(오보 방지가 신뢰의 반)
  - 수정 조항 = 단어 수준 diff 하이라이트(추가=초록·삭제=빨강 취소선)
  - ★핵심 수치 변경 표: 금액·%·일수·기간의 변경을 별도 표로(계약 검토에서 제일 중요한 것)
  - 산출: 색상 HTML 리포트 + 조항 수 대사(v1 = 동일+수정+이동+삭제 / v2 = 동일+수정+이동+추가)

검증(--make-demo) = 정답지 선작성: v1(12조) → 기지 변경으로 v2 합성(수정 3·추가 1·삭제 1·이동 2)
  ①분류 전수 일치 ②이동 2건을 수정/삭제+추가로 오보 0 ③수치 변경 추출 정확(3%→5%, 7일→14일)
  ④단어 하이라이트 존재+무변경 단어 미표시 ⑤동일 조항 오탐 0 ⑥조항 수 대사+재현성.
※ 입력 = 텍스트/마크다운(docx·hwp는 텍스트 추출 후 — 별도 단계 명시). 조항 패턴 = 설정값.
"""
import os, sys, re, html, difflib, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
CLAUSE_PAT = r'^(제\s*\d+\s*조\s*(?:\([^)]*\))?)'          # 조항 헤더(설정값)
NUM_PAT = r'\d+(?:\.\d+)?\s*(?:%|퍼센트|일(?!자)|개월|년|원|만\s*원|억\s*원|회)'


def split_clauses(text):
    """→ [(헤더, 본문)] — 헤더 앞 전문(전문/서문)은 '(전문)'으로."""
    parts = re.split(CLAUSE_PAT, text, flags=re.M)
    out = []
    if parts[0].strip():
        out.append(('(전문)', parts[0].strip()))
    for i in range(1, len(parts), 2):
        out.append((re.sub(r'\s+', ' ', parts[i].strip()),
                    parts[i + 1].strip() if i + 1 < len(parts) else ''))
    return out


def norm_body(b):
    return re.sub(r'\s+', ' ', b).strip()


def word_diff(old, new):
    """단어 수준 diff → [(op, 단어)] op = '='/'-'/'+'"""
    a, b = old.split(), new.split()
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes():
        if op == 'equal':
            out += [('=', w) for w in a[i1:i2]]
        else:
            out += [('-', w) for w in a[i1:i2]]
            out += [('+', w) for w in b[j1:j2]]
    return out


def num_changes(old, new):
    """수치 토큰 변경 추출 → [(이전, 이후)] — 제거·추가 토큰을 순서대로 짝지음"""
    o = re.findall(NUM_PAT, norm_body(old))
    n = re.findall(NUM_PAT, norm_body(new))
    removed = [x for x in o if o.count(x) > n.count(x)]
    added = [x for x in n if n.count(x) > o.count(x)]
    return list(zip([re.sub(r'\s+', '', x) for x in removed],
                    [re.sub(r'\s+', '', x) for x in added]))


def compare(text_a, text_b):
    """→ dict(items=[(헤더, 판정, old, new)], counts, num_table)
    판정 = 동일/수정/이동/추가/삭제. 이동 = 내용 동일 + 공통 순서(LCS) 밖."""
    A, B = split_clauses(text_a), split_clauses(text_b)
    a_map = {h: (i, b) for i, (h, b) in enumerate(A)}
    b_map = {h: (i, b) for i, (h, b) in enumerate(B)}
    common = [h for h, _ in A if h in b_map]
    # 공통 조항의 v2 순서 인덱스열에서 LIS(최장 증가 부분열) = 제자리 조항, 밖 = 이동
    seq = [b_map[h][0] for h in common]
    lis_idx = _lis_indices(seq)
    in_place = {common[i] for i in lis_idx}
    items, num_table = [], []
    counts = {'동일': 0, '수정': 0, '이동': 0, '추가': 0, '삭제': 0}
    for h, body in B:                                       # v2 순서 기준 표시
        if h not in a_map:
            counts['추가'] += 1
            items.append((h, '추가', '', body))
            continue
        old = a_map[h][1]
        same = (norm_body(old) == norm_body(body))
        if same and h in in_place:
            counts['동일'] += 1
            items.append((h, '동일', old, body))
        elif same:
            counts['이동'] += 1
            items.append((h, '이동', old, body))
        else:
            counts['수정'] += 1
            items.append((h, '수정', old, body))
            for pair in num_changes(old, body):
                num_table.append((h,) + pair)
    for h, body in A:
        if h not in b_map:
            counts['삭제'] += 1
            items.append((h, '삭제', body, ''))
    return dict(items=items, counts=counts, num_table=num_table,
                n_a=len(A), n_b=len(B))


def _lis_indices(seq):
    """최장 증가 부분열의 인덱스 집합(O(n^2) — 조항 수 규모에 충분)"""
    if not seq:
        return set()
    L = [1] * len(seq)
    prev = [-1] * len(seq)
    for i in range(len(seq)):
        for j in range(i):
            if seq[j] < seq[i] and L[j] + 1 > L[i]:
                L[i], prev[i] = L[j] + 1, j
    k = max(range(len(seq)), key=lambda i: L[i])
    out = set()
    while k != -1:
        out.add(k)
        k = prev[k]
    return out


def write_html(res, out, title='문서 개정 비교 리포트'):
    css = """<style>body{font-family:'Malgun Gothic',sans-serif;max-width:860px;margin:24px auto;color:#14181f}
h1{font-size:22px} .sum{color:#5b6472;font-size:14px;margin-bottom:14px}
table{border-collapse:collapse;width:100%;margin:10px 0}td,th{border:1px solid #dfe4ea;padding:7px 10px;font-size:13.5px;text-align:left}
th{background:#f2f4f8}.cl{border:1px solid #dfe4ea;border-radius:10px;padding:12px 16px;margin:10px 0}
.cl h3{margin:0 0 6px;font-size:15px}.b{font-size:13.5px;line-height:1.7;white-space:pre-wrap}
.tag{display:inline-block;font-size:11.5px;font-weight:800;padding:2px 9px;border-radius:999px;margin-left:8px}
.t동일{background:#eef1f5;color:#5b6472}.t수정{background:#fff3e0;color:#b45309}.t이동{background:#e8f0fe;color:#1e40af}
.t추가{background:#e6f6ec;color:#15803d}.t삭제{background:#fde8e8;color:#b4232c}
ins{background:#d7f5df;text-decoration:none}del{background:#fbd9d9}</style>"""
    parts = [css, f'<h1>{html.escape(title)}</h1>']
    c = res['counts']
    parts.append(f"<div class=sum>조항 {res['n_a']}→{res['n_b']} · 수정 {c['수정']} · 추가 {c['추가']}"
                 f" · 삭제 {c['삭제']} · 이동 {c['이동']} · 동일 {c['동일']}"
                 f" — 생성 {dt.datetime.now():%Y-%m-%d %H:%M}</div>")
    if res['num_table']:
        parts.append('<h2 style="font-size:17px">★핵심 수치 변경</h2><table><tr><th>조항</th><th>이전</th><th>이후</th></tr>')
        parts += [f'<tr><td>{html.escape(h)}</td><td>{html.escape(a)}</td><td><b>{html.escape(b)}</b></td></tr>'
                  for h, a, b in res['num_table']]
        parts.append('</table>')
    for h, tag, old, new in res['items']:
        if tag == '동일':
            continue                                        # 리포트엔 변경만(동일은 요약 수치로)
        parts.append(f'<div class=cl><h3>{html.escape(h)}<span class="tag t{tag}">{tag}</span></h3><div class=b>')
        if tag == '수정':
            buf = []
            for op, w in word_diff(norm_body(old), norm_body(new)):
                e = html.escape(w)
                buf.append(f'<del>{e}</del>' if op == '-' else (f'<ins>{e}</ins>' if op == '+' else e))
            parts.append(' '.join(buf))
        elif tag in ('추가', '이동'):
            parts.append(html.escape(norm_body(new)))
        else:
            parts.append(f'<del>{html.escape(norm_body(old))}</del>')
        parts.append('</div></div>')
    open(out, 'w', encoding='utf-8').write('\n'.join(parts))
    return out


# ── 검증 데모: 정답지 선작성 ───────────────────────────────────────
def make_demo():
    cl = {i: f'제{i}조(조항{i}) 본 조항 {i}의 기본 내용은 유지 관리 대상이며 표준 절차를 따른다.'
          for i in range(1, 13)}
    cl[3] = '제3조(위약금) 계약 해지 시 위약금은 총액의 3% 로 하며 통지 후 7일 이내 납부한다.'
    cl[5] = '제5조(대금 지급) 대금은 검수 완료 후 30일 이내 지급하며 지연 시 이자를 가산한다.'
    cl[8] = '제8조(비밀 유지) 양 당사자는 계약 기간 및 종료 후 2년 간 비밀을 유지한다.'
    v1 = '표준 용역 계약서 (v1)\n\n' + '\n\n'.join(cl[i] for i in range(1, 13))

    c2 = dict(cl)
    # 수정 3 (수치 변경 2 + 문구 변경 1)
    c2[3] = c2[3].replace('3% ', '5% ').replace('7일', '14일')          # 제3조: 3%→5%, 7일→14일
    c2[5] = c2[5].replace('30일', '45일')                                # 제5조: 30일→45일
    c2[8] = c2[8].replace('비밀을 유지한다', '비밀을 유지하며 위반 시 손해를 배상한다')  # 문구 변경
    del c2[11]                                                           # 삭제: 제11조
    order = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12]
    order.remove(9); order.insert(1, 9)                                  # 이동: 제9조 → 앞으로
    order.remove(2); order.append(2)                                     # 이동: 제2조 → 맨 뒤로
    v2 = ('표준 용역 계약서 (v2)\n\n' + '\n\n'.join(c2[i] for i in order)
          + '\n\n제13조(분쟁 해결) 분쟁은 상호 협의로 해결하며 협의 불성립 시 중재를 따른다.')  # 추가
    answer = {f'제{i}조(조항{i})': '동일' for i in (1, 4, 6, 7, 10, 12)}
    answer.update({'제3조(위약금)': '수정', '제5조(대금 지급)': '수정', '제8조(비밀 유지)': '수정',
                   '제9조(조항9)': '이동', '제2조(조항2)': '이동',
                   '제13조(분쟁 해결)': '추가', '제11조(조항11)': '삭제', '(전문)': '수정'})
    # (전문) v1/v2 문구가 다름(v1→v2 표기) = 수정으로 잡히는 게 정직
    return v1, v2, answer


def main_demo():
    v1, v2, answer = make_demo()
    open(os.path.join(HERE, 'demo_v1.txt'), 'w', encoding='utf-8').write(v1)
    open(os.path.join(HERE, 'demo_v2.txt'), 'w', encoding='utf-8').write(v2)
    res = compare(v1, v2)
    res2 = compare(v1, v2)

    got = {h: tag for h, tag, _, _ in res['items']}
    # ① 분류 전수 일치(정답지)
    mism = {h: (answer.get(h), got.get(h)) for h in set(answer) | set(got)
            if answer.get(h) != got.get(h)}
    ok1 = (not mism)
    # ② 이동 오보 0: 이동 2건이 정확히 이동(수정/추가/삭제 아님)
    ok2 = (got.get('제9조(조항9)') == '이동' and got.get('제2조(조항2)') == '이동'
           and res['counts']['이동'] == 2)
    # ③ 수치 변경 추출: 3%→5%, 7일→14일, 30일→45일
    nt = {(a, b) for _, a, b in res['num_table']}
    ok3 = ({('3%', '5%'), ('7일', '14일'), ('30일', '45일')} <= nt)
    # ④ 단어 하이라이트: 제8조 수정에서 '배상한다' 추가 표시 + 무변경 단어 '양' 미하이라이트
    wd = word_diff(norm_body(dict((h, o) for h, t, o, n in res['items'])['제8조(비밀 유지)']),
                   norm_body(dict((h, n) for h, t, o, n in res['items'])['제8조(비밀 유지)']))
    ok4 = (any(op == '+' and '배상한다' in w for op, w in wd)
           and all(op == '=' for op, w in wd if w == '양'))
    # ⑤ 동일 판정 오탐 0(동일로 분류된 조항은 실제 본문 동일)
    ok5 = all(norm_body(o) == norm_body(n) for h, t, o, n in res['items'] if t == '동일')
    # ⑥ 조항 수 대사 + 재현성
    c = res['counts']
    ok6 = (res['n_a'] == c['동일'] + c['수정'] + c['이동'] + c['삭제']
           and res['n_b'] == c['동일'] + c['수정'] + c['이동'] + c['추가']
           and res == res2)

    out = os.path.join(HERE, 'diff_report_demo.html')
    write_html(res, out)

    L = [f'# 문서 비교 검증 리포트 ({dt.datetime.now():%Y-%m-%d %H:%M})',
         '- 데모 = 계약서 12조 v1 → **정답지를 먼저 쓰고** v2 합성(수정 3·추가 1·삭제 1·★이동 2) → 판정 대조',
         '', '| 검증 | 결과 |', '|---|---|',
         f'| ① 조항 분류 전수 일치({len(answer)}항목) | 불일치 {len(mism)} → {"PASS" if ok1 else "★FAIL: " + str(mism)} |',
         f'| ② ★이동 2건 오보 0(수정/삭제+추가로 안 잡음) | {"PASS" if ok2 else "★FAIL"} |',
         f'| ③ 핵심 수치 변경 추출(3%→5% · 7일→14일 · 30일→45일) | {"PASS" if ok3 else "★FAIL"} |',
         f'| ④ 단어 하이라이트(추가 문구 표시·무변경 단어 미표시) | {"PASS" if ok4 else "★FAIL"} |',
         f'| ⑤ 동일 판정 오탐 0 | {"PASS" if ok5 else "★FAIL"} |',
         f'| ⑥ 조항 수 대사(v1={res["n_a"]}·v2={res["n_b"]}) + 재현성 | {"PASS" if ok6 else "★FAIL"} |',
         f'| 산출 | diff_report_demo.html (색상 리포트 — 비개발자용) |',
         '', '- ※ 입력 = 텍스트/마크다운. docx·hwp = 텍스트 추출 단계 별도(명시). 조항 헤더 패턴 = 설정값.',
         '- ※ 이동 오보 방지 = 공통 조항 순서의 최장 증가 부분열(LIS) 밖 = 이동 — 없는 변경을 찾게 만들지 않는다.']
    rep = os.path.join(HERE, 'doc_diff_verify.md')
    open(rep, 'w', encoding='utf-8').write('\n'.join(L))
    print('\n'.join(L))
    return ok1 and ok2 and ok3 and ok4 and ok5 and ok6


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    if len(sys.argv) >= 3 and not sys.argv[1].startswith('-'):
        # 실사용: python doc_diff.py 구버전.txt 신버전.txt [출력.html]
        a = open(sys.argv[1], encoding='utf-8').read()
        b = open(sys.argv[2], encoding='utf-8').read()
        out = sys.argv[3] if len(sys.argv) > 3 else os.path.join(HERE, 'diff_report.html')
        res = compare(a, b)
        write_html(res, out)
        c = res['counts']
        print(f'수정 {c["수정"]} · 추가 {c["추가"]} · 삭제 {c["삭제"]} · 이동 {c["이동"]} · 동일 {c["동일"]} → {out}')
        sys.exit(0)
    ok = main_demo()
    sys.exit(0 if ok else 1)
