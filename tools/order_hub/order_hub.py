#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""order_hub.py — 오픈마켓 주문 수집·통합 허브

여러 오픈마켓의 주문 API를 **표준 스키마 하나로 통합** 수집한다.
통합의 진짜 문제 = 마켓마다 다른 것들: 필드명 · 날짜 형식(ISO/유닉스/한국식) · 금액 표기(콤마 문자열)
· 페이징 방식(page/offset/cursor) · 상태 어휘. 어댑터가 전부 흡수해 한 장의 주문 대장으로 만든다.

기능:
  - 마켓별 어댑터(1회 맞춤) → 표준 스키마(마켓·주문ID·일시·상품·수량·금액·상태·구매자) 정규화
  - 증분 수집: (마켓, 주문ID) 기준 신규만 추가(중복 0) + 기존 주문 **상태 변화 추적**(배송중→완료 등)
  - 장애 격리: 한 마켓 API가 죽어도 나머지는 수집 완료, 실패 마켓은 묵살하지 않고 명시
  - 산출: 통합 주문 엑셀(주문 대장 + 상태 변화 + 수집 리포트)

검증(--make-demo) = **서버 진실 대조**: 형식이 제각각인 가짜 마켓 3종 API 서버를 띄우고(서버가 진실 보유)
  ①전수 필드 대조 ②정규화 수기 대조 ③페이징 소진 ④증분(신규·상태변화 기지 개수 정확, 중복 0)
  ⑤장애 마켓 격리 ⑥재현성. ※실마켓 연동 = 판매자 API 자격으로 어댑터 1회 맞춤(데모는 합성 서버).
