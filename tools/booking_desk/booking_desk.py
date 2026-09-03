#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""booking_desk.py — 예약·접수 데스크 (폼 → 검증 → 저장 → 알림 → 대장)

신청·예약·문의 접수를 받는 작은 백엔드.
예약 시스템의 본체 = **넘치게 받지 않는 것**: 동시에 몰려도 슬롯 정원을 절대 초과하지 않는다.

기능:
  - 접수 폼(단일 페이지, 잔여 표시) + 접수 API — 이름·연락처·슬롯·항목 검증(불량은 저장 안 하고 사유 반환)
  - ★슬롯 정원 관리: SQLite 원자 트랜잭션(BEGIN IMMEDIATE)으로 동시 접수에도 정원 초과 0
  - ★중복 접수 방지: (연락처, 슬롯) 유니크 — 같은 사람 같은 슬롯 재제출 = "이미 접수됨"
  - 접수 즉시 관리자 알림(훅 교체식 — 데모는 파일 sink, 실서비스는 이메일/슬랙/텔레그램)
  - 접수 대장 엑셀 내보내기

검증(--make-demo):
  ①정상 접수 전수 대조(제출 의도 vs DB) ②심은 불량 4종 전부 거부(저장 0·사유 반환)
  ③★동시 오버부킹: 정원 2 슬롯에 5건 동시 제출 → 정확히 2건만 수락 ④중복 재제출 → DB 불변
  ⑤알림 유실 0(수락 수=알림 수) ⑥대장 내보내기 = DB 전수 일치.
