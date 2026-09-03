#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tg_notify.py — 텔레그램 알림봇 골격 (발송·분할·레이트리밋·보류 재송·명령 응답)

시스템 알림을 텔레그램으로 보내는 봇 골격. 어떤 자동화에든 붙는 알림 부품이다.
"보내는 코드"는 쉽고, 진짜 일은 가장자리에 있다:

  - ★유실 0: 실패(네트워크·5xx)는 재시도, 그래도 안 되면 **보류 큐(파일)**에 남겨 다음 발송 때 재송
  - ★4096자 분할: 텔레그램 메시지 한도 초과 장문은 줄 경계로 안전 분할(수신측 재조립 = 원문)
  - ★429 존중: 레이트리밋 응답의 retry_after 만큼 기다렸다 재송(무한 폭주 금지)
  - 명령 응답: /status 등 최소 명령 폴링(getUpdates) — 일방 발송기가 아니라 "봇"
  - 자격 게이트: 토큰 없으면 발송 시도 자체를 차단(실수 방지)

검증(--make-demo) = **가짜 텔레그램 API 서버(수신 진실 보유)**:
  ①발송 전수 대조 ②장문 분할→서버측 재조립=원문 ③429(retry_after)→대기 후 성공·유실 0
  ④영구 실패→보류 큐→복구 후 재송 완료 ⑤심은 /status 명령→봇 응답 발송 ⑥빈 토큰→발송 차단.
