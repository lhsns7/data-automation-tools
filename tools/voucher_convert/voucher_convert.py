#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""voucher_convert.py — 주문/거래 내역 → 회계 전표(분개) 자동 변환 (2026-09)

판매 내역 CSV를 회계 시스템 업로드용 **전표(분개) 형식**으로 변환한다.
판매 1건 = 차변(결제수단별 채권 계정) / 대변(상품매출 공급가액 + 부가세예수금), 환불 = 역분개.

변환 규칙(명시):
  - 부가세 역산: **공급가액 = round(총액/1.1)** (원단위 반올림), 부가세 = 총액 − 공급가액 → 합계 항등 보장
  - 면세 상품: 공급가액 = 총액, 부가세 0
  - 결제수단 → 차변 계정: 카드=카드미수금 · 계좌이체/현금=보통예금 · 외상=외상매출금 (매핑 설정값)
  - 환불 = 같은 구조의 **역분개**(차대 반전)
  - ★검증: 전표마다 차변합 == 대변합 (1원 어긋나면 FAIL) + 전체 총액 보존

검증(--make-demo): 차대 균형 전수 · 부가세 항등 전수 · 수기 분개 대조 1건 · 면세·환불 케이스 ·
  템퍼(금액 조작 → 균형검증이 잡는지) · 불량행 격리 · 재현성.
