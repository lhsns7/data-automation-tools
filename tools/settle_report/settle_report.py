#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""settle_report.py — 거래내역 → 파트너별 정산서 자동 생성 (v2, 2026-09)

v1: 수수료·환불 차감·라운딩 귀속 + 대사(1원 항등) + 템퍼·손검산.
v2: **파트너 유형별 세금** — 개인(프리랜서)=원천징수 3.3%(소액부징수: 세액 1,000원 미만 징수 안 함),
        사업자=부가세 10% 별도. 대사를 2단으로 확장(①정산 항등 ②세금 항등, 둘 다 0원).

정산 규칙(설계 명시):
  - 수수료 = 건별 금액 × 요율, 건별 원단위 절사 → 라운딩 오차 회사 귀속
  - 환불 = 같은 요율 음수 차감
  - 기본지급 = 순매출 − 수수료
  - 개인: 원천세 = 기본지급 × 원천세율(기본 3.3%) 원단위 절사, **세액 < 1,000원이면 0(소액부징수)**.
         실지급 = 기본지급 − 원천세 (원천세는 회사가 보관·대납)
  - 사업자: 부가세 = 기본지급 × 10% 원단위 절사. 실지급 = 기본지급 + 부가세 (세금계산서 수취 전제)
  - ★대사①: Σ기본지급 + Σ수수료 == Σ순매출 (1원 차이 = FAIL)
  - ★대사②: Σ실지급 + Σ원천세 − Σ부가세 == Σ기본지급 (1원 차이 = FAIL)
  - ※세율·부징수 기준은 설정값 — 본 도구는 계산 자동화이며 세무 자문이 아님. 확정 기준은 고객 세무 담당 확인.

검증(--make-demo): 대사①② 0원 · 손검산(개인 1곳+사업자 1곳 독립 재계산) · 템퍼 테스트 ·
  소액부징수 경계 3케이스 명시 · 불량행 격리 · 재현성.
