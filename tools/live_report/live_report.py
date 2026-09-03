#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""live_report.py — 라이브 방송·판매 성과 리포트 (방송별 매출 귀속)

라이브커머스/홈쇼핑식 방송의 성과를 주문 데이터에서 **방송별로 귀속**해 리포트한다.


이 도메인의 진짜 문제 = ★귀속(attribution): "이 주문은 어느 방송의 성과인가?"
  - 방송중 주문: [시작, 종료] 안 + 그 방송의 상품 → 그 방송
  - 후속 주문: 종료 후 N시간(설정, 기본 3h) 안 + 그 방송의 상품 → 그 방송의 '후속'
  - ★겹침 규칙: 앞 방송의 후속 윈도우와 다음 방송의 생방이 겹치면 **방송중 우선**
    (같은 상품이 두 방송에 다 걸리면 지금 방송 중인 쪽의 성과)
  - 어디에도 안 걸리면 '제외'(윈도우 밖·비방송 상품) — 버리지 않고 사유와 함께 집계

산출: 방송별 요약(방송중/후속 분리) · 상품별 기여 · 회차 비교(증감) · 귀속 규칙 명시 — 서식 엑셀.

검증(--make-demo) = **정답지 선작성**: 주문마다 기대 귀속 라벨을 먼저 정해 놓고 데이터를 합성 →
  ①주문 1건 단위 귀속 전수 일치 ②경계 4케이스(시작 정각=포함·종료 정각=포함·후속끝 정각=포함·+1분=제외)
  ③겹침 케이스 → 방송중 우선 ④총액 보존(귀속 합+제외 = 전체) + 독립 재집계 ⑤회차 증감 수기 대조 ⑥재현성.