※ 세율·계정과목은 설정값 — 본 도구는 변환 자동화이며 세무·회계 자문이 아님. 시스템별 업로드 양식은 1회 맞춤.
"""
import os, sys, csv, random, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', '..', 'core'))
from xlsx import write_workbook

VAT_DIV = 1.1
PAY_ACCT = {'카드': '카드미수금', '계좌이체': '보통예금', '현금': '보통예금', '외상': '외상매출금'}
SALES_ACCT, VAT_ACCT = '상품매출', '부가세예수금'


def split_vat(total, taxfree=False):
    """총액 → (공급가액, 부가세). 공급가액=round(총액/1.1) → 합계 항등 보장."""
    if taxfree:
        return total, 0
    supply = round(total / VAT_DIV)
    return supply, total - supply


def convert(rows):
    """→ (전표 리스트, 격리). 전표 = {no, 일자, 구분, lines:[(차변계정,차,대변계정,대)], 거래처, 적요, 총액}"""
    vouchers, quar = [], []
    no = 0
    for i, r in enumerate(rows, 2):
        d = (r.get('일자') or '').strip()
        pay = (r.get('결제수단') or '').strip()
        st = (r.get('구분') or '판매').strip()
        try:
            total = int(str(r.get('총액', '')).replace(',', ''))
        except ValueError:
            quar.append((i, r.get('총액'), '총액이 숫자가 아님')); continue
        if pay not in PAY_ACCT:
            quar.append((i, pay, f'미등록 결제수단 "{pay}"(계정 매핑 없음)')); continue
        if st not in ('판매', '환불'):
            quar.append((i, st, f'알 수 없는 구분 "{st}"')); continue
        taxfree = (r.get('과세') or '과세').strip() == '면세'
        supply, vat = split_vat(abs(total), taxfree)
        sign = -1 if st == '환불' else 1
        no += 1
        lines = [(PAY_ACCT[pay], sign * abs(total), '', 0),
                 ('', 0, SALES_ACCT, sign * supply)]
        if vat:
            lines.append(('', 0, VAT_ACCT, sign * vat))
        vouchers.append({'no': no, '일자': d, '구분': st, 'lines': lines,
                         '거래처': (r.get('거래처') or '').strip(), '적요': (r.get('상품') or '').strip(),
                         '총액': sign * abs(total)})
    return vouchers, quar


def balance_check(vouchers):
    """전표별 차변합-대변합. 전부 0이어야 PASS."""
    bad = []
    for v in vouchers:
        dr = sum(l[1] for l in v['lines'])
        cr = sum(l[3] for l in v['lines'])
        if dr != cr:
            bad.append((v['no'], dr, cr))
    return bad


def write_out(out, vouchers, quar):
    rows = []
    for v in vouchers:
        for j, (da, dv, ca, cv) in enumerate(v['lines']):
            rows.append([v['no'] if j == 0 else '', v['일자'] if j == 0 else '', v['구분'] if j == 0 else '',
                         da, dv if dv else '', ca, cv if cv else '',
                         v['거래처'] if j == 0 else '', v['적요'] if j == 0 else ''])
    sheets = {'전표(분개)': (['전표번호', '일자', '구분', '차변계정', '차변금액', '대변계정', '대변금액', '거래처', '적요'], rows)}
    if quar:
        sheets['격리(확인 필요)'] = (['원본행', '값', '사유'], [list(q) for q in quar])
    bad = balance_check(vouchers)
    return write_workbook(out, sheets, summary={
        '생성': dt.datetime.now().strftime('%Y-%m-%d %H:%M'),
        '전표 수 / 격리': f'{len(vouchers)}건 / {len(quar)}행',
        '총액 합계': f"{sum(v['총액'] for v in vouchers):,}원",
        '★차대 균형': f'{len(vouchers) - len(bad)}/{len(vouchers)} 전표 균형 → ' + ('PASS' if not bad else f'★FAIL {len(bad)}건'),
        '규칙': '공급가액=round(총액/1.1)·부가세=차액(항등) / 면세=부가세0 / 환불=역분개',
        '주의': '세율·계정과목=설정값. 변환 자동화이며 세무·회계 자문 아님. 시스템별 양식 1회 맞춤'})


# ── 데모 + 검증 ─────────────────────────────────────────────────────
def make_demo(path, n=60):
    random.seed(20260903)
    상품 = ['비타민C', '도서(면세)', '유산균', '노트북 파우치', '쌀 10kg(면세)']
    수단 = ['카드', '계좌이체', '현금', '외상']
    rows = []
    for i in range(n):
        d = (dt.date(2026, 8, 1) + dt.timedelta(days=i % 30)).isoformat()
        prod = random.choice(상품)
        total = random.choice([11000, 33000, 10001, 99990, 123457, 55000])   # 10001=라운딩 유발
        st = '환불' if i % 9 == 0 else '판매'
        pay = random.choice(수단)
        if i % 23 == 0:
            pay = '포인트'                       # 미등록 수단 → 격리
        if i % 31 == 0:
            total = '만원'                       # 불량 → 격리
        taxfree = '면세' if '면세' in prod else '과세'
        rows.append([d, prod, total, pay, st, taxfree, f'거래처{i%7}'])
    with open(path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['일자', '상품', '총액', '결제수단', '구분', '과세', '거래처'])
        w.writerows(rows)
    return n


def main_demo():
    demo = os.path.join(HERE, 'demo_orders.csv')
    n = make_demo(demo)
    rows = list(csv.DictReader(open(demo, encoding='utf-8-sig')))
    vouchers, quar = convert(rows)
    vouchers2, _ = convert(rows)
    same = (vouchers == vouchers2)

    bad = balance_check(vouchers)                                  # ① 차대 균형 전수
    vat_ok = all(abs(v['lines'][1][3]) + (abs(v['lines'][2][3]) if len(v['lines']) > 2 else 0)
                 == abs(v['총액']) for v in vouchers)               # ② 부가세 항등 전수
    # ③ 수기 분개 대조: 총액 10,001 과세 판매 → 공급 9,092 / 부가세 909
    s, vt = split_vat(10001)
    hand_ok = (s == 9092 and vt == 909 and s + vt == 10001)
    # ④ 면세·환불 실측
    tf = [v for v in vouchers if len(v['lines']) == 2]             # 부가세 줄 없음 = 면세
    rf = [v for v in vouchers if v['구분'] == '환불']
    tf_ok = all(v['lines'][1][3] == v['총액'] for v in tf) and len(tf) > 0
    rf_ok = all(v['총액'] < 0 and sum(l[1] for l in v['lines']) == sum(l[3] for l in v['lines']) for v in rf) and len(rf) > 0
    # ⑤ 템퍼: 한 전표의 대변 1원 조작 → 균형검증이 잡아야 정상
    import copy
    tam = copy.deepcopy(vouchers)
    da, dv, ca, cv = tam[0]['lines'][1]
    tam[0]['lines'][1] = (da, dv, ca, cv + 1)
    tamper_ok = (len(balance_check(tam)) == 1)

    out = os.path.join(HERE, '전표_데모.xlsx')
    info = write_out(out, vouchers, quar)

    now = dt.datetime.now()
    L = [f'# 전표 변환 검증 리포트 ({now:%Y-%m-%d %H:%M})',
         f'- 데모 {n}건(면세·환불·라운딩 유발 10,001원·불량 포함) → 전표 {len(vouchers)}건 · 격리 {len(quar)}행',
         '', '| 검증 | 결과 |', '|---|---|',
         f'| ① ★차대 균형(전표별 차변합=대변합) | **{len(vouchers) - len(bad)}/{len(vouchers)}** → {"PASS" if not bad else "★FAIL"} |',
         f'| ② 부가세 항등(공급+부가세=총액, 건별) | {"전수 PASS" if vat_ok else "★FAIL"} |',
         f'| ③ 수기 분개 대조(10,001원) | 공급 {s:,}·부가세 {vt} → {"PASS" if hand_ok else "★FAIL"} |',
         f'| ④ 면세({len(tf)}건 부가세0)·환불({len(rf)}건 역분개 균형) | {"PASS" if tf_ok and rf_ok else "★FAIL"} |',
         f'| ⑤ 템퍼(대변 1원 조작 → 균형검증) | {"FAIL 검출 = 정상 PASS" if tamper_ok else "★못 잡음"} |',
         f'| ⑥ 격리(묵살 금지) | {len(quar)}행(미등록 수단·불량 금액) 사유 표기 |',
         f'| ⑦ 재현성 | {"OK" if same else "★불일치"} |',
         f'| 산출 | {os.path.basename(out)} ({info["sheets"]}시트) |',
         '', '- ※ 세율·계정과목=설정값. 변환 자동화이며 세무·회계 자문 아님. 이카운트/더존 등 시스템별 업로드 양식은 1회 맞춤.']
    rep = os.path.join(HERE, 'voucher_verify.md')
    open(rep, 'w', encoding='utf-8').write('\n'.join(L))
    print('\n'.join(L))
    return (not bad) and vat_ok and hand_ok and tf_ok and rf_ok and tamper_ok and same


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    ok = main_demo()
    sys.exit(0 if ok else 1)
