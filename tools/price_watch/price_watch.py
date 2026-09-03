#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""price_watch.py — 경쟁가 추적·변동 알림 (가격 이력·최저가 지위·노이즈 억제)

경쟁 상품 가격을 주기 수집해 이력을 쌓고, **의미 있는 변동만** 알린다. 감시 엔진 = core/watch Watcher — 첫 실행 폭탄 0·중복 0·발송 실패 보류 계약 재사용.

알림 이벤트 (전부 명시 규칙):
  - 가격 변동: **임계(기본 1%) 이상만** — 미세 노이즈(반올림·수시 변동)로 알림이 걸레가 되는 것 방지
  - ★최저가 지위 변화: 우리 몰이 최저가를 뺏기거나 탈환하면 알림 (재고 있는 상품 기준)
  - 품절/복구: 품절은 **가격 이력을 0원으로 오염시키지 않고** 상태로만 기록(가격=NULL)
  - 신규 관측 상품
산출: 가격 이력(SQLite) + 현재가 비교표(몰×상품, 최저 표시) 엑셀.

검증(--make-demo) = 몰 3곳 데모 서버(HTML 테이블 / HTML 카드 / JSON API — 구조 전부 다름, 서버가 진실):
  ①파싱 전수 대조(12항목 가격·재고) ②첫 틱 알림 0(폭탄 방지) ③심은 변동만 정확 알림(인하·인상·
  품절·최저가 뺏김×2 = 5건, ★0.5% 노이즈는 0건) ④같은 상태 재수집 = 0건 ⑤복구·인하·탈환×2 = 4건
  ⑥이력 유실 0(4틱×12=48행) · 품절 행 가격 NULL · 최저가 계산에서 품절 제외.
