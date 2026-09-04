#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ops_board.py — 운영 대시보드 v2 (다중 소스·이력 추이·임계 경보·신선도)

단발 리포트가 아니라 **매일 보는 운영 판**. CSV 대시보드(v1)의 깊이판으로, 네 가지를 더한다:
  ① 다중 소스: CSV·SQLite·JSON을 지표 정의(METRICS)로 통합 — 소스가 늘어도 판은 하나
  ② ★이력 축적: 실행할 때마다 지표값을 history에 쌓아 스파크라인 추이로 — "지금"만이 아니라 "흐름"
  ③ ★임계 경보: 지표별 상한/하한 — 위반만 배지·경보(정상 지표는 조용히)
  ④ ★신선도 감시: 소스 파일의 데이터 나이 표시, 오래되면 스테일 경고 — 죽은 수집기가 만든
     "어제 숫자"를 오늘 것처럼 보는 사고 방지
산출 = 서버·설치 불필요 단일 HTML(자체 SVG 스파크라인·외부 의존 0, v1 계보).

검증(--make-demo): ①3형식 지표 독립 재계산 전수 일치 ②심은 임계 위반 2건만 경보(오탐 0)
  ③3회 실행 이력 3점·유실 0·스파크라인 실재 ④심은 스테일 소스만 경고 ⑤실브라우저 렌더
  스모크(콘솔 에러 0·배지 수 일치) ⑥재현성(같은 데이터 = 같은 지표값).
※ 지표 정의·임계·신선도 한도 = 설정값. 갱신 주기 = 스케줄러(크론)에 걸면 됨.
"""
import os, sys, csv, json, html, sqlite3, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
HISTORY = os.path.join(HERE, 'board_history.json')
STALE_H = 24                                                # 신선도 한도(시간, 설정값)

# 지표 정의(설정값): (이름, 소스형식, 소스경로, 추출, 임계 dict)
METRICS = [
    dict(name='일 주문 수', kind='csv', src='demo_orders.csv', agg='count',
         col=None, limit=dict(min=10)),
    dict(name='일 매출(원)', kind='csv', src='demo_orders.csv', agg='sum',
         col='금액', limit=dict(min=100_000)),
    dict(name='재고 부족 SKU', kind='sqlite', src='demo_stock.db',
         query='SELECT COUNT(*) FROM stock WHERE qty < 5', limit=dict(max=3)),
    dict(name='총 재고 수량', kind='sqlite', src='demo_stock.db',
         query='SELECT SUM(qty) FROM stock', limit=dict(min=100)),
    dict(name='서버 오류(24h)', kind='json', src='demo_health.json',
         path='errors_24h', limit=dict(max=5)),
    dict(name='평균 응답(ms)', kind='json', src='demo_health.json',
         path='avg_ms', limit=dict(max=500)),
]


def read_metric(m):
    p = os.path.join(HERE, m['src'])
    if m['kind'] == 'csv':
        rows = list(csv.DictReader(open(p, encoding='utf-8-sig')))
        if m['agg'] == 'count':
            return len(rows)
        return sum(int(str(r[m['col']]).replace(',', '')) for r in rows)
    if m['kind'] == 'sqlite':
        con = sqlite3.connect(p)
        v = con.execute(m['query']).fetchone()[0]
        con.close()
        return int(v or 0)
    d = json.load(open(p, encoding='utf-8'))
    return d[m['path']]


def age_hours(path, now=None):
    now = now or dt.datetime.now()
    return (now - dt.datetime.fromtimestamp(os.path.getmtime(path))).total_seconds() / 3600


def check_limit(value, limit):
    """위반 사유 문자열('' = 정상)."""
    if 'max' in limit and value > limit['max']:
        return f'상한 {limit["max"]:,} 초과'
    if 'min' in limit and value < limit['min']:
        return f'하한 {limit["min"]:,} 미달'
    return ''


def tick(metrics=METRICS, now=None, history_path=HISTORY):
    """1회 갱신: 지표 읽기 → 임계·신선도 판정 → 이력 append → dict 반환."""
    now = now or dt.datetime.now()
    hist = {}
    if os.path.exists(history_path):
        try:
            hist = json.load(open(history_path, encoding='utf-8'))
        except Exception:
            hist = {}
    out, alerts, stale = [], [], []
    for m in metrics:
        v = read_metric(m)
        why = check_limit(v, m.get('limit', {}))
        ah = age_hours(os.path.join(HERE, m['src']), now)
        is_stale = ah > STALE_H
        out.append(dict(name=m['name'], value=v, why=why, src=m['src'],
                        age_h=round(ah, 1), stale=is_stale))
        if why:
            alerts.append(f"🚨 {m['name']} = {v:,} ({why})")
        if is_stale and m['src'] not in [s.split(' ')[0] for s in stale]:
            stale.append(f"{m['src']} (데이터 나이 {ah:.0f}시간 > {STALE_H}h)")
        hist.setdefault(m['name'], []).append(v)
        hist[m['name']] = hist[m['name']][-60:]
    json.dump(hist, open(history_path, 'w', encoding='utf-8'), ensure_ascii=False)
    return dict(metrics=out, alerts=alerts, stale=sorted(set(stale)), hist=hist, at=now)


def spark(vals, w=110, h=26):
    """자체 SVG 스파크라인(외부 의존 0). 점 1개면 수평선."""
    if not vals:
        return ''
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1
    pts = [(i * (w - 4) / max(len(vals) - 1, 1) + 2,
            h - 3 - (v - lo) / rng * (h - 6)) for i, v in enumerate(vals)]
    poly = ' '.join(f'{x:.1f},{y:.1f}' for x, y in pts)
    return (f'<svg width="{w}" height="{h}"><polyline points="{poly}" fill="none" '
            f'stroke="#0c4a6e" stroke-width="2"/><circle cx="{pts[-1][0]:.1f}" '
            f'cy="{pts[-1][1]:.1f}" r="2.5" fill="#0c4a6e"/></svg>')


def render(res, out):
    cards = []
    for m in res['metrics']:
        badge = (f'<span class="bad">🚨 {html.escape(m["why"])}</span>' if m['why']
                 else '<span class="ok">정상</span>')
        st = f'<span class="stale">⏳ {m["age_h"]}h</span>' if m['stale'] else f'<span class="age">{m["age_h"]}h</span>'
        cards.append(f'''<div class="card{' alert' if m['why'] else ''}">
<div class="nm">{html.escape(m['name'])}</div><div class="val">{m['value']:,}</div>
{spark(res['hist'][m['name']])}<div class="ft">{badge} · 데이터 {st} · {html.escape(m['src'])}</div></div>''')
    stale_bar = ('<div class="stalebar">⏳ 스테일 소스: ' + ' · '.join(html.escape(s) for s in res['stale'])
                 + '</div>') if res['stale'] else ''
    doc = f"""<meta charset="utf-8"><title>운영 대시보드</title>