※ 주문·방송 데이터 = 플랫폼 추출 CSV 가정(수집 연동은 별도 모듈). 윈도우·규칙 = 설정값.
"""
import os, sys, csv, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', '..', 'core'))
from xlsx import write_workbook

FOLLOW_H = 3        # 후속 귀속 윈도우(시간) — 설정값


def T(s):
    return dt.datetime.strptime(s, '%Y-%m-%d %H:%M')


def load_broadcasts(path):
    """방송ID,시작,종료,상품목록(;구분) → [dict]"""
    out = []
    for r in csv.DictReader(open(path, encoding='utf-8-sig')):
        out.append(dict(bid=r['방송ID'], start=T(r['시작']), end=T(r['종료']),
                        products=set(p.strip() for p in r['상품목록'].split(';') if p.strip())))
    return sorted(out, key=lambda b: b['start'])


def attribute(order_time, product, broadcasts, follow_h=FOLLOW_H):
    """주문 1건 귀속 → (방송ID, '방송중'/'후속') 또는 (None, 제외사유).
    ★방송중 우선: 생방 매칭을 전 방송에서 먼저 찾고, 없을 때만 후속 윈도우를 본다."""
    for b in broadcasts:                                   # 1순위: 방송중
        if b['start'] <= order_time <= b['end'] and product in b['products']:
            return b['bid'], '방송중'
    cands = []
    for b in broadcasts:                                   # 2순위: 후속(종료 후 N시간)
        if b['end'] < order_time <= b['end'] + dt.timedelta(hours=follow_h) and product in b['products']:
            cands.append(b)
    if cands:
        latest = max(cands, key=lambda b: b['end'])        # 여러 방송 후속에 걸치면 최근 방송
        return latest['bid'], '후속'
    return None, '제외(윈도우 밖 또는 비방송 상품)'


def build_report(broadcasts, orders, follow_h=FOLLOW_H):
    """orders = [dict(time, product, qty, amount)] → (per_order, 방송별 집계, 제외집계)"""
    per_order = []
    agg = {b['bid']: {'방송중': [0, 0], '후속': [0, 0]} for b in broadcasts}   # [건수, 금액]
    excluded = [0, 0]
    for o in orders:
        bid, tag = attribute(o['time'], o['product'], broadcasts, follow_h)
        per_order.append(dict(o, bid=bid, tag=tag))
        if bid is None:
            excluded[0] += 1
            excluded[1] += o['amount']
        else:
            agg[bid][tag][0] += 1
            agg[bid][tag][1] += o['amount']
    return per_order, agg, excluded


def write_report(broadcasts, per_order, agg, excluded, out):
    rows = []
    order_of = {b['bid']: b for b in broadcasts}
    prev_total = None
    for b in broadcasts:
        a = agg[b['bid']]
        total = a['방송중'][1] + a['후속'][1]
        delta = '' if prev_total is None else f'{total - prev_total:+,}'
        rows.append([b['bid'], b['start'].strftime('%m-%d %H:%M'), b['end'].strftime('%H:%M'),
                     a['방송중'][0], a['방송중'][1], a['후속'][0], a['후속'][1],
                     a['방송중'][0] + a['후속'][0], total, delta])
        prev_total = total
    prod = {}
    for o in per_order:
        if o['bid']:
            k = (o['bid'], o['product'])
            prod.setdefault(k, [0, 0])
            prod[k][0] += o['qty']
            prod[k][1] += o['amount']
    prows = [[b, p, v[0], v[1]] for (b, p), v in sorted(prod.items())]
    return write_workbook(out, {
        '방송별 성과': (['방송', '시작', '종료', '방송중 건', '방송중 매출', '후속 건', '후속 매출',
                     '합계 건', '합계 매출', '전회 대비'], rows),
        '상품별 기여': (['방송', '상품', '수량', '매출'], prows),
        '제외 주문': (['건수', '금액'], [[excluded[0], excluded[1]]]),
    }, summary={'생성': dt.datetime.now().strftime('%Y-%m-%d %H:%M'),
                '귀속 규칙': f'방송중 [시작,종료] 우선 → 후속 (종료, +{FOLLOW_H}h] · 겹침=방송중 우선 · 그 외 제외',
                '총액 보존': f"귀속 {sum(a['방송중'][1]+a['후속'][1] for a in agg.values()):,} + 제외 {excluded[1]:,}"
                          f" = 전체 {sum(o['amount'] for o in per_order):,}"})


# ── 검증 데모: 정답지를 먼저 쓰고 데이터를 합성 ────────────────────
def make_demo():
    """반환 (broadcasts, orders(기대 라벨 포함)). 라벨 = B1중/B1후속/B2중/B2후속/B3중/B3후속/제외"""
    b_path = os.path.join(HERE, 'demo_broadcasts.csv')
    with open(b_path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['방송ID', '시작', '종료', '상품목록'])
        w.writerow(['B1', '2026-09-02 18:00', '2026-09-02 19:00', '새싹삼 세트;도라지청'])
        w.writerow(['B2', '2026-09-02 20:00', '2026-09-02 21:30', '도라지청;유자차 선물세트'])
        w.writerow(['B3', '2026-09-03 11:00', '2026-09-03 12:00', '새싹삼 세트;유자차 선물세트'])
    # ★상품별 단가표 — 1차 검증 검거: 같은 상품이 방송마다 다른 단가로 합성돼 수기 대조 상수가 어긋남
    PRICE = {'새싹삼 세트': 29000, '도라지청': 15000, '유자차 선물세트': 33000}
    spec = []                                              # (시각, 상품, 금액, 기대bid, 기대tag)
    A = lambda t, p, bid, tag: spec.append((t, p, PRICE[p], bid, tag))
    # B1 방송중 6건 · 후속 4건(19:05~19:55 — B2 시작 전)
    for i, mm in enumerate(['18:05', '18:12', '18:25', '18:40', '18:52', '18:59']):
        A(f'2026-09-02 {mm}', '새싹삼 세트' if i % 2 else '도라지청', 'B1', '방송중')
    for mm in ['19:05', '19:20', '19:40', '19:55']:
        A(f'2026-09-02 {mm}', '새싹삼 세트', 'B1', '후속')
    # ★겹침 1건: 20:30 도라지청 — B1 후속 윈도우(~22:00) 안이지만 B2 생방 상품 → 방송중 우선 = B2
    A('2026-09-02 20:30', '도라지청', 'B2', '방송중')
    # B2 방송중 4건 + 후속 3건(21:35~23:50)
    for mm in ['20:05', '20:44', '21:10', '21:29']:
        A(f'2026-09-02 {mm}', '유자차 선물세트', 'B2', '방송중')
    for mm in ['21:35', '22:40', '23:50']:
        A(f'2026-09-02 {mm}', '유자차 선물세트', 'B2', '후속')
    # B3 방송중 5건 + ★경계 2건(시작 정각 11:00 포함·종료 정각 12:00 포함) + 후속 1건 + ★경계 2건(후속끝 15:00 포함·15:01 제외)
    for mm in ['11:07', '11:15', '11:30', '11:44', '11:58']:
        A(f'2026-09-03 {mm}', '새싹삼 세트', 'B3', '방송중')
    A('2026-09-03 11:00', '유자차 선물세트', 'B3', '방송중')             # 경계: 시작 정각
    A('2026-09-03 12:00', '새싹삼 세트', 'B3', '방송중')                 # 경계: 종료 정각
    A('2026-09-03 13:20', '유자차 선물세트', 'B3', '후속')
    A('2026-09-03 15:00', '새싹삼 세트', 'B3', '후속')                   # 경계: 후속끝 정각
    A('2026-09-03 15:01', '새싹삼 세트', None, '제외')                   # 경계: +1분
    # 제외 4건: 윈도우 밖 2 · 방송중 시각의 비방송 상품 2
    A('2026-09-02 10:00', '도라지청', None, '제외')
    A('2026-09-03 23:00', '유자차 선물세트', None, '제외')
    A('2026-09-02 18:30', '유자차 선물세트', None, '제외')               # B1 생방 시각, B1 상품 아님
    A('2026-09-03 11:30', '도라지청', None, '제외')                      # B3 생방 시각, B3 상품 아님
    o_path = os.path.join(HERE, 'demo_orders.csv')
    with open(o_path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['주문시각', '상품', '수량', '금액'])
        for t, p, amt, _, _ in spec:
            w.writerow([t, p, 1, amt])
    return b_path, o_path, spec


def main_demo():
    b_path, o_path, spec = make_demo()
    broadcasts = load_broadcasts(b_path)
    orders = [dict(time=T(r['주문시각']), product=r['상품'], qty=int(r['수량']), amount=int(r['금액']))
              for r in csv.DictReader(open(o_path, encoding='utf-8-sig'))]
    per_order, agg, excluded = build_report(broadcasts, orders)
    per_order2, agg2, _ = build_report(broadcasts, orders)

    # ① 주문 1건 단위 귀속 = 정답지 전수 일치
    mism = [(s, (o['bid'], o['tag'])) for s, o in zip(spec, per_order)
            if (s[3], '제외' if s[3] is None else s[4]) !=
               (o['bid'], '제외' if o['bid'] is None else o['tag'])]
    ok1 = (not mism)
    # ② 경계 4케이스(정답지 라벨에 포함 — 명시 재확인)
    edge = {t: next(o for o in per_order if o['time'] == T(t))
            for t in ['2026-09-03 11:00', '2026-09-03 12:00', '2026-09-03 15:00', '2026-09-03 15:01']}
    ok2 = (edge['2026-09-03 11:00']['tag'] == '방송중' and edge['2026-09-03 12:00']['tag'] == '방송중'
           and edge['2026-09-03 15:00']['tag'] == '후속' and edge['2026-09-03 15:01']['bid'] is None)
    # ③ 겹침 → 방송중 우선
    ov = next(o for o in per_order if o['time'] == T('2026-09-02 20:30'))
    ok3 = (ov['bid'] == 'B2' and ov['tag'] == '방송중')
    # ④ 총액 보존 + 독립 재집계(단순 필터 합으로 B2 재계산)
    total_all = sum(o['amount'] for o in orders)
    total_attr = sum(a['방송중'][1] + a['후속'][1] for a in agg.values())
    b2_indep = sum(o['amount'] for o in per_order if o['bid'] == 'B2')
    b2_rep = agg['B2']['방송중'][1] + agg['B2']['후속'][1]
    ok4 = (total_attr + excluded[1] == total_all and b2_indep == b2_rep)
    # ⑤ 회차 증감 수기: B1합 = 방송중(도라지청3×15,000+새싹삼3×29,000)+후속(새싹삼4×29,000) = 248,000
    #    B2합 = 겹침 도라지청 15,000 + 유자차 7×33,000 = 246,000 → 증감 -2,000
    b1t = agg['B1']['방송중'][1] + agg['B1']['후속'][1]
    hand = b2_rep - b1t
    ok5 = (b1t == 248000 and b2_rep == 246000 and hand == -2000)
    # ⑥ 재현성
    ok6 = (per_order == per_order2 and agg == agg2)

    out = os.path.join(HERE, '방송성과_데모.xlsx')
    info = write_report(broadcasts, per_order, agg, excluded, out)

    L = [f'# 방송 성과 리포트 검증 ({dt.datetime.now():%Y-%m-%d %H:%M})',
         f'- 데모 = 방송 3회 × 주문 {len(spec)}건 — **정답지(주문별 기대 귀속)를 먼저 쓰고 데이터를 합성** →'
         ' 도구 산출과 대조. 경계·겹침·제외 전부 심음',
         '', '| 검증 | 결과 |', '|---|---|',
         f'| ① 주문 1건 단위 귀속 전수 일치({len(spec)}건) | 불일치 {len(mism)} → {"PASS" if ok1 else "★FAIL"} |',
         f'| ② 경계 4케이스(시작·종료·후속끝 정각=포함 / +1분=제외) | {"PASS" if ok2 else "★FAIL"} |',
         f'| ③ ★겹침(B1 후속 윈도우 ∩ B2 생방) → 방송중 우선 | {"PASS" if ok3 else "★FAIL"} |',
         f'| ④ 총액 보존(귀속 {total_attr:,}+제외 {excluded[1]:,}={total_all:,}) + B2 독립 재집계 | {"PASS" if ok4 else "★FAIL"} |',
         f'| ⑤ 회차 증감 수기 대조(B2-B1 = {hand:+,}) | {"PASS" if ok5 else "★FAIL"} |',
         f'| ⑥ 재현성(2회 동일) | {"OK" if ok6 else "★불일치"} |',
         f'| 산출 | {os.path.basename(out)} ({info["sheets"]}시트) |',
         '', f'- 귀속 규칙(명시): 방송중 [시작,종료] 우선 → 후속 (종료, +{FOLLOW_H}h] · 겹침=방송중 우선 · 그 외 제외(버리지 않고 집계)',
         '- ※ 주문·방송 데이터 = 플랫폼 추출 CSV 가정(플랫폼 수집 연동은 별도 모듈로 맞춤). 윈도우 시간 = 설정값.']
    rep = os.path.join(HERE, 'live_report_verify.md')
    open(rep, 'w', encoding='utf-8').write('\n'.join(L))
    print('\n'.join(L))
    return ok1 and ok2 and ok3 and ok4 and ok5 and ok6


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    ok = main_demo()
    sys.exit(0 if ok else 1)
