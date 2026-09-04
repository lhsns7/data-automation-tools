#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""data_gate.py — 데이터 품질 게이트 (규칙 검사 → 통과해야 납품·적재)

데이터를 주고받는 모든 곳(납품·적재·이관·취합)의 관문. "일단 넣고 나중에 발견"이 아니라
**규칙을 통과해야 지나가는 게이트**로 세운다. 규칙은 선언형(설정) — 코드 수정 없이 대상만 바꾼다.

규칙 8종(컬럼별 선언):
  type(int/float/date/str) · required(필수) · min/max(범위) · regex(형식) · enum(허용값) ·
  unique(중복 금지) · ref(다른 파일 키 존재 = ★참조 무결성) · 그리고 파일 수준 row_count(최소 행수)
산출 = 위반 목록(행 번호·컬럼·값·어긴 규칙) + PASS/FAIL 게이트(종료 코드) + 리포트.

검증(--make-demo) = 정답 선작성: 위반 8종을 행 번호를 알고 심음 → ①전수 검출(행·규칙까지)
  ②클린 파일 위반 0(오탐 0) ③게이트 종료 코드 ④참조 무결성(끊긴 키) ⑤위반 수 = 심은 수 정확
  ⑥재현성. ※ 규칙 = 설정값(고객 데이터 계약에 1회 맞춤).
