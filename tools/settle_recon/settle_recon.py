#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""settle_recon.py — 카드 매출 마감 대사 (POS 승인 vs 실제 입금)

매장·온라인 사업자의 일 마감 질문: "오늘 통장에 들어온 카드사 입금이 왜 이 금액인가?"
카드 매출은 ①승인일≠입금일(카드사별 D+N 영업일) ②수수료 차감 후 입금 ③취소가 승인과
다른 날 끼어듦 — 그래서 통장과 POS가 눈으로는 안 맞는다. 이 도구가 그 사이를 잇는다.

로직:
  - POS 승인/취소 → 카드사·일자별 순매출 (취소는 ★취소일 기준으로 그날 정산분에서 차감)
  - 예상 입금 = 순매출 × (1-수수료), 입금일 = 정산 기준일 + D+N ★영업일(주말·공휴일 건너뜀)
  - 실제 입금 내역과 (입금일, 카드사) 단위 대조 → 일치 / ★미입금(예상 있는데 없음 = 최악) /
    금액 차이(수수료 오적용·취소 반영 차이 등) / 예정(아직 입금일 미도래)
  - 산출: 대사표 + 차이 명세 + 입금 예정표(캐시플로) 서식 엑셀

검증(--make-demo): 카드사 3(수수료·지연 상이) × 2주(주말+공휴일 1일 낀) — 입금 내역을 정답
  로직으로 생성하되 결함 심음(★입금 누락 1건·수수료 오적용 1건):
  ①영업일 수기 3케이스(금→화·휴일 낀 것) ②정상 건 전부 일치 ③심은 미입금 정확 검출
  ④심은 금액 차이 정확(차액까지) ⑤취소 교차일 수기 대조 ⑥항등(Σ예상=Σ순매출×(1-fee))+재현성.
