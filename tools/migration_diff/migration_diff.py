#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""migration_diff.py — 데이터 이관 검증기 (구시스템 vs 신시스템 전수 대조)

시스템 교체·이관(ERP 교체, DB 이전, 엑셀→시스템 적재) 후 "제대로 옮겨졌는가"를 전수 대조로 증명한다.
이관의 최종 관문은 검증이다.

핵심 설계 = **형식 변화와 값 훼손의 구분**:
  이관하면 표기가 바뀐다(금액 콤마, 날짜 형식, 공백). 이건 정상이다. 검증기는 표기를 정규화한 뒤
  비교해 **진짜 훼손(값이 달라진 것)만** 잡는다 — 정규화 없는 diff는 오탐 수천 건으로 무의미해진다.

대조 4층:
  ① 건수: A(원본) vs B(이관본) 총 건수
  ② 키 대사: A에만 있는 키(누락) / B에만 있는 키(여분·중복 유입)
  ③ 필드 대조: 공통 키의 필드별 값 비교(정규화 후) — 훼손 목록(키·필드·양쪽 값)
  ④ 합계 항등: 수치 컬럼 총합 A=B (개별 훼손의 이중 안전망)

검증(--make-demo): 원본 1,000건 → 이관 시뮬 2벌:
  클린본(표기만 변경) → **차이 0(오탐 0)** / 결함본(누락 3·중복 2·여분 1·필드 훼손 4 심음) →
  **정확히 그 키·필드만 검출** + 합계 안전망 + 재현성.