※ 데모 = 로컬 몰 서버. 실사이트 = 어댑터 1회 맞춤(대상 약관·robots 확인 후 진행 원칙).
"""
import os, sys, re, json, time, threading, sqlite3, datetime as dt
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', '..', 'core'))
from watch import Watcher
from xlsx import write_workbook

DB = os.path.join(HERE, 'price_history.db')
STATE = os.path.join(HERE, 'price_state.json')
PCT_TH = 1.0          # 가격 변동 알림 임계(%) — 설정값
OURS = '우리몰'        # 최저가 지위 추적 기준 몰 — 설정값


# ── 어댑터 3종 (실사이트에선 이 함수들만 1회 맞춤) ─────────────────
def _get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.read().decode('utf-8')


def parse_a(base):
    """우리몰: HTML 테이블 — <td class="name">..</td><td class="price">12,900원</td><td class="stock">판매중|품절</td>"""
    html = _get(f'{base}/mallA')
    out = {}
    for name, price, stock in re.findall(
            r'<td class="name">(.*?)</td><td class="price">([\d,]+)원</td><td class="stock">(판매중|품절)</td>', html):
        out[('우리몰', name)] = dict(price=int(price.replace(',', '')), stock=(stock == '판매중'))
    return out


def parse_b(base):
    """경쟁B: HTML 카드 — <h3>이름</h3><p class="cost">₩12,900</p>(<p class="badge">품절</p>)"""
    html = _get(f'{base}/mallB')
    out = {}
    for block in re.findall(r'<div class="item">(.*?)</div>', html, re.S):
        name = re.search(r'<h3>(.*?)</h3>', block).group(1)
        price = int(re.search(r'<p class="cost">₩([\d,]+)</p>', block).group(1).replace(',', ''))
        out[('경쟁B', name)] = dict(price=price, stock=('품절' not in block))
    return out


def parse_c(base):
    """경쟁C: JSON API — {"products":[{"title","sale_price","soldout"}]}"""
    d = json.loads(_get(f'{base}/mallC'))
    return {('경쟁C', p['title']): dict(price=int(p['sale_price']), stock=not p['soldout'])
            for p in d['products']}


ADAPTERS = [parse_a, parse_b, parse_c]


def collect(base):
    cur = {}
    for fn in ADAPTERS:
        cur.update(fn(base))
    return cur


# ── 이벤트 판정 (differ — 규칙 전부 명시) ──────────────────────────
def lowest_by_product(items):
    """{상품: (몰, 가격)} — ★재고 있는 것만(품절가로 최저 오염 금지)"""
    best = {}
    for (mall, prod), v in items.items():
        if not v['stock']:
            continue
        if prod not in best or v['price'] < best[prod][1]:
            best[prod] = (mall, v['price'])
    return best


def make_differ(tick_no):
    def differ(prev, cur):
        events = []
        prev_items = {tuple(k.split('|', 1)): v for k, v in prev.items()}
        for (mall, prod), c in sorted(cur.items()):
            p = prev_items.get((mall, prod))
            kid = f'{mall}|{prod}'
            if p is None:
                events.append((f'new|{kid}|{tick_no}', f'🆕 신규 관측 [{mall}] {prod} {c["price"]:,}원'))
                continue
            if p['stock'] and not c['stock']:
                events.append((f'out|{kid}|{tick_no}', f'⛔ 품절 [{mall}] {prod}'))
            if (not p['stock']) and c['stock']:
                events.append((f'back|{kid}|{tick_no}', f'✅ 재입고 [{mall}] {prod} {c["price"]:,}원'))
            if p['price'] != c['price']:
                pct = (c['price'] - p['price']) / p['price'] * 100
                if abs(pct) >= PCT_TH:
                    arrow = '📉 인하' if pct < 0 else '📈 인상'
                    events.append((f'chg|{kid}|{p["price"]}->{c["price"]}|{tick_no}',
                                   f'{arrow} [{mall}] {prod} {p["price"]:,} → {c["price"]:,}원 ({pct:+.1f}%)'))
        # ★최저가 지위 변화(우리 몰 관련만)
        lo_p, lo_c = lowest_by_product(prev_items), lowest_by_product(cur)
        for prod in sorted(set(lo_p) | set(lo_c)):
            mp = lo_p.get(prod, (None, None))[0]
            mc = lo_c.get(prod, (None, None))[0]
            if mp != mc and OURS in (mp, mc):
                if mc == OURS:
                    events.append((f'take|{prod}|{tick_no}',
                                   f'🏆 최저가 탈환 [{prod}] {mp} → {OURS} {lo_c[prod][1]:,}원'))
                else:
                    events.append((f'lost|{prod}|{tick_no}',
                                   f'🚨 최저가 뺏김 [{prod}] {OURS} → {mc} {lo_c[prod][1]:,}원'))
        return events
    return differ


def snapshot(cur):
    return {f'{mall}|{prod}': v for (mall, prod), v in cur.items()}


def record_history(con, cur, tick_label):
    for (mall, prod), v in sorted(cur.items()):
        con.execute('INSERT INTO price_history VALUES(?,?,?,?,?)',
                    (mall, prod, tick_label, v['price'] if v['stock'] else None, 1 if v['stock'] else 0))
    con.commit()


def write_price_report(cur, out):
    prods = sorted(set(p for _, p in cur))
    malls = sorted(set(m for m, _ in cur))
    lo = lowest_by_product(cur)
    rows = []
    for p in prods:
        row = [p]
        for m in malls:
            v = cur.get((m, p))
            row.append('품절' if v and not v['stock'] else (f"{v['price']:,}" if v else '-'))
        row.append(f'{lo[p][0]} {lo[p][1]:,}원' if p in lo else '전몰 품절')
        rows.append(row)
    return write_workbook(out, {'현재가 비교': (['상품'] + malls + ['최저(재고 있는 것만)'], rows)},
                          summary={'생성': dt.datetime.now().strftime('%Y-%m-%d %H:%M'),
                                   '규칙': f'변동 알림 임계 {PCT_TH}% · 최저가=재고 있는 것만 · 품절 이력=가격 NULL'})


# ── 데모 몰 서버 (진실 보유) ───────────────────────────────────────
class MallWorld:
    def __init__(self):
        self.state = {
            ('우리몰', '무선 이어폰'): [12900, True], ('경쟁B', '무선 이어폰'): [13500, True], ('경쟁C', '무선 이어폰'): [13900, True],
            ('우리몰', '텀블러 500ml'): [8900, True], ('경쟁B', '텀블러 500ml'): [8500, True], ('경쟁C', '텀블러 500ml'): [8800, True],
            ('우리몰', '유산균 30포'): [9900, True], ('경쟁B', '유산균 30포'): [10900, True], ('경쟁C', '유산균 30포'): [11500, True],
            ('우리몰', '노트북 파우치'): [15000, True], ('경쟁B', '노트북 파우치'): [15200, True], ('경쟁C', '노트북 파우치'): [15500, True],
        }

    def page(self, mall):
        items = sorted((p, v) for (m, p), v in self.state.items() if m == mall)
        if mall == '우리몰':
            rows = ''.join(f'<tr><td class="name">{p}</td><td class="price">{v[0]:,}원</td>'
                           f'<td class="stock">{"판매중" if v[1] else "품절"}</td></tr>' for p, v in items)
            return f'<table>{rows}</table>'
        if mall == '경쟁B':
            return ''.join(f'<div class="item"><h3>{p}</h3><p class="cost">₩{v[0]:,}</p>'
                           + ('' if v[1] else '<p class="badge">품절</p>') + '</div>' for p, v in items)
        return json.dumps({'products': [{'title': p, 'sale_price': v[0], 'soldout': not v[1]}
                                        for p, v in items]}, ensure_ascii=False)


def serve(world, port):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            mall = {'/mallA': '우리몰', '/mallB': '경쟁B', '/mallC': '경쟁C'}.get(self.path)
            if not mall:
                self.send_response(404); self.end_headers(); return
            body = world.page(mall).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8' if mall == '경쟁C'
                             else 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(body)
    srv = ThreadingHTTPServer(('127.0.0.1', port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ── 검증 데모 ───────────────────────────────────────────────────────
def main_demo():
    for p in (DB, STATE):
        if os.path.exists(p):
            os.remove(p)
    con = sqlite3.connect(DB)
    con.execute('CREATE TABLE price_history(mall TEXT, product TEXT, tick TEXT, price INTEGER, stock INT)')
    world = MallWorld()
    srv = serve(world, 8803)
    base = 'http://127.0.0.1:8803'
    time.sleep(0.2)
    w = Watcher(STATE)
    alerts = []

    def run_tick(no):
        cur = collect(base)
        record_history(con, cur, f't{no}')
        sent, held = w.tick(cur, make_differ(no), alerts.append, snapshot)
        w.save()
        return cur, sent, held

    # ① 파싱 전수 대조(서버 진실 12항목) + ② 첫 틱 알림 0
    cur1, s1, _ = run_tick(1)
    truth = {(m, p): dict(price=v[0], stock=v[1]) for (m, p), v in world.state.items()}
    ok1 = (cur1 == truth and len(cur1) == 12)
    ok2 = (s1 == 0 and alerts == [])

    # ③ 심은 변동: B 이어폰 -12%(최저가 뺏김 유발) · C 텀블러 +5% · 우리몰 유산균 품절(품절發 뺏김) ·
    #    ★노이즈 우리몰 파우치 -0.47%(임계 1% 미만 → 무시)
    world.state[('경쟁B', '무선 이어폰')][0] = 11880
    world.state[('경쟁C', '텀블러 500ml')][0] = 9240
    world.state[('우리몰', '유산균 30포')][1] = False
    world.state[('우리몰', '노트북 파우치')][0] = 14930
    cur2, s2, _ = run_tick(2)
    kinds2 = sorted(a.split()[0] for a in alerts)
    ok3 = (s2 == 5 and len(alerts) == 5
           and any('인하' in a and '무선 이어폰' in a and '-12.0%' in a for a in alerts)
           and any('인상' in a and '텀블러' in a and '+5.0%' in a for a in alerts)
           and any('품절' in a and '유산균' in a for a in alerts)
           and sum('뺏김' in a for a in alerts) == 2
           and not any('파우치' in a for a in alerts))

    # ④ 같은 상태 재수집 → 알림 0 (중복 0)
    _, s3, _ = run_tick(3)
    ok4 = (s3 == 0)

    # ⑤ 복구·인하·탈환×2: 유산균 재입고(→최저 탈환) + 우리몰 이어폰 11,500 인하(→탈환)
    world.state[('우리몰', '유산균 30포')][1] = True
    world.state[('우리몰', '무선 이어폰')][0] = 11500
    n_before = len(alerts)
    cur4, s4, _ = run_tick(4)
    new4 = alerts[n_before:]
    ok5 = (s4 == 4 and any('재입고' in a and '유산균' in a for a in new4)
           and any('인하' in a and '무선 이어폰' in a for a in new4)
           and sum('탈환' in a for a in new4) == 2)

    # ⑥ 이력 무결: 4틱×12=48행 · 품절 행 가격 NULL · 최저가 계산 품절 제외(t2 유산균 최저=경쟁B 10,900)
    n_hist = con.execute('SELECT COUNT(*) FROM price_history').fetchone()[0]
    null_ok = con.execute("SELECT price IS NULL FROM price_history WHERE tick='t2' AND mall='우리몰'"
                          " AND product='유산균 30포'").fetchone()[0]
    lo2 = lowest_by_product(cur2)
    ok6 = (n_hist == 48 and null_ok == 1 and lo2['유산균 30포'] == ('경쟁B', 10900))

    out = os.path.join(HERE, '가격비교_데모.xlsx')
    write_price_report(cur4, out)
    srv.shutdown()
    con.close()

    L = [f'# 경쟁가 추적 검증 리포트 ({dt.datetime.now():%Y-%m-%d %H:%M})',
         '- 데모 = 몰 3곳 서버(HTML 테이블·HTML 카드·JSON API — 구조 전부 다름, 서버가 진실) × 상품 4종 ·'
         ' 4틱 시나리오(변동·품절·노이즈·복구 심음) · 감시 엔진 = core Watcher(3번째 사용처)',
         '', '| 검증 | 결과 |', '|---|---|',
         f'| ① 파싱 전수 대조(3형식 12항목, 가격·재고) | {"PASS" if ok1 else "★FAIL"} |',
         f'| ② 첫 틱 알림 0(폭탄 방지 — Watcher 계약) | {"PASS" if ok2 else "★FAIL"} |',
         f'| ③ 심은 변동만 알림: 인하(-12%)·인상(+5%)·품절·뺏김×2 = 5건, ★0.47% 노이즈 0건 | {"PASS" if ok3 else "★FAIL"} |',
         f'| ④ 같은 상태 재수집 = 알림 0(중복 0) | {"PASS" if ok4 else "★FAIL"} |',
         f'| ⑤ 복구·인하·최저가 탈환×2 = 4건 | {"PASS" if ok5 else "★FAIL"} |',
         f'| ⑥ 이력 유실 0(48/48) · 품절=NULL(0원 오염 금지) · 최저가 품절 제외 | {"PASS" if ok6 else "★FAIL"} |',
         '', '- 규칙(명시): 변동 알림 임계 1%(설정) · 최저가 지위 = 재고 있는 것만 · 품절 이력 = 가격 NULL',
         '- ※ 데모 = 로컬 몰 서버. 실사이트 = 어댑터 1회 맞춤 — 대상 약관·robots 확인 후 진행 원칙.']
    rep = os.path.join(HERE, 'price_watch_verify.md')
    open(rep, 'w', encoding='utf-8').write('\n'.join(L))
    print('\n'.join(L))
    return ok1 and ok2 and ok3 and ok4 and ok5 and ok6


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    ok = main_demo()
    sys.exit(0 if ok else 1)