<style>body{{font-family:'Malgun Gothic',sans-serif;background:#f2f4f8;margin:0;padding:26px}}
h1{{font-size:20px;margin:0 0 4px}}.sub{{color:#5b6472;font-size:13px;margin-bottom:16px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:14px}}
.card{{background:#fff;border:1px solid #dfe4ea;border-radius:12px;padding:16px}}
.card.alert{{border:2px solid #b4232c;background:#fff7f7}}
.nm{{font-size:13px;color:#5b6472;font-weight:700}}.val{{font-size:26px;font-weight:900;margin:4px 0 6px}}
.ft{{font-size:11.5px;color:#8b95a3;margin-top:6px}}.bad{{color:#b4232c;font-weight:800}}
.ok{{color:#15803d;font-weight:700}}.stale{{color:#b45309;font-weight:800}}
.stalebar{{background:#fff3e0;border:1px solid #f3d9a4;border-radius:9px;padding:9px 13px;font-size:13px;margin-bottom:14px}}</style>
<h1>운영 대시보드 <span style="font-size:13px;color:#b4232c">{('경보 ' + str(len(res['alerts'])) + '건') if res['alerts'] else ''}</span></h1>
<div class="sub">갱신 {res['at']:%Y-%m-%d %H:%M} · 지표 {len(res['metrics'])}개 · 서버 불필요(단일 파일)</div>
{stale_bar}<div class="grid">{''.join(cards)}</div>"""
    open(out, 'w', encoding='utf-8').write(doc)
    return out


# ── 검증 데모 ───────────────────────────────────────────────────────
def make_demo():
    with open(os.path.join(HERE, 'demo_orders.csv'), 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['주문번호', '상품', '금액'])
        for i in range(23):                                 # 23건(하한 10 통과) · 합계 계산 가능
            w.writerow([f'O{i:03d}', '상품A', 12_000])
    con = sqlite3.connect(os.path.join(HERE, 'demo_stock.db'))
    con.execute('DROP TABLE IF EXISTS stock')
    con.execute('CREATE TABLE stock(sku TEXT, qty INT)')
    con.executemany('INSERT INTO stock VALUES(?,?)',
                    [(f'SKU{i}', q) for i, q in enumerate([50, 40, 3, 2, 1, 1, 30])])  # 부족 4(★상한 3 위반)
    con.commit()
    con.close()
    json.dump({'errors_24h': 2, 'avg_ms': 720},             # 오류 2 정상 · ★응답 720ms(상한 500 위반)
              open(os.path.join(HERE, 'demo_health.json'), 'w', encoding='utf-8'))


def main_demo():
    if os.path.exists(HISTORY):
        os.remove(HISTORY)
    make_demo()
    now = dt.datetime(2026, 9, 4, 12, 0)

    # ① 지표 독립 재계산: 주문 23 · 매출 23×12,000 · 부족 4 · 총재고 127 · 오류 2 · 응답 720
    r1 = tick(now=now)
    vals = {m['name']: m['value'] for m in r1['metrics']}
    ok1 = (vals == {'일 주문 수': 23, '일 매출(원)': 276_000, '재고 부족 SKU': 4,
                    '총 재고 수량': 127, '서버 오류(24h)': 2, '평균 응답(ms)': 720})
    # ② 심은 위반 2건만 경보(부족 4>3 · 응답 720>500), 정상 4개 오탐 0
    ok2 = (len(r1['alerts']) == 2
           and any('재고 부족' in a for a in r1['alerts'])
           and any('평균 응답' in a for a in r1['alerts']))
    # ③ 이력 3점: 2·3회차 실행 → 각 지표 이력 [v,v,v] 유실 0
    r2 = tick(now=now)
    r3 = tick(now=now)
    ok3 = all(len(r3['hist'][m['name']]) == 3 and len(set(r3['hist'][m['name']])) == 1
              for m in r3['metrics'])
    # ④ 신선도: demo_health.json mtime을 48시간 전으로 심음 → 그 소스만 스테일
    old = (now - dt.timedelta(hours=48)).timestamp()
    os.utime(os.path.join(HERE, 'demo_health.json'), (old, old))
    r4 = tick(now=now)
    stale_srcs = {m['src'] for m in r4['metrics'] if m['stale']}
    ok4 = (stale_srcs == {'demo_health.json'} and len(r4['stale']) == 1)
    # ⑥ 재현성: 같은 데이터 → 같은 지표값
    ok6 = ({m['name']: m['value'] for m in r4['metrics']} == vals)

    out = os.path.join(HERE, '운영대시보드_데모.html')
    render(r4, out)
    doc = open(out, encoding='utf-8').read()
    # ⑤ 렌더 스모크(실브라우저): 로드 OK·콘솔 에러 0·경보 카드 수 일치·스파크라인 존재
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            br = pw.chromium.launch()
            pg = br.new_page()
            errors = []
            pg.on('console', lambda m: errors.append(m.text) if m.type == 'error' else None)
            pg.goto('file:///' + out.replace(os.sep, '/'))
            n_alert = pg.locator('.card.alert').count()
            n_spark = pg.locator('svg polyline').count()
            br.close()
        ok5 = (not errors and n_alert == 2 and n_spark == 6)
        smoke = f'콘솔 에러 0 · 경보 카드 {n_alert}/2 · 스파크라인 {n_spark}/6'
    except Exception as e:
        ok5, smoke = False, f'렌더 실패 {type(e).__name__}'

    L = [f'# 운영 대시보드 v2 검증 리포트 ({dt.datetime.now():%Y-%m-%d %H:%M})',
         '- 데모 = 3형식 소스(CSV·SQLite·JSON) × 지표 6개 · ★임계 위반 2건·스테일 1소스 심음 · 3회 실행',
         '', '| 검증 | 결과 |', '|---|---|',
         f'| ① 지표 독립 재계산 전수 일치(6개, 3형식) | {"PASS" if ok1 else "★FAIL"} |',
         f'| ② 심은 임계 위반 2건만 경보(정상 4개 오탐 0) | {"PASS" if ok2 else "★FAIL"} |',
         f'| ③ 이력 축적 3회 실행 = 3점·유실 0 | {"PASS" if ok3 else "★FAIL"} |',
         f'| ④ ★신선도: 48h 묵힌 소스만 스테일 경고 | {"PASS" if ok4 else "★FAIL"} |',
         f'| ⑤ 실브라우저 렌더 스모크({smoke}) | {"PASS" if ok5 else "★FAIL"} |',
         f'| ⑥ 재현성(같은 데이터=같은 지표) | {"PASS" if ok6 else "★FAIL"} |',
         '', '- ※ 지표 정의·임계·신선도 한도 = 설정값. 주기 갱신 = 크론/스케줄러 1줄. 단일 HTML·외부 의존 0.']
    rep = os.path.join(HERE, 'ops_board_verify.md')
    open(rep, 'w', encoding='utf-8').write('\n'.join(L))
    print('\n'.join(L))
    return ok1 and ok2 and ok3 and ok4 and ok5 and ok6


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    if '--tick' in sys.argv:       # 실사용: 크론에서 주기 실행 → HTML 갱신
        res = tick()
        render(res, os.path.join(HERE, 'ops_board.html'))
        print(f"갱신 완료 — 경보 {len(res['alerts'])}건 · 스테일 {len(res['stale'])}소스")
        for a in res['alerts']:
            print(' ', a)
        sys.exit(1 if res['alerts'] else 0)
    ok = main_demo()
    sys.exit(0 if ok else 1)
