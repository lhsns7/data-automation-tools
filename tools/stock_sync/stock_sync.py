#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""stock_sync.py — 오픈마켓 재고·가격 동기화 (기준표 → 멀티마켓 반영 + 반영 검증)

기준 재고·가격표(CSV) 하나로 여러 오픈마켓의 재고·가격을 맞춘다. order_hub(주문 수집)의 쓰기판.
쓰기 자동화의 본체 = 안전: "전송했다"가 아니라 **"반영됐다"를 재조회(read-back)로 증명**한다.

설계 (사고 방지 우선):
  - diff 전송: 현재 마켓 상태를 먼저 읽고 **기준표와 다른 품목만** 전송 (멱등 — 재실행 = 전송 0)
  - ★read-back 검증: 반영 후 다시 조회해 기준표와 전수 대조 — 전송 성공≠반영 성공을 구분
  - ★급변 가드: 가격이 현재가 대비 ±50% 넘게 바뀌는 품목은 전송하지 않고 보류(오타 12,000→1,200 방지)
  - 부분 실패 격리: 한 마켓이 거부해도 나머지는 완료, 실패 마켓·품목은 명시
  - 검수 모드(--dry): 무엇이 어떻게 바뀔지 diff만 출력, 전송 0

검증(--make-demo) = 서버 진실 대조: 마켓 3종 데모 API 서버(재고·가격 보유, 형식 상이)
  ①diff 전송 건수 = 기지 차이 수 ②read-back 전수 대조(서버 상태=기준표) ③멱등(재실행 전송 0)
  ④심은 오타 1건 → 급변 가드 보류 ⑤한 마켓 쓰기 거부 → 격리+read-back이 미반영 검출 ⑥--dry 전송 0.