※ 데모 = 로컬 서버. 실서비스 = 슬롯·항목 설정과 알림 채널만 교체.
"""
import os, sys, re, json, time, sqlite3, threading, datetime as dt
import urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', '..', 'core'))
from xlsx import write_workbook

DB = os.path.join(HERE, 'bookings.db')
NOTIFY_LOG = os.path.join(HERE, 'notify.log')
SLOTS = [('09-10 오전 10:00', 3), ('09-10 오후 14:00', 2), ('09-10 오후 16:00', 3),
         ('09-11 오전 10:00', 3), ('09-11 오후 14:00', 3)]        # (슬롯, 정원) — 설정값
ITEMS = ['방문 상담', '전화 상담', '장비 점검']


def open_db(path=DB):
    con = sqlite3.connect(path, timeout=10)
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('''CREATE TABLE IF NOT EXISTS slots(slot TEXT PRIMARY KEY, capacity INT NOT NULL)''')
    con.execute('''CREATE TABLE IF NOT EXISTS bookings(
        id INTEGER PRIMARY KEY AUTOINCREMENT, slot TEXT NOT NULL, name TEXT NOT NULL,
        phone TEXT NOT NULL, item TEXT NOT NULL, memo TEXT, created TEXT NOT NULL,
        UNIQUE(phone, slot))''')
    return con


def seed_slots(con, slots=SLOTS):
    con.executemany('INSERT OR IGNORE INTO slots VALUES(?,?)', slots)
    con.commit()


def validate(d):
    """접수 검증 — 불량 사유 문자열(없으면 '')"""
    if not (d.get('name') or '').strip():
        return '이름은 필수입니다'
    phone = re.sub(r'[^0-9]', '', d.get('phone') or '')
    if not re.fullmatch(r'01[016789]\d{7,8}', phone):
        return '연락처 형식이 올바르지 않습니다 (휴대폰 번호)'
    if (d.get('item') or '') not in ITEMS:
        return '항목을 선택해 주세요'
    return ''


def book(con, d, notify):
    """접수 시도 → (상태, 메시지). 상태 = ok/full/dup/bad.
    ★BEGIN IMMEDIATE = 쓰기 잠금 선점 → 정원 검사~삽입이 원자적(동시 접수 정원 초과 0)."""
    bad = validate(d)
    if bad:
        return 'bad', bad
    phone = re.sub(r'[^0-9]', '', d['phone'])
    slot = (d.get('slot') or '').strip()
    try:
        con.execute('BEGIN IMMEDIATE')
        row = con.execute('SELECT capacity FROM slots WHERE slot=?', (slot,)).fetchone()
        if not row:
            con.execute('ROLLBACK')
            return 'bad', '존재하지 않는 시간대입니다'
        used = con.execute('SELECT COUNT(*) FROM bookings WHERE slot=?', (slot,)).fetchone()[0]
        if used >= row[0]:
            con.execute('ROLLBACK')
            return 'full', '해당 시간대는 마감되었습니다'
        con.execute('INSERT INTO bookings(slot,name,phone,item,memo,created) VALUES(?,?,?,?,?,?)',
                    (slot, d['name'].strip(), phone, d['item'], (d.get('memo') or '').strip(),
                     dt.datetime.now().isoformat(' ', 'seconds')))
        con.execute('COMMIT')
    except sqlite3.IntegrityError:
        con.execute('ROLLBACK')
        return 'dup', '이미 같은 시간대에 접수되어 있습니다'
    notify(f"[접수] {slot} · {d['name'].strip()} · {d['item']}")
    return 'ok', '접수가 완료되었습니다'


def file_notify(msg):
    """데모 알림 sink(파일). 실서비스 = 이메일/슬랙/텔레그램 훅으로 교체."""
    with open(NOTIFY_LOG, 'a', encoding='utf-8') as f:
        f.write(msg + '\n')


def export_xlsx(con, out):
    rows = [list(r) for r in con.execute(
        'SELECT id, slot, name, phone, item, memo, created FROM bookings ORDER BY id')]
    remain = [list(r) for r in con.execute(
        '''SELECT s.slot, s.capacity, COUNT(b.id), s.capacity-COUNT(b.id)
           FROM slots s LEFT JOIN bookings b ON b.slot=s.slot GROUP BY s.slot ORDER BY s.slot''')]
    return write_workbook(out, {
        '접수 대장': (['번호', '시간대', '이름', '연락처', '항목', '메모', '접수 시각'], rows),
        '슬롯 현황': (['시간대', '정원', '접수', '잔여'], remain),
    }, summary={'생성': dt.datetime.now().strftime('%Y-%m-%d %H:%M'),
                '접수 건수': f'{len(rows)}건',
                '규칙': '슬롯 정원 원자 검사(초과 0) · (연락처,슬롯) 중복 방지 · 불량 저장 0'}), rows


# ── 웹 폼 + API ─────────────────────────────────────────────────────
FORM_HTML = """<!doctype html><html lang=ko><head><meta charset=utf-8><title>접수 데스크 (데모)</title>
<style>body{font-family:'Malgun Gothic',sans-serif;background:#f2f4f8;margin:0}
.card{max-width:460px;margin:30px auto;background:#fff;border-radius:14px;padding:26px;border:1px solid #dfe4ea}
h1{font-size:20px;margin:0 0 4px} .sub{color:#5b6472;font-size:13px;margin-bottom:16px}
label{display:block;font-size:13px;font-weight:700;margin:12px 0 5px}
input,select,textarea{width:100%;box-sizing:border-box;padding:10px;border:1px solid #cfd6de;border-radius:9px;font-size:14px}
button{margin-top:16px;width:100%;padding:12px;border:0;border-radius:10px;background:#86198f;color:#fff;font-weight:800;cursor:pointer}
.msg{margin-top:12px;font-size:13.5px;font-weight:700}.ok{color:#15803d}.no{color:#b4232c}
.slot small{color:#8b95a3}</style></head><body><div class=card>
<h1>상담 예약 접수</h1><div class=sub>시간대별 정원이 있어 마감되면 접수되지 않습니다.</div>
<label>이름</label><input id=name><label>연락처</label><input id=phone placeholder="010-0000-0000">
<label>시간대 (잔여)</label><select id=slot class=slot></select>
<label>항목</label><select id=item></select>
<label>메모 (선택)</label><textarea id=memo rows=2></textarea>
<button onclick=go()>접수하기</button><div id=m class=msg></div></div>
<script>
async function load(){
  const d=await (await fetch('/slots')).json();
  slot.innerHTML=d.slots.map(s=>`<option ${s.remain<1?'disabled':''}>${s.slot} (잔여 ${s.remain})</option>`).join('');
  item.innerHTML=d.items.map(i=>`<option>${i}</option>`).join('');
}
async function go(){
  const body={name:name.value,phone:phone.value,slot:slot.value.replace(/ \\(잔여.*\\)$/,''),item:item.value,memo:memo.value};
  const r=await (await fetch('/submit',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
  m.className='msg '+(r.status==='ok'?'ok':'no'); m.textContent=r.message; load();
}
load();
</script></body></html>"""


def serve(port, db_path=DB, notify=file_notify):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, body, code=200):
            b = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            if self.path == '/slots':
                con = open_db(db_path)
                rows = con.execute('''SELECT s.slot, s.capacity-COUNT(b.id) FROM slots s
                    LEFT JOIN bookings b ON b.slot=s.slot GROUP BY s.slot ORDER BY s.slot''').fetchall()
                con.close()
                self._json({'slots': [{'slot': s, 'remain': r} for s, r in rows], 'items': ITEMS})
            else:
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.end_headers()
                self.wfile.write(FORM_HTML.encode())

        def do_POST(self):
            if self.path != '/submit':
                self._json({'status': 'bad', 'message': 'not found'}, 404); return
            n = int(self.headers.get('Content-Length', 0))
            d = json.loads(self.rfile.read(n).decode())
            con = open_db(db_path)               # 요청별 연결(스레드 안전) — 원자성은 BEGIN IMMEDIATE
            try:
                status, msg = book(con, d, notify)
            finally:
                con.close()
            self._json({'status': status, 'message': msg})
    srv = ThreadingHTTPServer(('127.0.0.1', port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ── 검증 데모 ───────────────────────────────────────────────────────
def _post(base, d):
    req = urllib.request.Request(f'{base}/submit', data=json.dumps(d).encode(),
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def main_demo():
    for p in (DB, DB + '-wal', DB + '-shm', NOTIFY_LOG):
        if os.path.exists(p):
            os.remove(p)
    con = open_db()
    seed_slots(con)
    con.close()
    srv = serve(8799)
    base = 'http://127.0.0.1:8799'
    time.sleep(0.2)

    # ① 정상 접수 8건(여러 슬롯·정원 내) → 전수 대조
    spread = ['09-10 오전 10:00', '09-10 오후 16:00', '09-11 오전 10:00', '09-11 오후 14:00']
    normal = [dict(name=f'고객{i}', phone=f'010-1234-00{i:02d}', slot=spread[i % 4],
                   item=ITEMS[i % 3], memo=f'메모{i}') for i in range(8)]
    res1 = [_post(base, d) for d in normal]
    ok1 = all(r['status'] == 'ok' for r in res1)
    con = open_db()
    db_rows = {(r[0], r[1], r[2]) for r in con.execute('SELECT slot, name, item FROM bookings')}
    intent = {(d['slot'], d['name'], d['item']) for d in normal}
    full_ok = ok1 and db_rows == intent and len(db_rows) == 8

    # ② 심은 불량 4종 → 전부 거부·저장 0
    bads = [dict(name='', phone='01012340099', slot=SLOTS[0][0], item=ITEMS[0]),
            dict(name='홍길동', phone='02-123-4567', slot=SLOTS[0][0], item=ITEMS[0]),
            dict(name='홍길동', phone='01012340098', slot='없는 슬롯', item=ITEMS[0]),
            dict(name='홍길동', phone='01012340097', slot=SLOTS[0][0], item='없는 항목')]
    res2 = [_post(base, d) for d in bads]
    n_after = con.execute('SELECT COUNT(*) FROM bookings').fetchone()[0]
    bad_ok = all(r['status'] == 'bad' for r in res2) and n_after == 8

    # ③ ★동시 오버부킹: 정원 2 슬롯('09-10 오후 14:00', 아직 0건)에 5건 동시 제출
    target = '09-10 오후 14:00'
    results, thr = [], []
    def rush(i):
        results.append(_post(base, dict(name=f'동시{i}', phone=f'010-9999-11{i:02d}',
                                        slot=target, item=ITEMS[0])))
    for i in range(5):
        thr.append(threading.Thread(target=rush, args=(i,)))
    for t in thr: t.start()
    for t in thr: t.join()
    got_ok = sum(1 for r in results if r['status'] == 'ok')
    got_full = sum(1 for r in results if r['status'] == 'full')
    n_slot = con.execute('SELECT COUNT(*) FROM bookings WHERE slot=?', (target,)).fetchone()[0]
    race_ok = (got_ok == 2 and got_full == 3 and n_slot == 2)

    # ④ 중복 재제출(①의 1건 그대로) → dup + DB 불변
    r4 = _post(base, normal[0])
    n4 = con.execute('SELECT COUNT(*) FROM bookings').fetchone()[0]
    dup_ok = (r4['status'] == 'dup' and n4 == 10)

    # ⑤ 알림 유실 0: 수락 10건 = 알림 10줄
    n_notify = sum(1 for _ in open(NOTIFY_LOG, encoding='utf-8'))
    notify_ok = (n_notify == 10)

    # ⑥ 대장 내보내기 = DB 전수(2회 동일)
    out = os.path.join(HERE, '접수대장_데모.xlsx')
    _, rows_a = export_xlsx(con, out)
    _, rows_b = export_xlsx(con, out)
    export_ok = (len(rows_a) == 10 and rows_a == rows_b)
    con.close()
    srv.shutdown()

    L = [f'# 예약·접수 데스크 검증 리포트 ({dt.datetime.now():%Y-%m-%d %H:%M})',
         '- 데모 = 로컬 접수 서버(폼+API) · 슬롯 5종(정원 2~3) · 정상 8건 + ★불량 4종 + ★동시 돌진 5건 + 중복 1건',
         '', '| 검증 | 결과 |', '|---|---|',
         f'| ① 정상 접수 전수 대조(제출 의도 vs DB) | 8/8 일치 → {"PASS" if full_ok else "★FAIL"} |',
         f'| ② 심은 불량 4종(이름 누락·유선번호·없는 슬롯·없는 항목) | 전부 거부·저장 0 → {"PASS" if bad_ok else "★FAIL"} |',
         f'| ③ ★동시 오버부킹(정원 2에 5건 동시) | 수락 {got_ok} · 마감 안내 {got_full} · DB {n_slot}건 → {"PASS" if race_ok else "★FAIL"} |',
         f'| ④ 중복 재제출((연락처,슬롯) 유니크) | "이미 접수" 안내 · DB 불변 → {"PASS" if dup_ok else "★FAIL"} |',
         f'| ⑤ 알림 유실 0(수락 10 = 알림 {n_notify}) | {"PASS" if notify_ok else "★FAIL"} |',
         f'| ⑥ 접수 대장 내보내기(DB 전수·재현) | 10건 · 2회 동일 → {"PASS" if export_ok else "★FAIL"} |',
         '', '- ※ 원자성 = SQLite BEGIN IMMEDIATE(정원 검사~삽입이 한 잠금 안) — "동시에 눌러도 넘치지 않음"이 이 도구의 존재 이유.',
         '- ※ 데모 = 로컬 서버·파일 알림. 실서비스 = 슬롯·항목 설정과 알림 채널(이메일/슬랙/텔레그램)만 교체.']
    rep = os.path.join(HERE, 'booking_verify.md')
    open(rep, 'w', encoding='utf-8').write('\n'.join(L))
    print('\n'.join(L))
    return full_ok and bad_ok and race_ok and dup_ok and notify_ok and export_ok


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    if '--serve' in sys.argv:
        con = open_db(); seed_slots(con); con.close()
        print('접수 데스크: http://127.0.0.1:8799 (Ctrl+C 종료)')
        serve(8799)
        while True:
            time.sleep(1)
    else:
        ok = main_demo()
        sys.exit(0 if ok else 1)