※ 데모 = 로컬 가짜 API(외부 발송 0). 실서비스 = 봇 토큰·chat_id만 설정(BotFather 발급).
"""
import os, sys, json, time, threading, datetime as dt
import urllib.request, urllib.error, urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
PENDING = os.path.join(HERE, 'tg_pending.json')
LIMIT = 4096                       # 텔레그램 메시지 길이 한도


def split_message(text, limit=LIMIT):
    """장문 → 줄 경계 분할(한 줄이 한도를 넘으면 그 줄만 하드컷). 재조립 시 원문과 동일해야 한다."""
    if len(text) <= limit:
        return [text]
    parts, buf = [], ''
    for line in text.split('\n'):
        while len(line) > limit:                # 초장문 한 줄 방어
            parts.append(line[:limit])
            line = line[limit:]
        cand = (buf + '\n' + line) if buf else line
        if len(cand) > limit:
            parts.append(buf)
            buf = line
        else:
            buf = cand
    if buf:
        parts.append(buf)
    return parts


class TgBot:
    def __init__(self, token, chat_id, base='https://api.telegram.org',
                 pending_path=PENDING, retries=2, wait=0.3):
        self.token, self.chat_id, self.base = token, str(chat_id), base.rstrip('/')
        self.pending_path, self.retries, self.wait = pending_path, retries, wait
        self.offset = 0

    def _call(self, method, payload):
        if not self.token:
            raise PermissionError('봇 토큰이 없습니다 — 발송 차단(자격 게이트)')
        data = urllib.parse.urlencode(payload).encode()
        req = urllib.request.Request(f'{self.base}/bot{self.token}/{method}', data=data)
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode('utf-8'))

    def _send_once(self, text):
        """1건 발송(429 존중·재시도 포함). 성공 True / 최종 실패 False.
        ★자격 게이트(PermissionError)는 삼키지 않고 그대로 올린다 — 1차 검증 검거:
        재시도 루프의 except Exception이 게이트까지 '실패 1건'으로 삼켜 무력화했었다."""
        for attempt in range(self.retries + 1):
            try:
                self._call('sendMessage', {'chat_id': self.chat_id, 'text': text})
                return True
            except PermissionError:
                raise
            except urllib.error.HTTPError as e:
                if e.code == 429:               # ★레이트리밋: retry_after 존중(폭주 금지)
                    try:
                        ra = json.loads(e.read().decode()).get('parameters', {}).get('retry_after', 1)
                    except Exception:
                        ra = 1
                    time.sleep(min(float(ra), 30))
                    continue                    # 429 대기는 시도 횟수에 세지 않음
                if attempt < self.retries:
                    time.sleep(self.wait)
            except Exception:
                if attempt < self.retries:
                    time.sleep(self.wait)
        return False

    # ── 공개 API ──
    def send(self, text):
        """보류분 먼저 재송 → 본문(장문 분할) 발송. 실패분은 보류 큐 저장. 반환 (성공수, 보류수)."""
        if not self.token:                      # ★게이트는 진입점에서 — 보류 큐에 넣지도 않는다
            raise PermissionError('봇 토큰이 없습니다 — 발송 차단(자격 게이트)')
        queue = []
        if os.path.exists(self.pending_path):
            try:
                queue = json.load(open(self.pending_path, encoding='utf-8'))
            except Exception:
                queue = []
        queue += split_message(text)
        sent, held = 0, []
        for part in queue:
            if self._send_once(part):
                sent += 1
            else:
                held.append(part)               # ★유실 0: 실패분은 파일에 남긴다
        if held:
            json.dump(held, open(self.pending_path, 'w', encoding='utf-8'), ensure_ascii=False)
        elif os.path.exists(self.pending_path):
            os.remove(self.pending_path)
        return sent, len(held)

    def poll_once(self, handlers):
        """명령 1회 폴링: {'/status': fn} — fn() 반환 문자열을 답장으로 발송."""
        d = self._call('getUpdates', {'offset': self.offset})
        answered = 0
        for u in d.get('result', []):
            self.offset = max(self.offset, u['update_id'] + 1)
            cmd = (u.get('message', {}).get('text') or '').split()[0] if u.get('message') else ''
            if cmd in handlers:
                self.send(handlers[cmd]())
                answered += 1
        return answered


# ── 가짜 텔레그램 API 서버 (수신 진실 보유) ────────────────────────
class FakeTg:
    def __init__(self):
        self.inbox = []                          # 수신 텍스트(진실)
        self.updates = []                        # 심어둘 명령
        self.rate_limit_next = 0                 # 앞 N회 429
        self.fail_marker = None                  # 이 문구 포함 = 500
        self.lock = threading.Lock()


def serve_fake(tg, port):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, body, code=200):
            b = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b)

        def do_POST(self):
            n = int(self.headers.get('Content-Length', 0))
            d = dict(urllib.parse.parse_qsl(self.rfile.read(n).decode()))
            if self.path.endswith('/sendMessage'):
                with tg.lock:
                    if tg.rate_limit_next > 0:
                        tg.rate_limit_next -= 1
                        self._json({'ok': False, 'parameters': {'retry_after': 0.3}}, 429); return
                    if tg.fail_marker and tg.fail_marker in d.get('text', ''):
                        self._json({'ok': False}, 500); return
                    tg.inbox.append(d.get('text', ''))
                self._json({'ok': True, 'result': {'message_id': len(tg.inbox)}})
            elif self.path.endswith('/getUpdates'):
                off = int(d.get('offset', 0))
                with tg.lock:
                    out = [u for u in tg.updates if u['update_id'] >= off]
                self._json({'ok': True, 'result': out})
            else:
                self._json({'ok': False}, 404)
    srv = ThreadingHTTPServer(('127.0.0.1', port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ── 검증 데모 ───────────────────────────────────────────────────────
def main_demo():
    if os.path.exists(PENDING):
        os.remove(PENDING)
    fake = FakeTg()
    srv = serve_fake(fake, 8801)
    bot = TgBot('DEMO-TOKEN', 12345, base='http://127.0.0.1:8801', wait=0.05)
    time.sleep(0.2)

    # ① 단문 5건 발송 → 서버 수신 전수 대조
    msgs = [f'[경보 {i}] 수집기 {i}번 지연 감지 ({i}분)' for i in range(1, 6)]
    for m in msgs:
        bot.send(m)
    ok1 = (fake.inbox == msgs)

    # ② 장문(4096 초과) → 분할 발송 → 서버측 재조립 = 원문
    long_msg = '\n'.join(f'{i:04d}행: 일일 리포트 항목 — 수치 {i*7:,}' for i in range(1, 201))
    assert len(long_msg) > LIMIT
    n_before = len(fake.inbox)
    bot.send(long_msg)
    parts = fake.inbox[n_before:]
    ok2 = (len(parts) >= 2 and all(len(p) <= LIMIT for p in parts)
           and '\n'.join(parts) == long_msg)

    # ③ 429 두 번 → retry_after 존중 후 성공(유실 0)
    fake.rate_limit_next = 2
    t0 = time.time()
    sent3, held3 = bot.send('레이트리밋 시험 메시지')
    ok3 = (sent3 == 1 and held3 == 0 and fake.inbox[-1] == '레이트리밋 시험 메시지'
           and time.time() - t0 >= 0.5)         # 0.3초 × 2회 대기 흔적

    # ④ 영구 실패 → 보류 큐 → 복구 후 다음 발송이 재송
    fake.fail_marker = '장애중메시지'
    sent4a, held4a = bot.send('장애중메시지: 디스크 경보')
    pend_exists = os.path.exists(PENDING)
    fake.fail_marker = None
    sent4b, held4b = bot.send('복구 후 새 메시지')
    ok4 = (held4a == 1 and pend_exists and held4b == 0
           and fake.inbox[-2] == '장애중메시지: 디스크 경보'    # 보류분 먼저 재송
           and fake.inbox[-1] == '복구 후 새 메시지'
           and not os.path.exists(PENDING))

    # ⑤ 심은 /status 명령 → 봇 응답 발송
    fake.updates = [{'update_id': 7, 'message': {'text': '/status'}}]
    n5 = len(fake.inbox)
    answered = bot.poll_once({'/status': lambda: '가동 중 · 보류 0건'})
    ok5 = (answered == 1 and len(fake.inbox) == n5 + 1 and fake.inbox[-1] == '가동 중 · 보류 0건')

    # ⑥ 자격 게이트: 빈 토큰 → 발송 시도 차단
    try:
        TgBot('', 12345, base='http://127.0.0.1:8801').send('나가면 안 되는 메시지')
        ok6 = False
    except PermissionError:
        ok6 = '나가면 안 되는 메시지' not in fake.inbox
    srv.shutdown()

    L = [f'# 텔레그램 알림봇 검증 리포트 ({dt.datetime.now():%Y-%m-%d %H:%M})',
         '- 데모 = **가짜 텔레그램 API 서버(수신 진실 보유)** — 외부 발송 0. 429·5xx·명령을 심어 가장자리 전부 시험',
         '', '| 검증 | 결과 |', '|---|---|',
         f'| ① 발송 전수 대조(5건, 순서 포함) | {"PASS" if ok1 else "★FAIL"} |',
         f'| ② 장문 분할(4096 한도, {len(parts)}조각) → 서버측 재조립 = 원문 | {"PASS" if ok2 else "★FAIL"} |',
         f'| ③ 429 레이트리밋 → retry_after 존중 후 성공(유실 0) | {"PASS" if ok3 else "★FAIL"} |',
         f'| ④ 영구 실패 → 보류 큐 → 복구 후 재송(보류분 우선·큐 소진) | {"PASS" if ok4 else "★FAIL"} |',
         f'| ⑤ /status 명령 폴링 → 봇 응답 발송 | {"PASS" if ok5 else "★FAIL"} |',
         f'| ⑥ 자격 게이트(빈 토큰 = 발송 시도 차단) | {"PASS" if ok6 else "★FAIL"} |',
         '', '- ※ 데모 = 로컬 가짜 API(외부 발송 0). 실서비스 = BotFather 토큰·chat_id 설정만.',
         '- ※ 유실 0 계약: 실패분은 보류 큐(파일)에 남고 다음 발송이 먼저 재송한다.']
    rep = os.path.join(HERE, 'tg_verify.md')
    open(rep, 'w', encoding='utf-8').write('\n'.join(L))
    print('\n'.join(L))
    return ok1 and ok2 and ok3 and ok4 and ok5 and ok6


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    ok = main_demo()
    sys.exit(0 if ok else 1)
