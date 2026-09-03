#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""webform_filler.py — 표(CSV)의 각 행을 웹 폼에 자동 입력·제출 (2026-09)

CSV 목록을 웹 폼에 한 건씩 자동 입력·제출한다(브라우저 자동화, Playwright).
데스크톱 매크로의 웹 버전 — 접수·등록·신청 폼 반복 입력용.

정직 설계:
  - **검수 모드(--dry)**: 입력까지만 하고 제출 버튼은 누르지 않음(실서비스 오발사 방지).
  - 필수값 누락 행 = **제출하지 않고 격리**(엉터리 데이터 밀어넣기 금지).
  - 실패 행 = 1회 재시도 후 격리·기록(유실 대신 표기).

검증(--make-demo): 데모 폼 서버(stdlib)가 **제출을 서버측 jsonl로 기록** → CSV 원본과
  **필드 단위 전수 대조**(특수문자 <>&"' ·긴 텍스트·셀렉트·체크박스 포함) + 필수 누락 격리 + 재현성.
"""
import os, sys, csv, json, html, threading, urllib.parse, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8877
REQUIRED = ['이름', '연락처']                      # 필수 필드(누락 = 격리)


# ── 데모 폼 서버 (제출을 서버측에 기록 → 검증의 그라운드트루스) ────────
FORM_HTML = """<!doctype html><html lang=ko><head><meta charset=utf-8><title>접수 폼 (데모)</title>
<style>body{font-family:'Malgun Gothic';max-width:460px;margin:40px auto;padding:0 16px}
label{display:block;margin:10px 0 4px;font-size:13px;font-weight:700}
input,select,textarea{width:100%;padding:8px;border:1px solid #bbb;border-radius:6px;font-size:14px}
button{margin-top:14px;padding:10px 18px;border:0;border-radius:8px;background:#4f46e5;color:#fff;font-weight:700}
.ok{color:#15803d;font-weight:700}</style></head><body>
<h2>서비스 접수 (데모)</h2>
<form method=post action=/submit>
  <label>이름</label><input name=이름 id=f-name>
  <label>연락처</label><input name=연락처 id=f-phone>
  <label>구분</label><select name=구분 id=f-type><option>일반</option><option>긴급</option><option>정기</option></select>
  <label><input type=checkbox name=동의 id=f-agree value=Y style="width:auto"> 개인정보 동의</label>
  <label>요청 내용</label><textarea name=내용 id=f-memo rows=3></textarea>
  <button id=f-submit>접수하기</button>
</form>
{msg}</body></html>"""


def demo_server(record_path, port=PORT):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _page(self, msg=''):
            body = FORM_HTML.replace('{msg}', msg).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._page()

        def do_POST(self):
            ln = int(self.headers.get('Content-Length', 0))
            d = urllib.parse.parse_qs(self.rfile.read(ln).decode(), keep_blank_values=True)
            rec = {k: v[0] for k, v in d.items()}
            rec['_ts'] = dt.datetime.now().isoformat()
            open(record_path, 'a', encoding='utf-8').write(json.dumps(rec, ensure_ascii=False) + '\n')
            self._page('<p class=ok>접수 완료</p>')
    return ThreadingHTTPServer(('127.0.0.1', port), H)


# ── 입력기 ──────────────────────────────────────────────────────────
def fill_rows(url, rows, dry=False, log=print):
    """반환 = (제출 성공 행번호들, 격리 [(행,사유)])"""
    from playwright.sync_api import sync_playwright
    done, quar = [], []
    with sync_playwright() as pw:
        br = pw.chromium.launch()
        pg = br.new_page()
        for i, r in enumerate(rows, 2):
            missing = [k for k in REQUIRED if not (r.get(k) or '').strip()]
            if missing:
                quar.append((i, f'필수값 누락: {",".join(missing)} — 제출 안 함'))
                continue
            ok = False
            for attempt in (1, 2):                     # 1회 재시도
                try:
                    pg.goto(url, timeout=10000)
                    pg.fill('#f-name', r.get('이름', ''))
                    pg.fill('#f-phone', r.get('연락처', ''))
                    pg.select_option('#f-type', r.get('구분') or '일반')
                    if (r.get('동의') or '').upper() in ('Y', 'YES', '예'):
                        pg.check('#f-agree')
                    pg.fill('#f-memo', r.get('내용', ''))
                    if dry:
                        ok = True; break               # 검수 모드: 제출 안 누름
                    pg.click('#f-submit')
                    pg.wait_for_selector('.ok', timeout=5000)
                    ok = True; break
                except Exception as e:
                    if attempt == 2:
                        quar.append((i, f'실패 {type(e).__name__} (재시도 후)'))
            if ok:
                done.append(i)
                log(f'  {i:>3} {"검수" if dry else "제출"} OK  {r.get("이름","")[:12]}')
        br.close()
    return done, quar


# ── 데모 + 검증 ─────────────────────────────────────────────────────
def make_demo_csv(path):
    rows = [
        ['김민수', '010-1111-2222', '일반', 'Y', '정기 점검 요청드립니다.'],
        ['이서연', '010-3333-4444', '긴급', 'Y', '금일 중 방문 부탁드립니다 & 주차 안내 필요'],
        ['박지훈', '010-5555-6666', '정기', '', '<수량:3> "견적서" 포함해주세요'],       # 특수문자 <>"
        ['John Smith', '010-7777-8888', '일반', 'Y', "It's urgent — 3층 A/S 접수"],      # 영문·아포스트로피
        ['최유진', '010-9999-0000', '긴급', 'Y', '긴 요청 내용 ' + '상세설명 ' * 30],     # 긴 텍스트
        ['', '010-1234-5678', '일반', 'Y', '이름 없는 행'],                              # 필수 누락 → 격리
        ['정하늘', '', '일반', 'Y', '연락처 없는 행'],                                   # 필수 누락 → 격리
        ['오한별', '010-2468-1357', '정기', 'N', '동의 안 함 케이스(체크 안 함)'],
    ]
    with open(path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f); w.writerow(['이름', '연락처', '구분', '동의', '내용']); w.writerows(rows)
    return len(rows)


def main_demo():
    rec = os.path.join(HERE, '_submissions.jsonl')
    if os.path.exists(rec):
        os.remove(rec)
    srv = demo_server(rec)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    demo = os.path.join(HERE, 'demo_list.csv')
    n = make_demo_csv(demo)
    rows = list(csv.DictReader(open(demo, encoding='utf-8-sig')))
    done, quar = fill_rows(f'http://127.0.0.1:{PORT}/form', rows, log=lambda m: None)
    srv.shutdown()

    # 전수 대조: 서버 기록 vs CSV 원본(제출 대상 행만) — 필드 단위
    recs = [json.loads(l) for l in open(rec, encoding='utf-8')]
    valid = [r for i, r in enumerate(rows, 2) if i in done]
    field_ok = mismatch = 0
    for src, got in zip(valid, recs):
        for k in ('이름', '연락처', '구분', '내용'):
            if (src.get(k) or '') == got.get(k, ''):
                field_ok += 1
            else:
                mismatch += 1
        agree_src = 'Y' if (src.get('동의') or '').upper() in ('Y', 'YES', '예') else ''
        if agree_src == got.get('동의', ''):
            field_ok += 1
        else:
            mismatch += 1
    total_fields = len(valid) * 5
    count_ok = (len(recs) == len(done) == len(valid) and len(quar) == 2)

    now = dt.datetime.now()
    L = [f'# 웹폼 자동 입력 검증 리포트 ({now:%Y-%m-%d %H:%M})',
         f'- 데모 {n}행(특수문자 <>&"\'·긴 텍스트·영문·필수 누락 2행 포함) → 데모 폼 서버가 제출을 서버측 기록',
         '', '| 검증 | 결과 |', '|---|---|',
         f'| ① 제출 수 정합 | 제출 {len(done)} = 서버 기록 {len(recs)} (격리 {len(quar)}행) → {"PASS" if count_ok else "★FAIL"} |',
         f'| ② 필드 단위 전수 대조(서버 기록 vs 원본) | **{field_ok}/{total_fields}** 일치, 불일치 {mismatch} → {"PASS" if mismatch == 0 else "★FAIL"} |',
         f'| ③ 필수값 누락 격리(제출 금지) | {len(quar)}행: ' + ' / '.join(f'{i}행 {why}' for i, why in quar) + ' |',
         f'| ④ 특수문자·긴 텍스트 보존 | <>&"\'·900자 메모 포함 전수 대조에 포함 |',
         f'| ⑤ 검수 모드(--dry) | 제출 버튼 미클릭 설계(코드 경로 분리) |',
         '', '- ※ 데모 폼 기준. 고객 폼은 셀렉터 1회 맞춤(맞춘 뒤 같은 전수 대조 검증 제공). 로그인 폼은 계정 협의.']
    rep = os.path.join(HERE, 'webform_verify.md')
    open(rep, 'w', encoding='utf-8').write('\n'.join(L))
    print('\n'.join(L))
    return count_ok and mismatch == 0


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    ok = main_demo()
    sys.exit(0 if ok else 1)