※ 키·필드 매핑·정규화 규칙 = 설정값(시스템 쌍마다 1회 맞춤). 리포트 = 서식 엑셀 동봉.
"""
import os, sys, csv, random, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', '..', 'core'))
from xlsx import write_workbook


# ── 정규화 (표기 차이 흡수 = 오탐 방지의 본체) ─────────────────────
def norm_amount(v):
    s = str(v).strip().replace(',', '').replace('원', '')
    try:
        return str(int(float(s)))
    except ValueError:
        return s


def norm_date(v):
    s = str(v).strip().replace('/', '-').replace('.', '-').replace('T', ' ')
    return s[:10]


def norm_text(v):
    return ' '.join(str(v).split())             # 공백 정리


NORMALIZERS = {'amount': norm_amount, 'date': norm_date, 'text': norm_text}


def load_rows(path, key_col):
    rows = list(csv.DictReader(open(path, encoding='utf-8-sig')))
    out = {}
    dups = []
    for r in rows:
        k = str(r[key_col]).strip()
        if k in out:
            dups.append(k)                       # 같은 키 2회 = 중복 유입
        out[k] = r
    return out, len(rows), dups


def compare(a_path, b_path, key_a, key_b, fields):
    """A(원본) vs B(이관본). 키 컬럼명이 달라도 됨. fields = [(A컬럼, B컬럼, 타입 amount/date/text)]."""
    A, na, dup_a = load_rows(a_path, key_a)
    B, nb, dup_b = load_rows(b_path, key_b)
    only_a = sorted(set(A) - set(B))             # 누락
    only_b = sorted(set(B) - set(A))             # 여분
    broken = []                                  # (키, 필드, A값, B값)
    for k in sorted(set(A) & set(B)):
        for ca, cb, typ in fields:
            f = NORMALIZERS[typ]
            va, vb = f(A[k].get(ca, '')), f(B[k].get(cb, ''))
            if va != vb:
                broken.append((k, ca, A[k].get(ca, ''), B[k].get(cb, '')))
    sums = {}
    for ca, cb, typ in fields:
        if typ == 'amount':
            sa = sum(int(norm_amount(r.get(ca, '0')) or 0) for r in A.values())
            sb = sum(int(norm_amount(r.get(cb, '0')) or 0) for r in B.values())
            sums[ca] = (sa, sb)
    clean = (na == nb and not only_a and not only_b and not broken and not dup_b
             and all(sa == sb for sa, sb in sums.values()))
    return dict(na=na, nb=nb, only_a=only_a, only_b=only_b, dup_b=dup_b,
                broken=broken, sums=sums, clean=clean)


def write_report(res, out):
    sheets = {}
    if res['only_a']:
        sheets['누락(원본에만)'] = (['키'], [[k] for k in res['only_a']])
    if res['only_b']:
        sheets['여분(이관본에만)'] = (['키'], [[k] for k in res['only_b']])
    if res['dup_b']:
        sheets['중복 유입(이관본)'] = (['키'], [[k] for k in res['dup_b']])
    if res['broken']:
        sheets['필드 훼손'] = (['키', '필드', '원본 값', '이관본 값'], [list(b) for b in res['broken']])
    if not sheets:
        sheets['판정'] = (['결과'], [['차이 없음 — 이관 정합']])
    return write_workbook(out, sheets, summary={
        '생성': dt.datetime.now().strftime('%Y-%m-%d %H:%M'),
        '건수': f"원본 {res['na']:,} vs 이관본 {res['nb']:,}",
        '누락 / 여분 / 중복': f"{len(res['only_a'])} / {len(res['only_b'])} / {len(res['dup_b'])}",
        '필드 훼손': f"{len(res['broken'])}건",
        '합계 항등': ' · '.join(f'{c} {sa:,}={sb:,} {"OK" if sa == sb else "★차이"}'
                            for c, (sa, sb) in res['sums'].items()) or '-',
        '★판정': '정합(차이 0)' if res['clean'] else '차이 있음 — 상세 시트 확인',
        '주의': '표기 차이는 정규화로 흡수(오탐 방지) · 키/필드 매핑=시스템 쌍마다 1회 설정'})


# ── 데모: 이관 시뮬 + 결함 심기 ────────────────────────────────────
def make_demo():
    rng = random.Random(20260903)
    n = 1000
    상품 = ['비타민C 1000', '유산균 30포', '노트북 파우치 15인치', '텀블러 500ml', '캠핑 랜턴 LED']
    rows = []
    for i in range(n):
        rows.append({'전표번호': f'V{20260800+i}', '일자': f'2026-08-{(i%28)+1:02d}',
                     '거래처': f'거래처{i%40:02d}', '품명': rng.choice(상품),
                     '금액': str(rng.choice([12000, 8900, 35000, 129000, 4500, 250000]))})
    a_path = os.path.join(HERE, 'demo_old.csv')
    with open(a_path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['전표번호', '일자', '거래처', '품명', '금액'])
        w.writeheader(); w.writerows(rows)

    def to_new(r):
        """신시스템 표기: 컬럼명 변경 + 금액 콤마 + 날짜 슬래시 + 공백 변형 = 전부 '정상' 변화"""
        return {'voucher_no': r['전표번호'], 'trx_date': r['일자'].replace('-', '/'),
                'partner': r['거래처'], 'item_name': ' ' + r['품명'] + ' ',
                'amount': f"{int(r['금액']):,}"}

    # 클린본: 표기만 바뀐 완전 정합 이관
    clean_rows = [to_new(r) for r in rows]
    b_clean = os.path.join(HERE, 'demo_new_clean.csv')

    # 결함본: 심은 결함 — 누락 3 · 중복 2 · 여분 1 · 필드 훼손 4
    # ★키는 반드시 실제 키 공간(V20260800~V20261799) 안에서 — 1차 검증 검거: 범위 밖 키를 심어
    #   10개 중 2개만 실존 = 심기 헛방 → 검증이 잡아냄(planted ≠ detected 불일치)
    MISS = ['V20260803', 'V20260977', 'V20261499']
    DUPS = ['V20260900', 'V20261300']
    EXTRA = {'voucher_no': 'V99999999', 'trx_date': '2026/08/31', 'partner': '유령거래처',
             'item_name': '유령품목', 'amount': '1'}
    BREAK = {'V20260810': ('amount', lambda v: f"{int(v.replace(',', '')) + 900:,}"),   # 금액 +900
             'V20261022': ('trx_date', lambda v: v.replace('/08/', '/09/')),            # 날짜 월 시프트
             'V20261333': ('item_name', lambda v: v.strip()[:3]),                       # 문자 잘림
             'V20261444': ('partner', lambda v: v + '?')}                               # 문자 훼손
    bad_rows = []
    for r in clean_rows:
        k = r['voucher_no']
        if k in MISS:
            continue
        r2 = dict(r)
        if k in BREAK:
            col, fn = BREAK[k]
            r2[col] = fn(r2[col])
        bad_rows.append(r2)
        if k in DUPS:
            bad_rows.append(dict(r2))
    bad_rows.append(EXTRA)
    b_bad = os.path.join(HERE, 'demo_new_bad.csv')
    for path, rws in ((b_clean, clean_rows), (b_bad, bad_rows)):
        with open(path, 'w', encoding='utf-8-sig', newline='') as f:
            w = csv.DictWriter(f, fieldnames=['voucher_no', 'trx_date', 'partner', 'item_name', 'amount'])
            w.writeheader(); w.writerows(rws)
    return a_path, b_clean, b_bad, MISS, DUPS, BREAK, n


FIELDS = [('일자', 'trx_date', 'date'), ('거래처', 'partner', 'text'),
          ('품명', 'item_name', 'text'), ('금액', 'amount', 'amount')]


def main_demo():
    a, b_clean, b_bad, MISS, DUPS, BREAK, n = make_demo()

    # ① 클린 이관(표기만 변화) → 차이 0 = 오탐 0
    r1 = compare(a, b_clean, '전표번호', 'voucher_no', FIELDS)
    ok1 = r1['clean'] and r1['na'] == r1['nb'] == n

    # ② 결함본 → 심은 것만 정확 검출
    r2 = compare(a, b_bad, '전표번호', 'voucher_no', FIELDS)
    miss_ok = (r2['only_a'] == sorted(MISS))
    extra_ok = (r2['only_b'] == ['V99999999'])
    dup_ok = (sorted(r2['dup_b']) == sorted(DUPS))
    broken_keys = sorted(set(k for k, _, _, _ in r2['broken']))
    field_map = {k: f for k, f, _, _ in r2['broken']}
    brk_ok = (broken_keys == sorted(BREAK)
              and field_map['V20260810'] == '금액' and field_map['V20261022'] == '일자'
              and field_map['V20261333'] == '품명' and field_map['V20261444'] == '거래처'
              and len(r2['broken']) == 4)
    # ⑤ 합계 안전망: 금액 훼손 +900과 중복·누락·여분이 합계 차이로도 드러남
    sa, sb = r2['sums']['금액']
    sum_ok = (sa != sb)
    # ⑥ 재현성
    r3 = compare(a, b_bad, '전표번호', 'voucher_no', FIELDS)
    rep_ok = (r2 == r3)

    out = os.path.join(HERE, '이관검증_데모.xlsx')
    res_for_report = dict(r2)
    info = write_report(res_for_report, out)

    L = [f'# 데이터 이관 검증 리포트 ({dt.datetime.now():%Y-%m-%d %H:%M})',
         f'- 데모 = 원본 {n:,}건(구시스템) → 이관 시뮬 2벌: **클린본**(컬럼명·금액 콤마·날짜 형식·공백 전부 변경 = 정상 변화만) /'
         f' **결함본**(누락 {len(MISS)}·중복 {len(DUPS)}·여분 1·필드 훼손 {len(BREAK)} 심음)',
         '', '| 검증 | 결과 |', '|---|---|',
         f'| ① 클린본(표기만 변화) → 차이 0 = ★오탐 0 | {"PASS" if ok1 else "★FAIL"} |',
         f'| ② 심은 누락 {len(MISS)}건 → 키 정확 검출 | {"PASS" if miss_ok else "★FAIL"} |',
         f'| ③ 심은 여분 1·중복 {len(DUPS)} → 정확 검출 | {"PASS" if extra_ok and dup_ok else "★FAIL"} |',
         f'| ④ 심은 필드 훼손 {len(BREAK)}건(금액+900·날짜 월시프트·잘림·문자훼손) → 키·필드 정확 검출 | {"PASS" if brk_ok else "★FAIL"} |',
         f'| ⑤ 합계 항등 안전망(금액 총합 {sa:,} vs {sb:,}) | 차이 검출 → {"PASS" if sum_ok else "★FAIL"} |',
         f'| ⑥ 재현성(2회 동일) | {"OK" if rep_ok else "★불일치"} |',
         f'| 산출 | {os.path.basename(out)} ({info["sheets"]}시트) |',
         '', '- ※ 표기 차이(콤마·날짜 형식·공백·컬럼명)는 정규화로 흡수 — 정규화 없는 diff는 오탐 수천 건으로 무의미.',
         '- ※ 키·필드 매핑·정규화 규칙 = 시스템 쌍마다 1회 설정. 실이관 검증도 같은 리포트 형식으로 납품.']
    rep = os.path.join(HERE, 'migration_verify.md')
    open(rep, 'w', encoding='utf-8').write('\n'.join(L))
    print('\n'.join(L))
    return ok1 and miss_ok and extra_ok and dup_ok and brk_ok and sum_ok and rep_ok


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    ok = main_demo()
    sys.exit(0 if ok else 1)