※ 수수료율·입금 지연·라운딩·공휴일 = 설정값(카드사 정산 방식에 1회 맞춤). 회계·세무 자문 아님.
"""
import os, sys, csv, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', '..', 'core'))
from xlsx import write_workbook

CARDS = {'한빛카드': dict(fee=0.015, delay=2), '누리카드': dict(fee=0.020, delay=3),
         '바다카드': dict(fee=0.025, delay=2)}                 # 수수료·D+N 영업일(설정값)
HOLIDAYS = {'2026-09-08'}                                      # 공휴일(설정값) — 화요일로 심음


def D(s):
    return dt.date.fromisoformat(s)


def add_bdays(d, n, holidays=HOLIDAYS):
    """★영업일 더하기: 주말(토5·일6)과 공휴일을 건너뛴다."""
    cur = d
    while n > 0:
        cur += dt.timedelta(days=1)
        if cur.weekday() >= 5 or cur.isoformat() in holidays:
            continue
        n -= 1
    return cur


def build_expected(pos_rows, cards=CARDS):
    """POS → (카드사, 정산일)별 순매출 → 예상 입금 {(입금일, 카드사): (금액, 정산일, 순매출)}.
    취소 = 취소일 기준 그날 정산분에서 차감(카드사 방식). 입금액 = round(순매출×(1-fee)) 일괄."""
    net = {}
    for r in pos_rows:
        key = (r['카드사'], r['일자'])                          # 승인이든 취소든 그 행의 일자 기준
        amt = r['금액'] if r['구분'] == '승인' else -r['금액']
        net[key] = net.get(key, 0) + amt
    expected = {}
    for (card, day), amount in sorted(net.items()):
        c = cards[card]
        pay_day = add_bdays(D(day), c['delay']).isoformat()
        pay = round(amount * (1 - c['fee']))
        k = (pay_day, card)
        if k in expected:                                       # 같은 입금일에 두 정산일이 겹치면 합산
            expected[k] = (expected[k][0] + pay, expected[k][1] + f',{day}', expected[k][2] + amount)
        else:
            expected[k] = (pay, day, amount)
    return expected


def reconcile(expected, deposits, today):
    """실제 입금 {(입금일, 카드사): 금액} 대조 → (일치, 차이, 미입금, 예정, 예상외입금)"""
    ok, diff, missing, future, extra = [], [], [], [], []
    for (day, card), (exp, src, net_amt) in sorted(expected.items()):
        act = deposits.get((day, card))
        if D(day) > today:
            future.append((day, card, exp, src))
        elif act is None:
            missing.append((day, card, exp, src))               # ★미입금 = 최악, 반드시 드러남
        elif act == exp:
            ok.append((day, card, exp))
        else:
            diff.append((day, card, exp, act, act - exp, src))
    for (day, card), act in sorted(deposits.items()):
        if (day, card) not in expected:
            extra.append((day, card, act))                      # 예상에 없는 입금(과입금·이월 등)
    return ok, diff, missing, future, extra


def write_out(res, out):
    ok, diff, missing, future, extra = res
    sheets = {'대사 일치': (['입금일', '카드사', '금액'], [list(r) for r in ok])}
    if missing:
        sheets['★미입금(확인 필요)'] = (['예정 입금일', '카드사', '예상 금액', '정산일'],
                                    [list(r) for r in missing])
    if diff:
        sheets['금액 차이'] = (['입금일', '카드사', '예상', '실제', '차이', '정산일'],
                           [list(r) for r in diff])
    if extra:
        sheets['예상외 입금'] = (['입금일', '카드사', '금액'], [list(r) for r in extra])
    if future:
        sheets['입금 예정(캐시플로)'] = (['예정일', '카드사', '예상 금액', '정산일'], [list(r) for r in future])
    return write_workbook(out, sheets, summary={
        '생성': dt.datetime.now().strftime('%Y-%m-%d %H:%M'),
        '판정': f'일치 {len(ok)} · ★미입금 {len(missing)} · 차이 {len(diff)} · 예정 {len(future)} · 예상외 {len(extra)}',
        '규칙': '취소=취소일 정산분 차감 · 입금일=정산일+D+N영업일(주말·공휴일 스킵) · 입금액=round(순매출×(1-fee))',
        '주의': '수수료·지연·공휴일=설정값(카드사 방식 1회 맞춤). 대사 자동화이며 회계·세무 자문 아님'})


# ── 검증 데모 ───────────────────────────────────────────────────────
def make_demo():
    """승인 2주치(9/1 화~9/11 금, 주말 제외 영업일) + 취소 교차 + 공휴일 9/8 심음."""
    rows = []
    days = [d for d in ('2026-09-01', '2026-09-02', '2026-09-03', '2026-09-04',
                        '2026-09-07', '2026-09-09', '2026-09-10', '2026-09-11')]
    base = {'한빛카드': 400_000, '누리카드': 300_000, '바다카드': 200_000}
    for i, day in enumerate(days):
        for card, b in base.items():
            rows.append(dict(일자=day, 카드사=card, 금액=b + i * 10_000, 구분='승인'))
    # ★취소 교차: 9/1 승인 건을 9/3에 취소(취소일 정산분에서 차감)
    rows.append(dict(일자='2026-09-03', 카드사='한빛카드', 금액=50_000, 구분='취소'))
    pos_path = os.path.join(HERE, 'demo_pos.csv')
    with open(pos_path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['일자', '카드사', '금액', '구분'])
        w.writeheader()
        w.writerows(rows)

    expected = build_expected(rows)
    # 실제 입금 = 정답 로직 그대로 생성하되 결함 2건 심음(오늘 이전 입금분만 존재)
    today = D('2026-09-10')
    MISS_KEY = ('2026-09-04', '누리카드')                       # ★입금 누락(9/1 정산분 D+3)
    FEE_KEY = ('2026-09-07', '바다카드')                        # ★수수료 오적용(2.5%→3.5% 잘못 뗌)
    deposits = {}
    for (day, card), (exp, src, net_amt) in expected.items():
        if D(day) > today or (day, card) == MISS_KEY:
            continue
        if (day, card) == FEE_KEY:
            deposits[(day, card)] = round(net_amt * (1 - 0.035))
        else:
            deposits[(day, card)] = exp
    dep_path = os.path.join(HERE, 'demo_deposits.csv')
    with open(dep_path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['입금일', '카드사', '입금액'])
        w.writerows([d, c, a] for (d, c), a in sorted(deposits.items()))
    return rows, expected, deposits, today, MISS_KEY, FEE_KEY


def main_demo():
    rows, expected, deposits, today, MISS_KEY, FEE_KEY = make_demo()
    res = reconcile(expected, deposits, today)
    res2 = reconcile(expected, deposits, today)
    ok, diff, missing, future, extra = res

    # ① 영업일 수기 3케이스: 금(9/4)+2=화 9/8이 공휴일→수 9/9 · 금(9/4)+3영업일=9/10 · 목(9/3)+2=월 9/7
    ok1 = (add_bdays(D('2026-09-04'), 2).isoformat() == '2026-09-09'
           and add_bdays(D('2026-09-04'), 3).isoformat() == '2026-09-10'
           and add_bdays(D('2026-09-03'), 2).isoformat() == '2026-09-07')
    # ② 정상 건 전부 일치(심은 결함 2건 외 차이 0)
    ok2 = (len(diff) == 1 and len(missing) == 1 and not extra)
    # ③ ★미입금 정확: 누리카드 9/1 정산분(D+3=9/4) — 금액 = round(310,000... 9/1 누리 = 300,000+0)
    exp_miss = expected[MISS_KEY]
    ok3 = (missing[0][:3] == (MISS_KEY[0], MISS_KEY[1], exp_miss[0])
           and exp_miss[0] == round(300_000 * 0.98))
    # ④ 금액 차이 정확: 바다카드 오적용 — 차이 = round(net×0.965) - round(net×0.975)
    d0 = diff[0]
    net_fee = expected[FEE_KEY][2]
    ok4 = (d0[0] == FEE_KEY[0] and d0[1] == FEE_KEY[1]
           and d0[4] == round(net_fee * (1 - 0.035)) - round(net_fee * (1 - 0.025)))
    # ⑤ 취소 교차 수기: 9/3 한빛 순매출 = 420,000 - 50,000 = 370,000 → 입금 round(×0.985) @ 9/7
    k5 = ('2026-09-07', '한빛카드')
    ok5 = (expected[k5][2] == 370_000 and expected[k5][0] == round(370_000 * 0.985)
           and deposits[k5] == expected[k5][0])
    # ⑥ 항등: Σ예상 = Σ(정산묶음별 round(net×(1-fee))) — 정의 일치 + 재현성
    tot = sum(v[0] for v in expected.values())
    tot2 = sum(round(v[2] * (1 - CARDS[c]['fee'])) for (d, c), v in expected.items())
    ok6 = (tot == tot2 and res == res2 and len(future) > 0)

    out = os.path.join(HERE, '마감대사_데모.xlsx')
    info = write_out(res, out)

    L = [f'# 마감 대사 검증 리포트 ({dt.datetime.now():%Y-%m-%d %H:%M})',
         '- 데모 = 카드사 3(수수료 1.5/2.0/2.5%·D+2/3/2영업일) × 2주(주말+공휴일 9/8 심음) ·'
         ' 입금은 정답 로직 생성 + ★결함 2건(입금 누락·수수료 오적용) 심음',
         '', '| 검증 | 결과 |', '|---|---|',
         f'| ① ★영업일 수기 3케이스(금+2=공휴일 넘어 수요일 등) | {"PASS" if ok1 else "★FAIL"} |',
         f'| ② 정상 건 전부 일치(심은 2건 외 차이 0 = 오탐 0) | 일치 {len(ok)} → {"PASS" if ok2 else "★FAIL"} |',
         f'| ③ ★심은 미입금 정확 검출(카드사·일자·금액) | {"PASS" if ok3 else "★FAIL"} |',
         f'| ④ 심은 수수료 오적용 → 차액까지 정확 | {d0[4]:,}원 → {"PASS" if ok4 else "★FAIL"} |',
         f'| ⑤ ★취소 교차일 수기(9/1 승인·9/3 취소 → 9/3 정산 370,000) | {"PASS" if ok5 else "★FAIL"} |',
         f'| ⑥ 항등(Σ예상={tot:,}) + 입금 예정 {len(future)}건 + 재현성 | {"PASS" if ok6 else "★FAIL"} |',
         f'| 산출 | {os.path.basename(out)} ({info["sheets"]}시트) |',
         '', '- ※ 수수료율·입금 지연·공휴일·라운딩 = 설정값(카드사 정산 방식 1회 맞춤). 회계·세무 자문 아님.']
    rep = os.path.join(HERE, 'settle_recon_verify.md')
    open(rep, 'w', encoding='utf-8').write('\n'.join(L))
    print('\n'.join(L))
    return ok1 and ok2 and ok3 and ok4 and ok5 and ok6


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    ok = main_demo()
    sys.exit(0 if ok else 1)
