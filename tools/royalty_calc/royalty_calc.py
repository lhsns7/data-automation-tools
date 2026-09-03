#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""royalty_calc.py — 콘텐츠·게임 로열티 정산 (구간 누진·MG 이월·채널 수수료)

게임·콘텐츠 로열티 정산의 어려움은 곱셈이 아니라 **규칙의 모서리**다:
  - 채널(스토어) 수수료 차감 후 **순매출** 기준 — 채널마다 수수료 상이
  - ★구간 누진 요율(러닝 개런티): 순매출 구간별 요율 — 경계에 걸친 달의 계산이 사고 지점
  - ★미니멈 개런티(MG) 이월: 선지급금을 로열티에서 차감 — 소진 전 지급 0, 소진에 걸친 달은
    부분 지급, 이후 전액. **월을 넘는 잔액 추적**이 이 도메인의 본체
  - ★대사 항등: Σ지급 + ΣMG차감 = Σ로열티 발생 — 1원이라도 어긋나면 FAIL

산출: 파트너별 월 명세(순매출·로열티·MG차감·지급·MG잔액) + 총괄 + 격리 — 서식 엑셀.

검증(--make-demo): 파트너 3(단일요율+MG / 구간누진 / 구간+MG) × 채널 2(수수료 30%/15%) × 3개월,
  ①구간 누진 수기 대조 ②구간 경계(정확히 상한 = 상위 구간 0원) ③MG 이월 궤적(지급 0→0→부분)
  ④항등(지급+MG=로열티, 순매출 대조) ⑤템퍼(지급 1원 조작 → 대사 검출) ⑥격리·재현성.