※ 데모 = 합성 마켓 서버. 실마켓 = 판매자 API 자격으로 어댑터 1회 맞춤.
"""
import os, sys, csv, json, time, random, threading, datetime as dt
import urllib.request, urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
GUARD_PCT = 0.5      # 현재가 대비 ±50% 초과 변경 = 보류(설정값)


def _req(url, data=None):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data else None,
                                 headers={'Content-Type': 'application/json'} if data else {})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode('utf-8'))


# ── 어댑터 3종 (실마켓에선 이 함수들만 1회 맞춤) ───────────────────
def read_a(base):
    d = _req(f'{base}/martA/items')
    return {i['sku']: (int(i['stock']), int(i['price'])) for i in d['items']}


def write_a(base, sku, stock, price):
    _req(f'{base}/martA/items/update', {'sku': sku, 'stock': stock, 'price': price})


def read_b(base):
    d = _req(f'{base}/martB/goods')
    return {g['code']: (int(g['qty']), int(str(g['amount']).replace(',', ''))) for g in d['goods']}


def write_b(base, sku, stock, price):
    _req(f'{base}/martB/goods/set', {'code': sku, 'qty': stock, 'amount': f'{price:,}'})


def read_c(base):
    d = _req(f'{base}/martC/inventory')
    return {v['product_id']: (int(v['available']), int(v['unit_price'])) for v in d['inventory']}


def write_c(base, sku, stock, price):
    _req(f'{base}/martC/inventory/put', {'product_id': sku, 'available': stock, 'unit_price': price})


MARTS = {'A': (read_a, write_a), 'B': (read_b, write_b), 'C': (read_c, write_c)}


# ── 동기화 엔진 ─────────────────────────────────────────────────────
def load_master(path):
    """기준표 CSV(sku,재고,가격) → {sku: (재고, 가격)}"""
    out = {}
    for r in csv.DictReader(open(path, encoding='utf-8-sig')):
        out[r['sku'].strip()] = (int(r['재고']), int(str(r['가격']).replace(',', '')))
    return out


def sync(base, master, dry=False, guard=GUARD_PCT):
    """마켓별: 읽기 → diff → (가드 통과분만) 전송 → read-back 대조.
    반환 {마켓: {sent, held, failed, mismatch, err}} — mismatch = read-back에서 기준표와 다른 품목."""
    report = {}
    for mk, (read, write) in MARTS.items():
        r = dict(sent=[], held=[], failed=[], mismatch=[], err='')
        report[mk] = r
        try:
            cur = read(base)
        except Exception as e:
            r['err'] = f'읽기 실패 {type(e).__name__}'
            continue
        for sku, (st, pr) in master.items():
            if sku not in cur:
                r['held'].append((sku, '마켓에 없는 품목(신규 등록은 별도)'))
                continue
            cst, cpr = cur[sku]
            if (cst, cpr) == (st, pr):
                continue                                   # 이미 일치 = 전송 안 함(멱등)
            if cpr > 0 and abs(pr - cpr) / cpr > guard:
                r['held'].append((sku, f'급변 가드: {cpr:,}→{pr:,} ({(pr-cpr)/cpr:+.0%})'))
                continue
            if dry:
                r['sent'].append((sku, f'[dry] 재고 {cst}→{st} · 가격 {cpr:,}→{pr:,}'))
                continue
            try:
                write(base, sku, st, pr)
                r['sent'].append((sku, f'재고 {cst}→{st} · 가격 {cpr:,}→{pr:,}'))
            except Exception as e:
                r['failed'].append((sku, f'{type(e).__name__}'))
        if dry:
            continue
        try:                                               # ★read-back: 반영 확인(전송≠반영)
            after = read(base)
            held_skus = {s for s, _ in r['held']}
            for sku, (st, pr) in master.items():
                if sku in after and sku not in held_skus and after[sku] != (st, pr):
                    r['mismatch'].append((sku, f'기준 {st}/{pr:,} vs 마켓 {after[sku][0]}/{after[sku][1]:,}'))
        except Exception as e:
            r['err'] = f'재조회 실패 {type(e).__name__}'
    return report


# ── 데모 서버 (서버가 재고·가격 진실 보유) ─────────────────────────
class StockWorld:
    def __init__(self, seed=20260903):
        rng = random.Random(seed)
        self.state = {}                                    # {마켓: {sku: [stock, price]}}
        self.reject_write = set()                          # 쓰기 거부 마켓(부분 실패 검증)
        skus = [f'SKU-{i:03d}' for i in range(20)]
        base_state = {s: [rng.randrange(0, 90), rng.choice([8900, 12000, 25000, 49000])]
                      for s in skus}                       # ★3마켓 동일 출발(1차 검증 검거: 마켓별
        for mk in 'ABC':                                   #   독립 랜덤 → 의도 밖 대량 diff+가드 간섭)
            self.state[mk] = {s: list(v) for s, v in base_state.items()}

    def handler(self):
        world = self

        from http.server import BaseHTTPRequestHandler

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, body, code=200):
                b = json.dumps(body, ensure_ascii=False).encode()
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                p = self.path
                if p == '/martA/items':
                    self._json({'items': [{'sku': s, 'stock': v[0], 'price': v[1]}
                                          for s, v in sorted(world.state['A'].items())]})
                elif p == '/martB/goods':
                    self._json({'goods': [{'code': s, 'qty': v[0], 'amount': f'{v[1]:,}'}
                                          for s, v in sorted(world.state['B'].items())]})
                elif p == '/martC/inventory':
                    self._json({'inventory': [{'product_id': s, 'available': v[0], 'unit_price': v[1]}
                                              for s, v in sorted(world.state['C'].items())]})
                else:
                    self._json({'error': 'not found'}, 404)

            def do_POST(self):
                n = int(self.headers.get('Content-Length', 0))
                d = json.loads(self.rfile.read(n).decode())
                mk = {'martA': 'A', 'martB': 'B', 'martC': 'C'}[self.path.split('/')[1]]
                if mk in world.reject_write:
                    self._json({'error': 'forbidden'}, 500); return
                key = {'A': ('sku', 'stock', 'price'), 'B': ('code', 'qty', 'amount'),
                       'C': ('product_id', 'available', 'unit_price')}[mk]
                sku = d[key[0]]
                world.state[mk][sku] = [int(d[key[1]]), int(str(d[key[2]]).replace(',', ''))]
                self._json({'ok': True})
        return H


def serve(world, port):
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(('127.0.0.1', port), world.handler())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ── 검증 데모 ───────────────────────────────────────────────────────
def main_demo():
    world = StockWorld()
    srv = serve(world, 8797)
    base = 'http://127.0.0.1:8797'
    time.sleep(0.2)

    # 기준표: 서버 A 상태에서 출발 → 품목 7개를 의도 변경 + ★오타 1건(가드 검증용) 심음
    master = {s: tuple(v) for s, v in world.state['A'].items()}
    changed = ['SKU-001', 'SKU-004', 'SKU-007', 'SKU-010', 'SKU-013', 'SKU-016']
    for i, s in enumerate(changed):
        st, pr = master[s]
        master[s] = (st + 10 + i, pr + 1000)
    TYPO = 'SKU-019'
    master[TYPO] = (master[TYPO][0], 1200)                 # 12,000원대 → 1,200 오타(-90%)
    mpath = os.path.join(HERE, 'demo_master.csv')
    with open(mpath, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['sku', '재고', '가격'])
        w.writerows([s, v[0], v[1]] for s, v in sorted(master.items()))
    master = load_master(mpath)                            # 파일 규격 실증(저장→로드)

    # 마켓별 기지 diff 수 계산(서버 진실 기준, 가드 대상 제외)
    expect_diff = {mk: sum(1 for s, v in master.items()
                           if s != TYPO and tuple(world.state[mk][s]) != v) for mk in 'ABC'}

    # ⑥ 먼저 --dry: 전송 0 증명(서버 상태 불변)
    before = json.dumps(world.state, sort_keys=True)
    rep_dry = sync(base, master, dry=True)
    dry_ok = (json.dumps(world.state, sort_keys=True) == before
              and all(not r['failed'] and not r['mismatch'] for r in rep_dry.values()))

    # ①② 본 동기화: diff 전송 건수 = 기지 · read-back 전수 대조
    rep1 = sync(base, master)
    sent_ok = all(len(rep1[mk]['sent']) == expect_diff[mk] for mk in 'ABC')
    guard_ok = all(any(s == TYPO and '급변 가드' in why for s, why in rep1[mk]['held'])
                   for mk in 'ABC')                        # ④ 오타 = 전 마켓 보류
    rb_ok = all(not rep1[mk]['mismatch'] and not rep1[mk]['failed'] and not rep1[mk]['err']
                for mk in 'ABC')
    truth_ok = all(tuple(world.state[mk][s]) == v
                   for mk in 'ABC' for s, v in master.items() if s != TYPO)

    # ③ 멱등: 재실행 → 전송 0
    rep2 = sync(base, master)
    idem_ok = all(len(rep2[mk]['sent']) == 0 for mk in 'ABC')

    # ⑤ 부분 실패: B 쓰기 거부 상태에서 새 변경 1건 → B 실패 명시+read-back mismatch 검출, A·C 반영
    master2 = dict(master)
    master2['SKU-002'] = (77, world.state['A']['SKU-002'][1])
    world.reject_write.add('B')
    rep3 = sync(base, master2)
    world.reject_write.discard('B')
    fault_ok = (len(rep3['B']['failed']) == 1 and rep3['B']['mismatch']
                and len(rep3['A']['sent']) == 1 and len(rep3['C']['sent']) == 1
                and not rep3['A']['mismatch'] and not rep3['C']['mismatch'])
    srv.shutdown()

    n_items = len(master)
    L = [f'# 재고·가격 동기화 검증 리포트 ({dt.datetime.now():%Y-%m-%d %H:%M})',
         f'- 데모 = 마켓 3종 데모 서버(재고·가격 진실 보유, 응답/쓰기 형식 상이) × 품목 {n_items}종 ·'
         f' 의도 변경 {len(changed)}건 + ★가격 오타 1건(-90%) 심음',
         '', '| 검증 | 결과 |', '|---|---|',
         f'| ① diff 전송(변경분만) | 마켓별 {" · ".join(f"{mk} {len(rep1[mk]['sent'])}/{expect_diff[mk]}" for mk in "ABC")} → {"PASS" if sent_ok else "★FAIL"} |',
         f'| ② ★read-back 전수 대조(반영=기준표, 서버 진실 일치) | 불일치 0 → {"PASS" if rb_ok and truth_ok else "★FAIL"} |',
         f'| ③ 멱등(재실행 전송 0) | {sum(len(rep2[mk]["sent"]) for mk in "ABC")}건 → {"PASS" if idem_ok else "★FAIL"} |',
         f'| ④ ★급변 가드(심은 오타 12,000대→1,200) | 전 마켓 보류 → {"PASS" if guard_ok else "★FAIL"} |',
         f'| ⑤ 부분 실패 격리(B 쓰기 거부) | B 실패 명시+read-back 미반영 검출 · A/C 정상 → {"PASS" if fault_ok else "★FAIL"} |',
         f'| ⑥ 검수 모드(--dry) | 서버 상태 불변(전송 0) → {"PASS" if dry_ok else "★FAIL"} |',
         '', '- ※ "전송 성공"과 "반영 성공"을 구분 — 반영은 항상 재조회로 증명. 급변 가드 임계=설정값.',
         '- ※ 데모 = 합성 마켓 서버. 실마켓 = 판매자 API 자격으로 어댑터 1회 맞춤, 같은 read-back 검증 제공.']
    rep = os.path.join(HERE, 'stock_sync_verify.md')
    open(rep, 'w', encoding='utf-8').write('\n'.join(L))
    print('\n'.join(L))
    return dry_ok and sent_ok and guard_ok and rb_ok and truth_ok and idem_ok and fault_ok


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    ok = main_demo()
    sys.exit(0 if ok else 1)