"""
import os, sys, json, time, random, threading, datetime as dt
import urllib.request, urllib.error, urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', '..', 'core'))
from xlsx import write_workbook

STATE = os.path.join(HERE, 'orders_state.json')
STD_STATUS = {  # 마켓별 상태 어휘 → 표준(결제완료/배송중/완료/취소)
    'A': {'PAYED': '결제완료', 'SHIPPING': '배송중', 'DONE': '완료', 'CANCEL': '취소'},
    'B': {'결제완료': '결제완료', '배송중': '배송중', '구매확정': '완료', '취소': '취소'},
    'C': {'paid': '결제완료', 'shipping': '배송중', 'delivered': '완료', 'canceled': '취소'},
}


def to_iso(v):
    """일시 정규화: ISO 문자열 / 유닉스 초 / '2026/09/01 14:22' → 'YYYY-MM-DD HH:MM'."""
    if isinstance(v, (int, float)):
        return dt.datetime.fromtimestamp(v).strftime('%Y-%m-%d %H:%M')
    s = str(v).strip().replace('T', ' ')
    if '/' in s:
        s = s.replace('/', '-')
    return s[:16]


def to_amount(v):
    return int(str(v).replace(',', '').replace('원', ''))


# ── 수집기(어댑터 3종 — 실마켓에선 이 함수들만 1회 맞춤) ───────────
def _get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read().decode('utf-8'))


def fetch_a(base):
    """마켓A: ?page=1.. / {orders:[{orderId, orderedAt(ISO), productName, qty, totalPrice, status, buyer}], totalPages}"""
    out, page = [], 1
    while True:
        d = _get(f'{base}/martA/orders?page={page}')
        for o in d['orders']:
            out.append(dict(market='A', order_id=str(o['orderId']), ordered_at=to_iso(o['orderedAt']),
                            product=o['productName'], qty=int(o['qty']), amount=to_amount(o['totalPrice']),
                            status=STD_STATUS['A'][o['status']], buyer=o['buyer']))
        if page >= d['totalPages']:
            return out
        page += 1


def fetch_b(base):
    """마켓B: ?offset=&limit=7 / {list:[{no, date(한국식), item, count, amount('12,000'), state, customer}], total}"""
    out, off = [], 0
    while True:
        d = _get(f'{base}/martB/orders?offset={off}&limit=7')
        for o in d['list']:
            out.append(dict(market='B', order_id=str(o['no']), ordered_at=to_iso(o['date']),
                            product=o['item'], qty=int(o['count']), amount=to_amount(o['amount']),
                            status=STD_STATUS['B'][o['state']], buyer=o['customer']))
        off += len(d['list'])
        if off >= d['total'] or not d['list']:
            return out


def fetch_c(base):
    """마켓C: ?cursor= / {items:[{id, ts(unix), goods, quantity, pay_amount, order_status, buyer_name}], next_cursor}"""
    out, cur = [], ''
    while True:
        d = _get(f'{base}/martC/orders?cursor={cur}')
        for o in d['items']:
            out.append(dict(market='C', order_id=str(o['id']), ordered_at=to_iso(o['ts']),
                            product=o['goods'], qty=int(o['quantity']), amount=to_amount(o['pay_amount']),
                            status=STD_STATUS['C'][o['order_status']], buyer=o['buyer_name']))
        cur = d.get('next_cursor')
        if not cur:
            return out


ADAPTERS = {'A': fetch_a, 'B': fetch_b, 'C': fetch_c}


def collect(base, state):
    """전 마켓 수집 → (신규, 상태변화, 실패마켓, 전체수집수). state = {'A:101': 주문dict}."""
    new, changed, failed, got = [], [], [], 0
    for mk, fn in ADAPTERS.items():
        try:
            orders = fn(base)
        except Exception as e:
            failed.append((mk, f'{type(e).__name__}: {e}'))
            continue
        got += len(orders)
        for o in orders:
            key = f"{o['market']}:{o['order_id']}"
            old = state.get(key)
            if old is None:
                state[key] = o
                new.append(o)
            elif old['status'] != o['status']:
                changed.append((key, old['status'], o['status']))
                state[key] = o
    return new, changed, failed, got


def write_out(out, state, changed, failed):
    rows = [[o['market'], o['order_id'], o['ordered_at'], o['product'], o['qty'], o['amount'], o['status'], o['buyer']]
            for o in sorted(state.values(), key=lambda x: (x['market'], x['ordered_at'], x['order_id']))]
    sheets = {'통합 주문 대장': (['마켓', '주문ID', '주문일시', '상품', '수량', '금액', '상태', '구매자'], rows)}
    if changed:
        sheets['상태 변화'] = (['주문', '이전', '현재'], [list(c) for c in changed])
    if failed:
        sheets['수집 실패(확인 필요)'] = (['마켓', '사유'], [list(f) for f in failed])
    per = {}
    for o in state.values():
        per[o['market']] = per.get(o['market'], 0) + 1
    return write_workbook(out, sheets, summary={
        '생성': dt.datetime.now().strftime('%Y-%m-%d %H:%M'),
        '보유 주문': f"{len(state)}건 ({' · '.join(f'{m} {n}' for m, n in sorted(per.items()))})",
        '상태 변화 / 실패 마켓': f'{len(changed)}건 / {len(failed)}곳',
        '규칙': '(마켓,주문ID) 기준 증분 · 상태 어휘/일시/금액 표준화 · 실패 마켓 묵살 금지',
        '주의': '데모=합성 마켓 서버. 실마켓은 판매자 API 자격으로 어댑터 1회 맞춤'})


# ── 데모 서버: 형식 제각각 마켓 3종 (서버가 진실 보유) ─────────────
class MartWorld:
    def __init__(self, seed=20260903):
        self.rng = random.Random(seed)
        self.truth = {}                       # {'A:101': {…표준 스키마…}} = 서버측 진실
        self.fail_b = False
        상품 = ['무선 이어폰', '텀블러 500ml', '노트북 파우치', '유산균 30포', '캠핑 랜턴', '강아지 간식']
        구매자 = ['김서준', '이도윤', '박하은', '최지우', '정시우', '한아린']
        base = dt.datetime(2026, 8, 25, 9, 0)
        for mk, n, oid0 in (('A', 23, 100), ('B', 17, 5000), ('C', 31, 90000)):
            for i in range(n):
                t = base + dt.timedelta(minutes=self.rng.randrange(0, 60 * 24 * 8))
                self.truth[f'{mk}:{oid0+i}'] = dict(
                    market=mk, order_id=str(oid0 + i), ordered_at=t.strftime('%Y-%m-%d %H:%M'),
                    product=self.rng.choice(상품), qty=self.rng.randrange(1, 4),
                    amount=self.rng.choice([12000, 8900, 35000, 129000, 4500]),
                    status=self.rng.choice(['결제완료', '배송중', '완료']), buyer=self.rng.choice(구매자))

    def advance(self):
        """2차분: 신규 6건(A2·B3·C1) 추가 + 기존 5건 상태 전진 → (신규키, 변화키) 반환."""
        newk = []
        for mk, cnt in (('A', 2), ('B', 3), ('C', 1)):
            ids = [int(k.split(':')[1]) for k in self.truth if k.startswith(mk + ':')]
            for j in range(cnt):
                oid = max(ids) + 1 + j
                k = f'{mk}:{oid}'
                self.truth[k] = dict(market=mk, order_id=str(oid), ordered_at='2026-09-03 08:3' + str(j),
                                     product='신규주문상품', qty=1, amount=19900, status='결제완료', buyer='신규구매자')
                newk.append(k)
        nxt = {'결제완료': '배송중', '배송중': '완료'}
        chg = []
        for k in sorted(self.truth):                    # 전이 가능한 기존 주문에서 정확히 5건
            if len(chg) >= 5:
                break
            if k not in newk and self.truth[k]['status'] in nxt:
                self.truth[k]['status'] = nxt[self.truth[k]['status']]
                chg.append(k)
        return newk, chg

    # 마켓별 응답 변환(형식 제각각 재현)
    def payload(self, mk, o):
        r = {'결제완료': 'PAYED', '배송중': 'SHIPPING', '완료': 'DONE', '취소': 'CANCEL'}
        if mk == 'A':
            return {'orderId': int(o['order_id']), 'orderedAt': o['ordered_at'].replace(' ', 'T') + ':00',
                    'productName': o['product'], 'qty': o['qty'], 'totalPrice': o['amount'],
                    'status': r[o['status']], 'buyer': o['buyer']}
        if mk == 'B':
            inv = {'결제완료': '결제완료', '배송중': '배송중', '완료': '구매확정', '취소': '취소'}
            return {'no': int(o['order_id']), 'date': o['ordered_at'].replace('-', '/'),
                    'item': o['product'], 'count': o['qty'], 'amount': f"{o['amount']:,}",
                    'state': inv[o['status']], 'customer': o['buyer']}
        inv = {'결제완료': 'paid', '배송중': 'shipping', '완료': 'delivered', '취소': 'canceled'}
        ts = int(dt.datetime.strptime(o['ordered_at'], '%Y-%m-%d %H:%M').timestamp())
        return {'id': int(o['order_id']), 'ts': ts, 'goods': o['product'], 'quantity': o['qty'],
                'pay_amount': o['amount'], 'order_status': inv[o['status']], 'buyer_name': o['buyer']}

    def mart(self, mk):
        return sorted((o for k, o in self.truth.items() if k.startswith(mk + ':')),
                      key=lambda o: int(o['order_id']))


def serve(world, port):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            if u.path == '/martB/orders' and world.fail_b:
                self.send_response(500); self.end_headers(); return
            if u.path == '/martA/orders':
                page = int(q.get('page', ['1'])[0])
                rows = world.mart('A')
                chunk = rows[(page - 1) * 10: page * 10]
                body = {'orders': [world.payload('A', o) for o in chunk],
                        'totalPages': max(1, -(-len(rows) // 10))}
            elif u.path == '/martB/orders':
                off, lim = int(q.get('offset', ['0'])[0]), int(q.get('limit', ['7'])[0])
                rows = world.mart('B')
                body = {'list': [world.payload('B', o) for o in rows[off:off + lim]], 'total': len(rows)}
            elif u.path == '/martC/orders':
                cur = int(q.get('cursor', [''])[0] or 0)
                rows = world.mart('C')
                chunk = rows[cur:cur + 8]
                body = {'items': [world.payload('C', o) for o in chunk],
                        'next_cursor': str(cur + 8) if cur + 8 < len(rows) else None}
            else:
                self.send_response(404); self.end_headers(); return
            b = json.dumps(body, ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(b)
    srv = ThreadingHTTPServer(('127.0.0.1', port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ── 검증 데모 ───────────────────────────────────────────────────────
def main_demo():
    world = MartWorld()
    srv = serve(world, 8791)
    base = 'http://127.0.0.1:8791'
    time.sleep(0.2)

    state = {}
    new1, chg1, fail1, got1 = collect(base, state)                 # 1차: 최초 수집

    # ① 전수 필드 대조(수집 결과 vs 서버 진실)
    mism = sum(1 for k, o in world.truth.items() if state.get(k) != o)
    full_ok = (mism == 0 and len(state) == len(world.truth) == 71 and not fail1)
    n1 = len(world.truth)

    # ② 정규화 수기 대조: B '12,000'식 콤마, C 유닉스, A ISO → 표준 (각 1건)
    bk = next(k for k in world.truth if k.startswith('B:'))
    ck = next(k for k in world.truth if k.startswith('C:'))
    ak = next(k for k in world.truth if k.startswith('A:'))
    norm_ok = all(isinstance(state[k]['amount'], int) for k in (ak, bk, ck)) and \
        all(len(state[k]['ordered_at']) == 16 and state[k]['ordered_at'][4] == '-' for k in (ak, bk, ck))

    # ③ 페이징 소진: 마켓별 수집수 = 진실수 (A 23=3페이지 · B 17=offset · C 31=cursor)
    per = {mk: sum(1 for k in state if k.startswith(mk + ':')) for mk in 'ABC'}
    page_ok = (per == {'A': 23, 'B': 17, 'C': 31})

    # ④ 증분: 서버 전진(신규 6·상태변화 5 기지) → 2차 수집이 정확히 그것만
    newk, chgk = world.advance()
    new2, chg2, fail2, _ = collect(base, state)
    inc_ok = (sorted(f"{o['market']}:{o['order_id']}" for o in new2) == sorted(newk)
              and sorted(k for k, _, _ in chg2) == sorted(chgk)
              and len(state) == len(world.truth) and not fail2)

    # ⑤ 장애 격리: B 다운 → A·C는 수집, B는 실패 명시 + 기존 B 데이터 보존
    world.fail_b = True
    b_before = per['B'] + 3
    new3, chg3, fail3, _ = collect(base, state)
    fault_ok = (len(fail3) == 1 and fail3[0][0] == 'B'
                and sum(1 for k in state if k.startswith('B:')) == b_before)
    world.fail_b = False

    # ⑥ 재현성: 변화 없는 재수집 → 신규 0 · 변화 0
    new4, chg4, fail4, _ = collect(base, state)
    rep_ok = (not new4 and not chg4 and not fail4)

    out = os.path.join(HERE, '통합주문_데모.xlsx')
    info = write_out(out, state, chg2, fail3)
    json.dump(state, open(STATE, 'w', encoding='utf-8'), ensure_ascii=False)
    srv.shutdown()

    L = [f'# 오픈마켓 주문 통합 검증 리포트 ({dt.datetime.now():%Y-%m-%d %H:%M})',
         '- 데모 = 형식 제각각 마켓 3종 API 서버(필드명·날짜(ISO/유닉스/한국식)·금액(콤마)·페이징(page/offset/cursor)·상태 어휘 전부 상이,'
         ' **서버가 진실 보유**) → 수집·통합 결과를 서버 진실과 대조',
         '', '| 검증 | 결과 |', '|---|---|',
         f'| ① 전수 필드 대조({n1}건 × 8필드) | 불일치 {mism}건 → {"PASS" if full_ok else "★FAIL"} |',
         f'| ② 정규화 수기 대조(ISO·유닉스·한국식 날짜 / 콤마 금액) | {"PASS" if norm_ok else "★FAIL"} |',
         f'| ③ 페이징 소진(page 3장·offset·cursor) | A {per["A"]}/23 · B {per["B"]}/17 · C {per["C"]}/31 → {"PASS" if page_ok else "★FAIL"} |',
         f'| ④ 증분 수집(심은 신규 {len(newk)}·상태변화 {len(chgk)} — 기지 목록 대조) | 신규 {len(new2)} · 변화 {len(chg2)} · 중복 0 → {"PASS" if inc_ok else "★FAIL"} |',
         f'| ⑤ 장애 마켓 격리(B 다운) | 실패 명시 {len(fail3)}곳 · A/C 정상 · B 데이터 보존 → {"PASS" if fault_ok else "★FAIL"} |',
         f'| ⑥ 재현성(변화 없는 재수집) | 신규 {len(new4)} · 변화 {len(chg4)} → {"PASS" if rep_ok else "★FAIL"} |',
         f'| 산출 | {os.path.basename(out)} ({info["sheets"]}시트) |',
         '', '- ※ 데모 = 합성 마켓 서버(로컬). 실마켓(스마트스토어·쿠팡 등)은 판매자 API 자격 발급 후 어댑터 1회 맞춤 —'
         ' 같은 서버 대조 검증을 실데이터로 제공.']
    rep = os.path.join(HERE, 'order_hub_verify.md')
    open(rep, 'w', encoding='utf-8').write('\n'.join(L))
    print('\n'.join(L))
    return full_ok and norm_ok and page_ok and inc_ok and fault_ok and rep_ok


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    ok = main_demo()
    sys.exit(0 if ok else 1)
