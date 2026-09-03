#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""email_notify.py — 이메일 발송 자동화 (개인화 템플릿·첨부·재시도·발송 대장)

수신자 목록(CSV) × 템플릿 → 개인화 메일을 자동 발송한다.
용도 = 거래·운영 알림(주문 확인·정산 통지·리포트 배포 등, **고객이 자기 수신자에게 보내는 메일**).

설계 (대량 메일 사고 방지가 본체):
  - 개인화: 제목·본문의 {이름}{금액}식 자리표를 행 값으로 치환 · 한글 제목/본문 UTF-8 · 첨부 지원
  - ★발송 대장: 수신자별 성공/실패/재시도 기록 → **재실행 시 이미 성공한 수신자 스킵**(중복 발송 0)
  - 실패 재시도 1회 + 영구 실패는 보류 목록에 명시(묵살 금지) — 나머지 발송은 계속
  - 검수 모드(--dry): 실제 발송 없이 렌더 결과만 대장으로

검증(--make-demo) = **서버 수신 대조**: 로컬 SMTP 수신 서버(진실 보유)를 띄우고
  ①발송 의도 vs 서버 수신함 전수 대조(수신자·개인화 제목·본문·첨부 해시) ②개인화 수기 대조
  ③일시 거부→재시도 성공 ④영구 거부→보류 격리 ⑤재실행 중복 0 ⑥재현성.