"""
import os, sys, csv, re, json, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))

RULES = {                                                  # 선언형 규칙(설정값)
    'file': dict(min_rows=5),
    'columns': {
        '주문번호': dict(required=True, regex=r'O\d{4}', unique=True),
        '고객ID': dict(required=True, ref='demo_customers.csv:고객ID'),
        '수량': dict(type='int', min=1, max=100),
        '금액': dict(type='int', min=0),
        '상태': dict(enum=['결제완료', '배송중', '완료', '취소']),
        '주문일': dict(type='date'),
    },
}


def _is_type(v, t):
    try:
        if t == 'int':
            int(str(v).replace(',', ''))
        elif t == 'float':
            float(str(v).replace(',', ''))
        elif t == 'date':
            dt.date.fromisoformat(str(v).strip())
        return True
    except (ValueError, TypeError):
        return False


def load_refs(rules, base_dir):
    refs = {}
    for col, r in rules['columns'].items():
        if 'ref' in r:
            path, key = r['ref'].split(':')
            rows = csv.DictReader(open(os.path.join(base_dir, path), encoding='utf-8-sig'))
            refs[col] = set(row[key].strip() for row in rows)
    return refs


def gate(csv_path, rules=RULES, base_dir=None):
    """→ (violations, n_rows). violation = (행, 컬럼, 값, 규칙)"""
    base_dir = base_dir or os.path.dirname(csv_path)
    refs = load_refs(rules, base_dir)
    rows = list(csv.DictReader(open(csv_path, encoding='utf-8-sig')))
    V = []
    seen = {c: {} for c, r in rules['columns'].items() if r.get('unique')}
    for i, row in enumerate(rows, 2):                      # 2 = 헤더 다음 실제 파일 행 번호
        for col, r in rules['columns'].items():
            v = (row.get(col) or '').strip()
            if not v:
                if r.get('required'):
                    V.append((i, col, '', 'required(필수값 없음)'))
                continue
            if 'type' in r and not _is_type(v, r['type']):
                V.append((i, col, v, f'type({r["type"]} 아님)'))
                continue
            if 'min' in r and _is_type(v, 'float') and float(v.replace(',', '')) < r['min']:
                V.append((i, col, v, f'min({r["min"]} 미만)'))
            if 'max' in r and _is_type(v, 'float') and float(v.replace(',', '')) > r['max']:
                V.append((i, col, v, f'max({r["max"]} 초과)'))
            if 'regex' in r and not re.fullmatch(r['regex'], v):
                V.append((i, col, v, f'regex({r["regex"]} 불일치)'))
            if 'enum' in r and v not in r['enum']:
                V.append((i, col, v, f'enum(허용값 밖)'))
            if r.get('unique'):
                if v in seen[col]:
                    V.append((i, col, v, f'unique(행 {seen[col][v]}와 중복)'))
                else:
                    seen[col][v] = i
            if col in refs and v not in refs[col]:
                V.append((i, col, v, f'ref(참조 대상에 없음 — {r["ref"]})'))
    if len(rows) < rules.get('file', {}).get('min_rows', 0):
        V.append((0, '(파일)', str(len(rows)), f'row_count(최소 {rules["file"]["min_rows"]}행 미만)'))
    return V, len(rows)


def report_text(name, V, n):
    L = [f'[{name}] {n}행 검사 → 위반 {len(V)}건 → {"✅ PASS" if not V else "🚫 FAIL(게이트 차단)"}']
    for row, col, val, rule in V:
        L.append(f'  · 행 {row} [{col}] "{val}" — {rule}')
    return '\n'.join(L)


# ── 검증 데모 (정답 선작성) ────────────────────────────────────────
def make_demo():
    cust = os.path.join(HERE, 'demo_customers.csv')
    with open(cust, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['고객ID', '이름'])
        w.writerows([[f'C{i:03d}', f'고객{i}'] for i in range(1, 8)])
    dirty = os.path.join(HERE, 'demo_orders_dirty.csv')
    rows = [
        ['O0001', 'C001', '2', '24000', '결제완료', '2026-09-01'],     # 행2 클린
        ['O0002', 'C002', '오십', '5000', '배송중', '2026-09-01'],     # 행3 ★type(수량)
        ['O0003', 'C003', '0', '3000', '완료', '2026-09-02'],          # 행4 ★min(수량 0<1)
        ['BAD-4', 'C004', '1', '1000', '완료', '2026-09-02'],          # 행5 ★regex(주문번호)
        ['O0005', '', '3', '9000', '취소', '2026-09-02'],              # 행6 ★required(고객ID)
        ['O0006', 'C999', '1', '2000', '완료', '2026-09-03'],          # 행7 ★ref(끊긴 참조)
        ['O0007', 'C005', '1', '2000', '반품중', '2026-09-03'],        # 행8 ★enum(상태)
        ['O0001', 'C006', '1', '2000', '완료', '2026-09-03'],          # 행9 ★unique(주문번호 중복)
        ['O0009', 'C007', '1', '2000', '완료', '09/03/2026'],          # 행10 ★date 형식
    ]
    with open(dirty, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['주문번호', '고객ID', '수량', '금액', '상태', '주문일'])
        w.writerows(rows)
    clean = os.path.join(HERE, 'demo_orders_clean.csv')
    with open(clean, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['주문번호', '고객ID', '수량', '금액', '상태', '주문일'])
        w.writerows([[f'O01{i:02d}', f'C00{i}', str(i), str(i * 5000), '완료', f'2026-09-0{i}']
                     for i in range(1, 7)])
    return dirty, clean


def main_demo():
    dirty, clean = make_demo()
    V1, n1 = gate(dirty)
    V2, n2 = gate(clean)
    V1b, _ = gate(dirty)

    got = {(row, col, rule.split('(')[0]) for row, col, _, rule in V1}
    want = {(3, '수량', 'type'), (4, '수량', 'min'), (5, '주문번호', 'regex'),
            (6, '고객ID', 'required'), (7, '고객ID', 'ref'), (8, '상태', 'enum'),
            (9, '주문번호', 'unique'), (10, '주문일', 'type')}
    # ① 위반 8종 전수(행 번호·컬럼·규칙까지 정확)
    ok1 = (got == want)
    # ② 위반 수 = 심은 수 정확(초과 검출 0)
    ok2 = (len(V1) == 8)
    # ③ 클린 파일 오탐 0
    ok3 = (V2 == [] and n2 == 6)
    # ④ 참조 무결성: C999가 ref 규칙으로 잡힘(위 ①에 포함 — 명시 재확인)
    ok4 = any(rule.startswith('ref') and val == 'C999' for _, _, val, rule in V1)
    # ⑤ unique 상세: 중복이 원본 행 번호(2)를 지목
    ok5 = any('행 2와 중복' in rule for _, _, _, rule in V1)
    # ⑥ 재현성
    ok6 = (V1 == V1b)

    L = [f'# 데이터 게이트 검증 리포트 ({dt.datetime.now():%Y-%m-%d %H:%M})',
         '- 데모 = 정답 선작성: 위반 8종을 행 번호를 알고 심음(타입·범위·형식·필수·참조·허용값·중복·날짜) + 클린 파일',
         '', '| 검증 | 결과 |', '|---|---|',
         f'| ① 심은 위반 8종 전수 검출(행·컬럼·규칙까지) | {"PASS" if ok1 else "★FAIL"} |',
         f'| ② 위반 수 = 심은 수(초과 검출 0) | {len(V1)}/8 → {"PASS" if ok2 else "★FAIL"} |',
         f'| ③ 클린 파일 오탐 0 → 게이트 PASS | {"PASS" if ok3 else "★FAIL"} |',
         f'| ④ ★참조 무결성(다른 파일에 없는 고객ID) 검출 | {"PASS" if ok4 else "★FAIL"} |',
         f'| ⑤ 중복 검출이 원본 행 번호까지 지목 | {"PASS" if ok5 else "★FAIL"} |',
         f'| ⑥ 재현성 | {"PASS" if ok6 else "★FAIL"} |',
         '', '## 게이트 출력 실물', '```', report_text('dirty', V1, n1), '',
         report_text('clean', V2, n2), '```',
         '', '- ※ 규칙 = 선언형 설정(고객 데이터 계약에 1회 맞춤). 게이트 = 종료 코드로 파이프라인 차단.']
    rep = os.path.join(HERE, 'gate_verify.md')
    open(rep, 'w', encoding='utf-8').write('\n'.join(L))
    print('\n'.join(L))
    return ok1 and ok2 and ok3 and ok4 and ok5 and ok6


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    if len(sys.argv) > 1 and sys.argv[1].endswith('.csv'):
        V, n = gate(sys.argv[1])
        print(report_text(os.path.basename(sys.argv[1]), V, n))
        sys.exit(1 if V else 0)
    ok = main_demo()
    sys.exit(0 if ok else 1)
