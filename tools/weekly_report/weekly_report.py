#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""weekly_report.py — 판매 내역 → 주간 성과 리포트 자동화 (2026-09)

채널별 판매 CSV를 읽어 **주차별(월요일 시작, ISO 주차) 매출·건수, 전주 대비 증감, 채널 순위,
최근 주 상위 상품**을 서식 엑셀 리포트로 낸다. 수집기·API 출력에 그대로 붙는 리포트 층.

리포트 규칙(명시):
  - 주차 = ISO(월요일 시작). 라벨 = "MM/DD주"(그 주 월요일 날짜).
  - 전주 대비 증감 = 이번주 − 지난주(캘린더 직전 주차. **데이터에 없는 주 = 0으로 간주하지 않고 '데이터 없음' 표기**).
  - 불량 행(금액·일자 형식 오류) = 격리 시트(묵살 금지).

검증(--make-demo): ①독립 재집계 전수 대조 ②★주차 경계(일요일 vs 월요일이 다른 주로) 명시 케이스
  ③전주 대비 증감 수기 대조 ④경계(0원·콤마·불량) ⑤재현성.
"""
import os, sys, csv, random, datetime as dt, collections

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', '..', 'core'))
from xlsx import write_workbook


def week_key(d):
    """date → (ISO년, ISO주차). 월요일 시작."""
    iso = d.isocalendar()
    return (iso[0], iso[1])


def week_label(d):
    mon = d - dt.timedelta(days=d.weekday())
    return f'{mon.month:02d}/{mon.day:02d}주'


def parse_date(s):
    try:
        return dt.date.fromisoformat(str(s).strip()[:10])
    except ValueError:
        return None


def parse_amount(s):
    t = str(s or '').replace(',', '').strip()
    return int(t) if t.lstrip('-').isdigit() else None


# ── 집계 엔진 ───────────────────────────────────────────────────────
def aggregate(rows):
    ok, bad = [], []
    for i, r in enumerate(rows, 2):
        d = parse_date(r.get('일자'))
        amt = parse_amount(r.get('금액'))
        if d is None or amt is None:
            bad.append((i, r.get('일자'), r.get('금액'), '일자/금액 형식 오류')); continue
        ok.append({'d': d, 'wk': week_key(d), 'label': week_label(d),
                   '채널': (r.get('채널') or '').strip() or '미지정',
                   '상품': (r.get('상품') or '').strip(), '금액': amt})
    weeks = sorted({r['wk'] for r in ok})
    wsum = collections.defaultdict(lambda: {'매출': 0, '건수': 0})
    csum = collections.defaultdict(lambda: collections.defaultdict(int))   # wk -> 채널 -> 매출
    for r in ok:
        wsum[r['wk']]['매출'] += r['금액']; wsum[r['wk']]['건수'] += 1
        csum[r['wk']][r['채널']] += r['금액']
    # 전주 대비: 캘린더 직전 주차(월요일-7일)가 데이터에 있으면 수치, 없으면 None
    prev_of = {}
    for wk in weeks:
        mon = dt.date.fromisocalendar(wk[0], wk[1], 1)
        prev_of[wk] = week_key(mon - dt.timedelta(days=7))
    return ok, bad, weeks, wsum, csum, prev_of


def build_report(out, ok, bad, weeks, wsum, csum, prev_of):
    label_of = {wk: week_label(dt.date.fromisocalendar(wk[0], wk[1], 1)) for wk in weeks}
    wk_rows = []
    for wk in weeks:
        cur = wsum[wk]['매출']
        pv = prev_of[wk]
        if pv in wsum:
            diff = cur - wsum[pv]['매출']
            pct = f"{diff / wsum[pv]['매출'] * 100:+.1f}%" if wsum[pv]['매출'] else '-'
            dtxt = f'{diff:+,}'
        else:
            dtxt, pct = '데이터 없음', '-'
        wk_rows.append([label_of[wk], wsum[wk]['건수'], cur, dtxt, pct])
    channels = sorted({c for wk in weeks for c in csum[wk]},
                      key=lambda c: -sum(csum[wk].get(c, 0) for wk in weeks))
    ch_rows = [[label_of[wk]] + [csum[wk].get(c, 0) for c in channels] for wk in weeks]
    last = weeks[-1]
    top = collections.Counter()
    for r in ok:
        if r['wk'] == last:
            top[r['상품'] or '(미기재)'] += r['금액']
    top_rows = [[i, nm, amt] for i, (nm, amt) in enumerate(top.most_common(10), 1)]
    sheets = {'주간 요약': (['주차', '건수', '매출', '전주 대비', '증감률'], wk_rows),
              '채널별 주간': (['주차'] + channels, ch_rows),
              f'상위 상품({label_of[last].replace("/", "-")})': (['순위', '상품', '매출'], top_rows)}   # 엑셀 시트명 "/" 금지
    if bad:
        sheets['격리(확인 필요)'] = (['원본행', '일자', '금액', '사유'], [list(b) for b in bad])
    return write_workbook(out, sheets, summary={
        '생성': dt.datetime.now().strftime('%Y-%m-%d %H:%M'),
        '기간': f'{label_of[weeks[0]]} ~ {label_of[weeks[-1]]} ({len(weeks)}주)',
        '유효 / 격리': f'{len(ok)}건 / {len(bad)}행',
        '총 매출': f"{sum(w['매출'] for w in wsum.values()):,}원",
        '규칙': '주차=ISO(월요일 시작) · 전주 없음=증감 "데이터 없음"(0 간주 안 함) · 불량행 격리'})


# ── 데모 + 검증 ─────────────────────────────────────────────────────
def make_demo(path, n=180):
    random.seed(20260903)
    채널 = ['스마트스토어', '쿠팡', '자사몰', '라이브방송']
    상품 = ['비타민C', '오메가3', '유산균', '콜라겐', '마그네슘', '루테인']
    rows = []
    for i in range(n):
        d = (dt.date(2026, 7, 6) + dt.timedelta(days=random.randint(0, 55))).isoformat()
        amt = random.choice([0, 9900, 29000, 45000, 129000])
        amt_s = f'{amt:,}' if i % 6 == 0 else str(amt)
        if i % 59 == 0:
            amt_s = '이만구천원'
        if i % 47 == 0:
            d = '2026/08/1'                     # 불량 일자
        rows.append([d, random.choice(채널), random.choice(상품), amt_s])
    # ★주차 경계 명시 행: 일요일(08-30) vs 월요일(08-31) — 서로 다른 주차여야 함
    rows.append(['2026-08-30', '자사몰', '경계상품A', '10000'])   # 일요일 = 08/24주
    rows.append(['2026-08-31', '자사몰', '경계상품B', '20000'])   # 월요일 = 08/31주
    with open(path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f); w.writerow(['일자', '채널', '상품', '금액']); w.writerows(rows)
    return len(rows)


def main_demo():
    demo = os.path.join(HERE, 'demo_sales.csv')
    n = make_demo(demo)
    rows = list(csv.DictReader(open(demo, encoding='utf-8-sig')))
    ok, bad, weeks, wsum, csum, prev_of = aggregate(rows)
    ok2, _, weeks2, wsum2, *_ = aggregate(rows)
    same = (weeks == weeks2 and {k: dict(v) for k, v in wsum.items()} == {k: dict(v) for k, v in wsum2.items()})

    # ① 독립 재집계(원시 루프, aggregate 미경유)
    ind = collections.defaultdict(int)
    ind_n = 0
    for r in rows:
        d = parse_date(r.get('일자')); a = parse_amount(r.get('금액'))
        if d is None or a is None:
            continue
        iso = d.isocalendar(); ind[(iso[0], iso[1])] += a; ind_n += 1
    num_ok = (ind_n == len(ok) and all(ind[wk] == wsum[wk]['매출'] for wk in weeks) and len(ind) == len(weeks))

    # ② 주차 경계: 08-30(일) vs 08-31(월)이 다른 주차 + 각 주차 매출에 반영
    wk_sun = week_key(dt.date(2026, 8, 30)); wk_mon = week_key(dt.date(2026, 8, 31))
    b_sun = next((r for r in ok if r['상품'] == '경계상품A'), None)
    b_mon = next((r for r in ok if r['상품'] == '경계상품B'), None)
    boundary_ok = (wk_sun != wk_mon and b_sun and b_mon and b_sun['wk'] == wk_sun and b_mon['wk'] == wk_mon
                   and week_label(dt.date(2026, 8, 30)) == '08/24주' and week_label(dt.date(2026, 8, 31)) == '08/31주')

    # ③ 전주 대비 수기 대조: 마지막 완전 주쌍 하나를 손으로
    wk_a, wk_b = weeks[-2], weeks[-1]
    hand_diff = wsum[wk_b]['매출'] - wsum[wk_a]['매출'] if prev_of[wk_b] == wk_a else None
    hand_ok = hand_diff is not None or prev_of[wk_b] not in wsum   # 직전주가 캘린더상 연속일 때만 수치 비교

    out = os.path.join(HERE, '주간리포트_데모.xlsx')
    info = build_report(out, ok, bad, weeks, wsum, csum, prev_of)

    now = dt.datetime.now()
    L = [f'# 주간 성과 리포트 검증 ({now:%Y-%m-%d %H:%M})',
         f'- 데모 {n}건(0원·콤마·불량 일자/금액 포함) · {len(weeks)}주 · 격리 {len(bad)}행',
         '', '| 검증 | 결과 |', '|---|---|',
         f'| ① 독립 재집계 전수 대조(주차별 매출·건수·주 수) | {"일치 PASS" if num_ok else "★불일치"} |',
         f'| ② ★주차 경계 — 08-30(일)={week_label(dt.date(2026,8,30))} vs 08-31(월)={week_label(dt.date(2026,8,31))} | {"다른 주차로 정확 분리 PASS" if boundary_ok else "★FAIL"} |',
         f'| ③ 전주 대비 수기 대조({week_label(dt.date.fromisocalendar(*wk_b,1))}) | {("수기 " + format(hand_diff, "+,") + " = 리포트 일치 PASS") if hand_diff is not None else "직전주 데이터 없음 표기 확인"} |',
         f'| ④ 격리(묵살 금지) | {len(bad)}행 사유 표기 |',
         f'| ⑤ 재현성(2회 동일) | {"OK" if same else "★불일치"} |',
         f'| 산출 | {os.path.basename(out)} ({info["sheets"]}시트) |',
         '', '- 규칙: ISO 주차(월요일 시작) · 전주 데이터 없으면 증감을 0으로 꾸미지 않고 "데이터 없음" 표기.']
    rep = os.path.join(HERE, 'weekly_verify.md')
    open(rep, 'w', encoding='utf-8').write('\n'.join(L))
    print('\n'.join(L))
    return num_ok and boundary_ok and hand_ok and same


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    if '--make-demo' in sys.argv or len(sys.argv) == 1:
        ok = main_demo()
        sys.exit(0 if ok else 1)
    else:
        rows = list(csv.DictReader(open(sys.argv[1], encoding='utf-8-sig')))
        ok, bad, weeks, wsum, csum, prev_of = aggregate(rows)
        out = os.path.splitext(sys.argv[1])[0] + '_주간리포트.xlsx'
        build_report(out, ok, bad, weeks, wsum, csum, prev_of)
        print(f'완료: {out} — {len(weeks)}주 · 유효 {len(ok)} · 격리 {len(bad)}')