※ 요율·구간·MG·수수료 = 계약 설정값. 정산 자동화이며 회계·세무 자문 아님.
"""
import os, sys, csv, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', '..', 'core'))
from xlsx import write_workbook

CHANNEL_FEE = {'스토어X': 0.30, '스토어Y': 0.15}             # 채널 수수료(설정값)
CONTRACTS = {                                                # 계약(설정값)
    '알파스튜디오': dict(type='flat', rate=0.20, mg=1_000_000),
    '베타게임즈': dict(type='tier', tiers=[(3_500_000, 0.10), (7_000_000, 0.15), (None, 0.20)], mg=0),
    '감마웍스': dict(type='tier', tiers=[(3_500_000, 0.10), (7_000_000, 0.15), (None, 0.20)], mg=500_000),
}


def tier_royalty(net, tiers):
    """구간 누진: [(상한, 요율)…, (None, 요율)] — 경계 정확 처리(상한 이하까지 해당 구간)."""
    total, prev = 0, 0
    for cap, rate in tiers:
        hi = net if cap is None else min(net, cap)
        if hi > prev:
            total += round((hi - prev) * rate)
        if cap is not None and net <= cap:
            break
        prev = cap
    return total


def settle(rows, contracts=CONTRACTS, fees=CHANNEL_FEE):
    """rows = [{월,파트너,채널,상품,매출}] → (파트너별 월 명세, 격리)"""
    quar = []
    net_by = {}                                             # (파트너, 월) → 순매출 합
    for i, r in enumerate(rows, 2):
        p, ch = (r.get('파트너') or '').strip(), (r.get('채널') or '').strip()
        if p not in contracts:
            quar.append((i, p, '미등록 파트너')); continue
        if ch not in fees:
            quar.append((i, ch, '미등록 채널(수수료 없음)')); continue
        try:
            gross = int(str(r.get('매출', '')).replace(',', ''))
        except ValueError:
            quar.append((i, r.get('매출'), '매출이 숫자가 아님')); continue
        net = round(gross * (1 - fees[ch]))
        key = (p, (r.get('월') or '').strip())
        net_by[key] = net_by.get(key, 0) + net
    detail = {}                                             # 파트너 → [월 명세 dict]
    for p, c in contracts.items():
        months = sorted(m for (pp, m) in net_by if pp == p)
        mg_left = c['mg']
        out = []
        for m in months:
            net = net_by[(p, m)]
            roy = round(net * c['rate']) if c['type'] == 'flat' else tier_royalty(net, c['tiers'])
            mg_use = min(mg_left, roy)                      # ★MG 이월 차감(월 경계 넘는 잔액 추적)
            pay = roy - mg_use
            mg_left -= mg_use
            out.append(dict(월=m, 순매출=net, 로열티=roy, MG차감=mg_use, 지급=pay, MG잔액=mg_left))
        detail[p] = out
    return detail, quar


def audit(detail):
    """대사 항등: 파트너별 Σ지급 + ΣMG차감 == Σ로열티. 어긋난 파트너 목록 반환(빈 목록 = PASS)."""
    bad = []
    for p, rows in detail.items():
        if sum(r['지급'] for r in rows) + sum(r['MG차감'] for r in rows) != sum(r['로열티'] for r in rows):
            bad.append(p)
    return bad


def write_out(detail, quar, out):
    sheets = {}
    for p, rows in detail.items():
        sheets[f'{p}'] = (['월', '순매출', '로열티', 'MG차감', '지급', 'MG잔액'],
                          [[r['월'], r['순매출'], r['로열티'], r['MG차감'], r['지급'], r['MG잔액']] for r in rows])
    total = [[p, sum(r['로열티'] for r in rows), sum(r['MG차감'] for r in rows), sum(r['지급'] for r in rows),
              rows[-1]['MG잔액'] if rows else 0] for p, rows in detail.items()]
    sheets['총괄'] = (['파트너', '로열티 합', 'MG차감 합', '지급 합', 'MG 잔액'], total)
    if quar:
        sheets['격리(확인 필요)'] = (['원본행', '값', '사유'], [list(q) for q in quar])
    bad = audit(detail)
    return write_workbook(out, sheets, summary={
        '생성': dt.datetime.now().strftime('%Y-%m-%d %H:%M'),
        '파트너 / 격리': f'{len(detail)}곳 / {len(quar)}행',
        '★대사(지급+MG차감=로열티)': 'PASS' if not bad else f'★FAIL {bad}',
        '규칙': '순매출=매출×(1-채널수수료) · 구간 누진 경계 정확 · MG 이월 차감',
        '주의': '요율·구간·MG·수수료=계약 설정값. 정산 자동화이며 회계·세무 자문 아님'})


# ── 검증 데모 (수기 정답 선계산) ───────────────────────────────────
def make_demo(path):
    rows = [
        # 알파스튜디오(20% 단일 + MG 1,000,000): 순매출 2,250,000 / 2,400,000 / 3,400,000
        ('2026-06', '알파스튜디오', '스토어X', '알파RPG', 2_000_000),
        ('2026-06', '알파스튜디오', '스토어Y', '알파RPG', 1_000_000),
        ('2026-07', '알파스튜디오', '스토어X', '알파RPG', 1_000_000),
        ('2026-07', '알파스튜디오', '스토어Y', '알파RPG', 2_000_000),
        ('2026-08', '알파스튜디오', '스토어Y', '알파RPG', 4_000_000),
        # 베타게임즈(구간 누진): 6월 10,400,000(3구간 걸침) / 7월 3,500,000(★정확 경계) / 8월 1,700,000
        ('2026-06', '베타게임즈', '스토어X', '베타퍼즐', 10_000_000),
        ('2026-06', '베타게임즈', '스토어Y', '베타퍼즐', 4_000_000),
        ('2026-07', '베타게임즈', '스토어X', '베타퍼즐', 5_000_000),
        ('2026-08', '베타게임즈', '스토어Y', '베타퍼즐', 2_000_000),
        # 감마웍스(구간 + MG 500,000): 1,700,000 / 2,800,000 / 1,400,000
        ('2026-06', '감마웍스', '스토어Y', '감마슈팅', 2_000_000),
        ('2026-07', '감마웍스', '스토어X', '감마슈팅', 4_000_000),
        ('2026-08', '감마웍스', '스토어X', '감마슈팅', 2_000_000),
        # 격리 2행
        ('2026-06', '알파스튜디오', '무명스토어', '알파RPG', 500_000),
        ('2026-07', '베타게임즈', '스토어X', '베타퍼즐', '오십만'),
    ]
    with open(path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['월', '파트너', '채널', '상품', '매출'])
        w.writerows(rows)


def main_demo():
    demo = os.path.join(HERE, 'demo_sales.csv')
    make_demo(demo)
    rows = list(csv.DictReader(open(demo, encoding='utf-8-sig')))
    detail, quar = settle(rows)
    detail2, _ = settle(rows)

    A, B, C = detail['알파스튜디오'], detail['베타게임즈'], detail['감마웍스']

    # ① 구간 누진 수기 대조(베타 6월 순 10,400,000): 3.5M×10% + 3.5M×15% + 3.4M×20% = 1,555,000
    ok1 = (B[0]['순매출'] == 10_400_000 and B[0]['로열티'] == 1_555_000)
    # ② 구간 경계(베타 7월 순매출 = 정확히 3,500,000): 1구간만 = 350,000 (상위 구간 0원)
    ok2 = (B[1]['순매출'] == 3_500_000 and B[1]['로열티'] == 350_000)
    # ③ MG 이월 궤적: 알파 지급 (0,0,610,000)·잔액 (550,000→70,000→0) / 감마 지급 (0,0,90,000)
    ok3 = ([r['지급'] for r in A] == [0, 0, 610_000]
           and [r['MG잔액'] for r in A] == [550_000, 70_000, 0]
           and [r['지급'] for r in C] == [0, 0, 90_000]
           and [r['MG잔액'] for r in C] == [330_000, 50_000, 0])
    # ④ 항등: 파트너 전부 지급+MG=로열티 + 총액 수기(로열티 총 4,275,000·지급 총 2,775,000·MG 총 1,500,000)
    tot_roy = sum(r['로열티'] for rows_ in detail.values() for r in rows_)
    tot_pay = sum(r['지급'] for rows_ in detail.values() for r in rows_)
    tot_mg = sum(r['MG차감'] for rows_ in detail.values() for r in rows_)
    ok4 = (not audit(detail) and tot_roy == 4_275_000 and tot_pay == 2_775_000 and tot_mg == 1_500_000)
    # ⑤ 템퍼: 지급 1원 조작 → 대사가 그 파트너를 검출
    import copy
    tam = copy.deepcopy(detail)
    tam['베타게임즈'][0]['지급'] += 1
    ok5 = (audit(tam) == ['베타게임즈'])
    # ⑥ 격리 2행(미등록 채널·문자 매출) + 재현성
    ok6 = (len(quar) == 2 and detail == detail2)

    out = os.path.join(HERE, '로열티정산_데모.xlsx')
    info = write_out(detail, quar, out)

    L = [f'# 로열티 정산 검증 리포트 ({dt.datetime.now():%Y-%m-%d %H:%M})',
         '- 데모 = 파트너 3(단일+MG / 구간 누진 / 구간+MG) × 채널 2(수수료 30%/15%) × 3개월 — **수기 정답 선계산** 대조',
         '', '| 검증 | 결과 |', '|---|---|',
         f'| ① 구간 누진 수기(순 10,400,000 → 3구간 = 1,555,000) | {"PASS" if ok1 else "★FAIL"} |',
         f'| ② ★구간 경계(순매출 = 정확히 상한 3,500,000 → 상위 구간 0원) | {"PASS" if ok2 else "★FAIL"} |',
         f'| ③ ★MG 이월 궤적(지급 0→0→부분 · 잔액 550,000→70,000→0 등 2계약) | {"PASS" if ok3 else "★FAIL"} |',
         f'| ④ 대사 항등(Σ지급 {tot_pay:,}+ΣMG {tot_mg:,}=Σ로열티 {tot_roy:,}) | {"PASS" if ok4 else "★FAIL"} |',
         f'| ⑤ 템퍼(지급 1원 조작 → 대사 검출) | {"PASS" if ok5 else "★FAIL"} |',
         f'| ⑥ 격리 2행(미등록 채널·문자 매출) + 재현성 | {"PASS" if ok6 else "★FAIL"} |',
         f'| 산출 | {os.path.basename(out)} ({info["sheets"]}시트) |',
         '', '- ※ 요율·구간·MG·채널 수수료 = 계약 설정값. 정산 자동화이며 회계·세무 자문 아님.']
    rep = os.path.join(HERE, 'royalty_verify.md')
    open(rep, 'w', encoding='utf-8').write('\n'.join(L))
    print('\n'.join(L))
    return ok1 and ok2 and ok3 and ok4 and ok5 and ok6


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    ok = main_demo()
    sys.exit(0 if ok else 1)