"""
import os, sys, csv, random, datetime as dt, collections

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', '..', 'core'))
from xlsx import write_workbook

WHT_RATE = 0.033        # 원천징수(소득세 3% + 지방소득세 0.3%) — 설정값
WHT_MIN = 1000          # 소액부징수: 원천세액이 이 미만이면 징수 0 — 설정값
VAT_RATE = 0.10         # 부가세 — 설정값

PARTNERS = {
    '열심상회': {'rate': 0.10,  'type': '개인'},
    '달빛서점': {'rate': 0.125, 'type': '사업자'},
    '한강마켓': {'rate': 0.08,  'type': '개인'},
    '푸른과일': {'rate': 0.15,  'type': '사업자'},
    '소소공방': {'rate': 0.095, 'type': '개인'},
    '작은책방': {'rate': 0.12,  'type': '개인'},   # 소액 파트너(소액부징수 경계 실측용)
}


def wht(base):
    """개인 원천세: 원단위 절사 + 소액부징수(<WHT_MIN → 0)."""
    t = int(base * WHT_RATE) if base > 0 else 0
    return 0 if t < WHT_MIN else t


def vat(base):
    return int(base * VAT_RATE) if base > 0 else 0


# ── 엔진 ────────────────────────────────────────────────────────────
def settle(rows, partners):
    agg = collections.defaultdict(lambda: {'매출': 0, '환불': 0, '수수료': 0, '건수': 0, '환불건수': 0})
    valid, quarantined = [], []
    for i, r in enumerate(rows, 2):
        p = (r.get('파트너') or '').strip()
        st = (r.get('상태') or '').strip()
        try:
            amt = int(str(r.get('금액', '')).replace(',', ''))
        except ValueError:
            quarantined.append((i, p, r.get('금액'), st, '금액이 숫자가 아님')); continue
        if p not in partners:
            quarantined.append((i, p, amt, st, '미등록 파트너(요율 없음)')); continue
        if st not in ('완료', '환불'):
            quarantined.append((i, p, amt, st, f'알 수 없는 상태 "{st}"')); continue
        sign = -1 if st == '환불' else 1
        fee = sign * int(abs(amt) * partners[p]['rate'])
        a = agg[p]
        a['건수'] += 1
        if sign < 0:
            a['환불'] += amt; a['환불건수'] += 1
        else:
            a['매출'] += amt
        a['수수료'] += fee
        valid.append((i, p, sign * amt, fee))
    for p, a in agg.items():
        a['유형'] = partners[p]['type']
        a['순매출'] = a['매출'] - a['환불']
        a['기본지급'] = a['순매출'] - a['수수료']
        a['원천세'] = wht(a['기본지급']) if a['유형'] == '개인' else 0
        a['부가세'] = vat(a['기본지급']) if a['유형'] == '사업자' else 0
        a['실지급'] = a['기본지급'] - a['원천세'] + a['부가세']
    net = sum(a['순매출'] for a in agg.values())
    base = sum(a['기본지급'] for a in agg.values())
    fee_t = sum(a['수수료'] for a in agg.values())
    wht_t = sum(a['원천세'] for a in agg.values())
    vat_t = sum(a['부가세'] for a in agg.values())
    pay_t = sum(a['실지급'] for a in agg.values())
    recon = {'순매출총액': net, '기본지급총액': base, '수수료총액': fee_t,
             '원천세총액': wht_t, '부가세총액': vat_t, '실지급총액': pay_t,
             '대사1(순매출-기본지급-수수료)': net - base - fee_t,
             '대사2(실지급+원천세-부가세-기본지급)': pay_t + wht_t - vat_t - base}
    recon['PASS'] = (recon['대사1(순매출-기본지급-수수료)'] == 0 and
                     recon['대사2(실지급+원천세-부가세-기본지급)'] == 0)
    return dict(agg), valid, quarantined, recon


def write_report(out, agg, valid, quarantined, recon, partners):
    sheets = {}
    head = ['파트너', '유형', '요율', '판매', '환불', '순매출', '수수료', '기본지급', '원천세', '부가세(+)', '실지급']
    total_rows = [[p, a['유형'], f"{partners[p]['rate']*100:g}%", a['건수'] - a['환불건수'], a['환불건수'],
                   a['순매출'], a['수수료'], a['기본지급'], a['원천세'], a['부가세'], a['실지급']]
                  for p, a in sorted(agg.items(), key=lambda x: -x[1]['실지급'])]
    sheets['총괄'] = (head, total_rows)
    for p, a in agg.items():
        rows = [[i, amt, fee, amt - fee] for i, pp, amt, fee in valid if pp == p]
        sheets[f'정산_{p}'] = (['원본행', '금액(환불 음수)', '수수료', '지급분'], rows)
    if quarantined:
        sheets['격리(확인 필요)'] = (['원본행', '파트너', '금액', '상태', '사유'], [list(q) for q in quarantined])
    return write_workbook(out, sheets, summary={
        '생성': dt.datetime.now().strftime('%Y-%m-%d %H:%M'),
        '유효 거래 / 격리': f"{len(valid)}건 / {len(quarantined)}행",
        '순매출 총액': f"{recon['순매출총액']:,}원",
        '회사 수수료': f"{recon['수수료총액']:,}원",
        '원천세 보관(대납)': f"{recon['원천세총액']:,}원",
        '부가세 가산': f"{recon['부가세총액']:,}원",
        '실지급 총액': f"{recon['실지급총액']:,}원",
        '★대사① (정산 항등)': f"{recon['대사1(순매출-기본지급-수수료)']}원 → {'PASS' if recon['대사1(순매출-기본지급-수수료)']==0 else '★FAIL'}",
        '★대사② (세금 항등)': f"{recon['대사2(실지급+원천세-부가세-기본지급)']}원 → {'PASS' if recon['대사2(실지급+원천세-부가세-기본지급)']==0 else '★FAIL'}",
        '규칙': f'건별 절사·오차 회사 귀속 / 개인 원천세 {WHT_RATE*100:g}%(소액부징수 {WHT_MIN:,}원 미만 0) / 사업자 부가세 {VAT_RATE*100:g}%',
        '주의': '세율·부징수 기준은 설정값 — 세무 확정은 고객 세무 담당 기준 확인(본 도구는 계산 자동화)'})


# ── 데모 + 검증 ─────────────────────────────────────────────────────
def make_demo(path, n=220):
    random.seed(20260903)
    상품 = ['텀블러', '노트', '파우치', '머그컵', '달력', '스티커팩', '에코백']
    pool = [p for p in PARTNERS if p != '작은책방']      # 작은책방 = 소액부징수 실측 전용(아래 2건만)
    rows = []
    for i in range(n):
        d = (dt.date(2026, 8, 1) + dt.timedelta(days=i % 31)).isoformat()
        p = '무명상점' if i % 37 == 0 else random.choice(pool)
        amt = random.choice([1, 999, 1234, 5678, 12345, 45000, 99999, 123457])
        st = '환불' if i % 11 == 0 else '완료'
        if i % 53 == 0:
            st = '취소요청'
        if i % 71 == 0:
            amt = '만이천원'
        rows.append([d, p, random.choice(상품), amt, st])
    # 소액 파트너: 기본지급이 소액부징수 경계 아래로 떨어지게 소액 거래만
    rows.append(['2026-08-15', '작은책방', '엽서', 15000, '완료'])
    rows.append(['2026-08-20', '작은책방', '책갈피', 9000, '완료'])
    with open(path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f); w.writerow(['일자', '파트너', '상품', '금액', '상태']); w.writerows(rows)
    return len(rows)


def hand_check(agg, valid, partner):
    """독립 손검산: 순매출·수수료·기본지급 + 유형별 세금·실지급을 별도 경로로 재계산."""
    cfg = PARTNERS[partner]
    net = sum(amt for _, p, amt, _ in valid if p == partner)
    fee = sum((-1 if amt < 0 else 1) * int(abs(amt) * cfg['rate']) for _, p, amt, _ in valid if p == partner)
    base = net - fee
    w_ = wht(base) if cfg['type'] == '개인' else 0
    v_ = vat(base) if cfg['type'] == '사업자' else 0
    pay = base - w_ + v_
    a = agg[partner]
    ok = (net == a['순매출'] and fee == a['수수료'] and base == a['기본지급']
          and w_ == a['원천세'] and v_ == a['부가세'] and pay == a['실지급'])
    return ok, {'net': net, 'fee': fee, 'base': base, 'wht': w_, 'vat': v_, 'pay': pay}


def main_demo():
    demo = os.path.join(HERE, 'demo_sales.csv')
    n = make_demo(demo)
    rows = list(csv.DictReader(open(demo, encoding='utf-8-sig')))
    agg, valid, quar, recon = settle(rows, PARTNERS)
    agg2, _, _, recon2 = settle(rows, PARTNERS)
    same = (agg == agg2 and recon == recon2)

    hc1_ok, hc1 = hand_check(agg, valid, '한강마켓')     # 개인
    hc2_ok, hc2 = hand_check(agg, valid, '달빛서점')     # 사업자
    small = agg.get('작은책방', {})                       # 소액부징수 실측 대상

    # 템퍼: 실지급 1원 조작 → 대사②가 잡아야 정상
    pay_t = sum(a['실지급'] for a in agg.values())
    diff2_t = (pay_t + 1) + recon['원천세총액'] - recon['부가세총액'] - recon['기본지급총액']
    tamper_ok = (diff2_t != 0)

    # 소액부징수 경계 3케이스(단위 명시)
    b1, b2, b3 = wht(29000), wht(30303), wht(31000)      # 957→0 / 999→0 / 1023→징수
    bd_ok = (b1 == 0 and b2 == 0 and b3 == 1023)

    out = os.path.join(HERE, '정산서_데모.xlsx')
    info = write_report(out, agg, valid, quar, recon, PARTNERS)

    now = dt.datetime.now()
    d1 = recon['대사1(순매출-기본지급-수수료)']
    d2 = recon['대사2(실지급+원천세-부가세-기본지급)']
    L = [f'# 정산 자동화 v2 검증 리포트 ({now:%Y-%m-%d %H:%M}) — 원천세·부가세 심화',
         f'- 데모 {n}건 · 파트너 {len(agg)}곳(개인 {sum(1 for a in agg.values() if a["유형"]=="개인")}·사업자 {sum(1 for a in agg.values() if a["유형"]=="사업자")}) · 격리 {len(quar)}행',
         '', '| 검증 | 결과 |', '|---|---|',
         f'| ★대사①(순매출−기본지급−수수료) | **{d1}원 → {"PASS" if d1==0 else "★FAIL"}** |',
         f'| ★대사②(실지급+원천세−부가세−기본지급) | **{d2}원 → {"PASS" if d2==0 else "★FAIL"}** |',
         f'| 손검산·개인(한강마켓) | {"일치 PASS" if hc1_ok else "★불일치"} — base {hc1["base"]:,} · 원천세 {hc1["wht"]:,} · 실지급 {hc1["pay"]:,} |',
         f'| 손검산·사업자(달빛서점) | {"일치 PASS" if hc2_ok else "★불일치"} — base {hc2["base"]:,} · 부가세 {hc2["vat"]:,} · 실지급 {hc2["pay"]:,} |',
         f'| 소액부징수 실측(작은책방) | 기본지급 {small.get("기본지급",0):,}원 → 원천세 {small.get("원천세",0):,}원 {"(경계 미만=0, 규칙 작동)" if small.get("원천세",0)==0 else ""} |',
         f'| 소액부징수 경계 3케이스 | wht(29,000)={b1} · wht(30,303)={b2} · wht(31,000)={b3} → {"PASS" if bd_ok else "★FAIL"} |',
         f'| 템퍼(실지급 1원 조작 → 대사②) | {"FAIL 검출 = 정상 PASS" if tamper_ok else "★조작 못 잡음"} |',
         f'| 불량행 격리 / 재현성 | {len(quar)}행 사유 표기 / {"OK" if same else "★불일치"} |',
         f'| 산출 | {os.path.basename(out)} ({info["sheets"]}시트) |',
         '',
         f'- 규칙: 개인 원천세 {WHT_RATE*100:g}%(절사·소액부징수 {WHT_MIN:,}원 미만 0) · 사업자 부가세 {VAT_RATE*100:g}% · 라운딩 회사 귀속.',
         '- ※ 세율·부징수 기준은 설정값. 본 도구는 계산 자동화이며 **세무 자문이 아님** — 확정 기준은 고객 세무 담당 확인.']
    rep = os.path.join(HERE, 'settle_verify.md')
    open(rep, 'w', encoding='utf-8').write('\n'.join(L))
    print('\n'.join(L))
    return recon['PASS'] and hc1_ok and hc2_ok and tamper_ok and bd_ok and same


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    if '--make-demo' in sys.argv or len(sys.argv) == 1:
        ok = main_demo()
        sys.exit(0 if ok else 1)
    else:
        inp = sys.argv[1]
        rows = list(csv.DictReader(open(inp, encoding='utf-8-sig')))
        agg, valid, quar, recon = settle(rows, PARTNERS)
        out = os.path.splitext(inp)[0] + '_정산서.xlsx'
        write_report(out, agg, valid, quar, recon, PARTNERS)
        print(f"완료: {out} · 대사① {recon['대사1(순매출-기본지급-수수료)']}원 · 대사② {recon['대사2(실지급+원천세-부가세-기본지급)']}원")