※ 데모 = 로컬 수신 서버(외부 발송 없음). 실서비스 = 고객 SMTP/메일 API 자격으로 접속 정보만 교체.
"""
import os, sys, csv, json, time, socket, hashlib, threading, socketserver, datetime as dt
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from email.header import Header, decode_header
from email import message_from_bytes
from email.utils import parseaddr

HERE = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.path.join(HERE, 'send_ledger.json')


# ── 발송기 ──────────────────────────────────────────────────────────
def render(tmpl, row):
    out = tmpl
    for k, v in row.items():
        out = out.replace('{' + k + '}', str(v))
    return out


def build_msg(sender, to, subject, body, attach=None):
    m = MIMEMultipart()
    m['From'] = sender
    m['To'] = to
    m['Subject'] = Header(subject, 'utf-8')
    m.attach(MIMEText(body, 'plain', 'utf-8'))
    if attach:
        with open(attach, 'rb') as f:
            part = MIMEApplication(f.read())
        part.add_header('Content-Disposition', 'attachment', filename=os.path.basename(attach))
        m.attach(part)
    return m


def send_all(host, port, sender, rows, subj_tmpl, body_tmpl, attach=None,
             ledger_path=LEDGER, dry=False, retry=1, wait=0.2):
    """rows = [{'email':…, 개인화 키…}] → 대장 갱신. 반환 (성공, 스킵, 보류) 리스트."""
    ledger = {}
    if os.path.exists(ledger_path):
        try:
            ledger = json.load(open(ledger_path, encoding='utf-8'))
        except Exception:
            ledger = {}
    ok, skip, held = [], [], []
    for row in rows:
        to = row['email'].strip()
        if ledger.get(to, {}).get('status') == 'ok':
            skip.append(to)                       # ★중복 발송 방지(재실행 안전)
            continue
        subject, body = render(subj_tmpl, row), render(body_tmpl, row)
        if dry:
            ledger[to] = {'status': 'dry', 'subject': subject, 'at': dt.datetime.now().isoformat(' ', 'seconds')}
            continue
        last_err = ''
        for attempt in range(retry + 1):
            try:
                with smtplib.SMTP(host, port, timeout=10) as s:
                    s.send_message(build_msg(sender, to, subject, body, attach))
                ledger[to] = {'status': 'ok', 'attempts': attempt + 1,
                              'at': dt.datetime.now().isoformat(' ', 'seconds')}
                ok.append(to)
                break
            except Exception as e:
                last_err = f'{type(e).__name__}: {e}'
                if attempt < retry:
                    time.sleep(wait)
        else:
            ledger[to] = {'status': 'held', 'error': last_err,
                          'at': dt.datetime.now().isoformat(' ', 'seconds')}
            held.append((to, last_err))           # 보류 = 묵살 금지, 목록으로 명시
    json.dump(ledger, open(ledger_path, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    return ok, skip, held


# ── 검증용 로컬 SMTP 수신 서버 (진실 보유) ─────────────────────────
class Inbox:
    def __init__(self):
        self.msgs = []                            # [(rcpt, raw_bytes)]
        self.reject_once = set()                  # 1회 거부 후 통과(재시도 검증)
        self.reject_always = set()                # 영구 거부(보류 검증)
        self.lock = threading.Lock()


def smtp_sink(inbox, port):
    class H(socketserver.StreamRequestHandler):
        def w(self, s):
            self.wfile.write((s + '\r\n').encode())

        def handle(self):
            self.w('220 demo-sink')
            rcpts, buf = [], None
            while True:
                line = self.rfile.readline()
                if not line:
                    return
                if buf is not None:                       # DATA 수집 중
                    if line.rstrip(b'\r\n') == b'.':
                        raw = b''.join(b[1:] if b.startswith(b'.') else b for b in buf)
                        with inbox.lock:
                            for r in rcpts:
                                inbox.msgs.append((r, raw))
                        buf = None
                        rcpts = []
                        self.w('250 OK stored')
                    else:
                        buf.append(line)
                    continue
                cmd = line.decode('utf-8', 'replace').strip()
                up = cmd.upper()
                if up.startswith(('HELO', 'EHLO')):
                    self.w('250 hello')
                elif up.startswith('MAIL FROM'):
                    self.w('250 ok')
                elif up.startswith('RCPT TO'):
                    addr = parseaddr(cmd.split(':', 1)[1])[1]
                    with inbox.lock:
                        if addr in inbox.reject_always:
                            self.w('550 rejected'); continue
                        if addr in inbox.reject_once:
                            inbox.reject_once.discard(addr)
                            self.w('451 try again'); continue
                    rcpts.append(addr)
                    self.w('250 ok')
                elif up.startswith('DATA'):
                    buf = []
                    self.w('354 go')
                elif up.startswith('QUIT'):
                    self.w('221 bye'); return
                else:
                    self.w('250 ok')
    srv = socketserver.ThreadingTCPServer(('127.0.0.1', port), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def parse_stored(raw):
    m = message_from_bytes(raw)
    subj = ''.join(t.decode(enc or 'utf-8') if isinstance(t, bytes) else t
                   for t, enc in decode_header(m['Subject']))
    body, att = '', b''
    for p in m.walk():
        if p.get_content_type() == 'text/plain':
            body = p.get_payload(decode=True).decode('utf-8')
        elif p.get_content_disposition() == 'attachment':
            att = p.get_payload(decode=True)
    return subj, body, att


# ── 데모 + 검증 ─────────────────────────────────────────────────────
def main_demo():
    port = 8794
    inbox = Inbox()
    srv = smtp_sink(inbox, port)
    time.sleep(0.2)

    # 수신자 30명 CSV(개인화 값 포함) + 첨부 + 실패 주입 2명
    demo_csv = os.path.join(HERE, 'demo_recipients.csv')
    rows = [{'email': f'user{i:02d}@example.com', '이름': f'고객{i:02d}',
             '금액': f'{(i + 1) * 1500:,}', '주문번호': f'ORD-2026-{i:04d}'} for i in range(30)]
    with open(demo_csv, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['email', '이름', '금액', '주문번호'])
        w.writeheader(); w.writerows(rows)
    attach = os.path.join(HERE, 'demo_attach.txt')
    open(attach, 'w', encoding='utf-8').write('9월 정산 요약 (데모 첨부)\n합계 1,234,500원\n')
    att_sha = hashlib.sha256(open(attach, 'rb').read()).hexdigest()

    inbox.reject_once.add('user07@example.com')       # ③ 일시 거부 → 재시도 성공 기대
    inbox.reject_always.add('user23@example.com')     # ④ 영구 거부 → 보류 기대

    if os.path.exists(LEDGER):
        os.remove(LEDGER)
    subj_t = '[한빛상사] {이름}님 {주문번호} 정산 안내'
    body_t = '{이름}님, 주문 {주문번호}의 정산 금액은 {금액}원입니다.\n첨부의 요약을 확인해 주세요.'
    loaded = list(csv.DictReader(open(demo_csv, encoding='utf-8-sig')))
    ok1, skip1, held1 = send_all('127.0.0.1', port, 'noreply@demo.local', loaded, subj_t, body_t, attach)

    # ① 전수 대조: 서버 수신함 vs 발송 의도 (수신자·개인화 제목·본문·첨부 해시)
    with inbox.lock:
        got = {r: parse_stored(raw) for r, raw in inbox.msgs}
    mism = 0
    for row in loaded:
        to = row['email']
        if to == 'user23@example.com':
            continue                                   # 영구 거부자는 수신 없어야 정상
        if to not in got:
            mism += 1; continue
        subj, body, att = got[to]
        if subj != render(subj_t, row) or body != render(body_t, row) \
                or hashlib.sha256(att).hexdigest() != att_sha:
            mism += 1
    full_ok = (mism == 0 and len(got) == 29 and 'user23@example.com' not in got)

    # ② 개인화 수기 대조 1건: user05 제목/본문에 고객05·ORD-2026-0005·9,000
    s5, b5, _ = got['user05@example.com']
    hand_ok = ('고객05' in s5 and 'ORD-2026-0005' in s5 and '9,000' in b5)

    # ③ 재시도: user07 = 시도 2회 후 성공으로 대장 기록
    ledger = json.load(open(LEDGER, encoding='utf-8'))
    retry_ok = (ledger['user07@example.com']['status'] == 'ok'
                and ledger['user07@example.com']['attempts'] == 2)

    # ④ 보류 격리: user23 = held 명시 + 나머지 29명 전원 발송
    held_ok = (len(held1) == 1 and held1[0][0] == 'user23@example.com' and len(ok1) == 29)

    # ⑤ 재실행 중복 0: 다시 돌리면 성공자 29명 전원 스킵(서버 수신함 증가 0)
    n_before = len(inbox.msgs)
    ok2, skip2, held2 = send_all('127.0.0.1', port, 'noreply@demo.local', loaded, subj_t, body_t, attach)
    dup_ok = (len(skip2) == 29 and len(ok2) == 0 and len(inbox.msgs) == n_before)

    # ⑥ 검수 모드(--dry) 실증: 새 대장으로 dry 실행 → 서버 수신 증가 0 + 전원 dry 기록
    dry_ledger = os.path.join(HERE, 'send_ledger_dry.json')
    if os.path.exists(dry_ledger):
        os.remove(dry_ledger)
    send_all('127.0.0.1', port, 'noreply@demo.local', loaded, subj_t, body_t, attach,
             ledger_path=dry_ledger, dry=True)
    dl = json.load(open(dry_ledger, encoding='utf-8'))
    dry_ok = (len(inbox.msgs) == n_before
              and len(dl) == 30 and all(v['status'] == 'dry' for v in dl.values()))
    srv.shutdown()

    L = [f'# 이메일 발송 자동화 검증 리포트 ({dt.datetime.now():%Y-%m-%d %H:%M})',
         '- 데모 = **로컬 SMTP 수신 서버(진실 보유)** + 수신자 30명(개인화 값)·첨부 1종·실패 주입 2명(일시/영구)',
         '', '| 검증 | 결과 |', '|---|---|',
         f'| ① 전수 수신 대조(수신자·개인화 제목·본문·첨부 SHA-256) | 수신 {len(got)}/29 · 불일치 {mism}건 → {"PASS" if full_ok else "★FAIL"} |',
         f'| ② 개인화 수기 대조(user05: 이름·주문번호·금액) | {"PASS" if hand_ok else "★FAIL"} |',
         f'| ③ 일시 거부 → 재시도 성공(user07, 시도 2회) | {"PASS" if retry_ok else "★FAIL"} |',
         f'| ④ 영구 거부 → 보류 격리(user23) + 나머지 {len(ok1)}명 발송 | {"PASS" if held_ok else "★FAIL"} |',
         f'| ⑤ ★재실행 중복 0(성공자 {len(skip2)}명 스킵·서버 수신 증가 0) | {"PASS" if dup_ok else "★FAIL"} |',
         f'| ⑥ 검수 모드(--dry) 실증(발송 0 · 30명 전원 dry 기장) | {"PASS" if dry_ok else "★FAIL"} |',
         '', '- 발송 대장 = `send_ledger.json`(수신자별 성공/시도수/보류 사유) · 검수 모드 `--dry` 내장',
         '- ※ 데모 = 로컬 수신 서버(외부 발송 0). 실서비스 = 고객 SMTP/메일 API 자격으로 접속 정보만 교체.'
         ' 용도 = 거래·운영 알림(고객이 자기 수신자에게) — 무차별 대량 발송 도구 아님.']
    rep = os.path.join(HERE, 'email_verify.md')
    open(rep, 'w', encoding='utf-8').write('\n'.join(L))
    print('\n'.join(L))
    return full_ok and hand_ok and retry_ok and held_ok and dup_ok and dry_ok


def _arg(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    if len(sys.argv) > 1 and sys.argv[1].endswith('.csv'):
        # 실사용: python email_notify.py 수신자.csv --subject "…{이름}…" --body-file 본문.txt
        #         [--attach 파일] [--host smtp주소 --port 587 --from 발신주소] [--dry]
        rows = list(csv.DictReader(open(sys.argv[1], encoding='utf-8-sig')))
        subj = _arg('--subject', '(제목 없음)')
        body = open(_arg('--body-file'), encoding='utf-8').read() if _arg('--body-file') else ''
        ok, skip, held = send_all(_arg('--host', '127.0.0.1'), int(_arg('--port', '25')),
                                  _arg('--from', 'noreply@localhost'), rows, subj, body,
                                  attach=_arg('--attach'), dry='--dry' in sys.argv)
        print(f'성공 {len(ok)} · 스킵(기발송) {len(skip)} · 보류 {len(held)}'
              + (' [검수 모드 — 발송 안 함]' if '--dry' in sys.argv else ''))
        for to, err in held:
            print(f'  보류: {to} — {err}')
        sys.exit(0 if not held else 2)
    ok = main_demo()
    sys.exit(0 if ok else 1)
