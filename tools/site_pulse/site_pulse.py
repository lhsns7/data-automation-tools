#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""site_pulse.py — 업타임·SSL 만료 감시 (상태 변화 알림·200 착시 방지)

사이트 감시의 세 함정을 겨눈다:
  ① 죽은 걸 모름 — 주기 프로브(HTTP 상태·응답시간·타임아웃)
  ② ★200 착시 — 서버는 200인데 내용이 "점검 중" 페이지인 경우: 본문 기대 키워드 검사로 검출
  ③ ★SSL 만료 사고 — 인증서 만료를 까먹으면 어느 날 전 방문자가 브라우저 경고를 봄:
     만료 D-day 단계 경고(D-30 WARN → D-7 CRIT → 만료 EXPIRED)
알림 = **상태가 바뀔 때만**(UP→DOWN·회복·SSL 단계 진입) — 경보 피로 방지. 채널 훅 교체식.

검증(--make-demo): 로컬 서버 3종(정상 200 / 500 / 응답 지연=타임아웃) + 점검페이지(200 착시) 케이스
  ①UP·응답시간 ②500=DOWN ③타임아웃=DOWN ④200 착시=DOWN(키워드) ⑤상태 변화만 알림(동일 상태
  재검사 0·회복 알림 1) ⑥SSL 판정 경계 주입(D-45/20/3/만료) + 실인증서 1건 실측 파싱 ⑦재현성.
※ URL 목록·기대 키워드·타임아웃·SSL 경고 단계 = 설정값. 크론 1줄 운영.
"""
import os, sys, json, ssl, time, socket, threading, datetime as dt
import urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, 'pulse_state.json')
TIMEOUT = 1.5                                              # 프로브 타임아웃(초, 설정값)
SSL_WARN, SSL_CRIT = 30, 7                                 # SSL 경고 단계(일, 설정값)


def probe(url, expect=None, timeout=TIMEOUT):
    """→ (상태, 상세, 응답ms). 상태 = UP/DOWN."""
    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'site-pulse/1.0'})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(65536).decode('utf-8', 'ignore')
            ms = int((time.time() - t0) * 1000)
            if r.status != 200:
                return 'DOWN', f'HTTP {r.status}', ms
            if expect and expect not in body:
                return 'DOWN', f'★200이지만 기대 키워드 "{expect}" 없음(점검 페이지 의심)', ms
            return 'UP', f'{ms}ms', ms
    except urllib.error.HTTPError as e:
        return 'DOWN', f'HTTP {e.code}', int((time.time() - t0) * 1000)
    except Exception as e:
        return 'DOWN', f'{type(e).__name__}(타임아웃/연결 실패)', int((time.time() - t0) * 1000)


def ssl_grade(not_after, today):
    """만료일 → (등급, D-day). 등급 = OK/WARN/CRIT/EXPIRED. 경계 = 그날 포함."""
    days = (not_after - today).days
    if days < 0:
        return 'EXPIRED', days
    if days <= SSL_CRIT:
        return 'CRIT', days
    if days <= SSL_WARN:
        return 'WARN', days
    return 'OK', days


def fetch_cert_expiry(host, port=443, timeout=6):
    """실연결로 인증서 만료일 읽기 → date. (실측용 — 판정 로직은 ssl_grade)"""
    ctx = ssl.create_default_context()
    with socket.create_connection((host, port), timeout=timeout) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as tls:
            cert = tls.getpeercert()
    exp = dt.datetime.strptime(cert['notAfter'], '%b %d %H:%M:%S %Y %Z')
    return exp.date()


def tick(sites, state_path=STATE, notify=None, today=None):
    """감시 1회: 프로브+SSL → 상태 변화만 알림. sites = [dict(name,url,expect?,ssl_host?)]"""
    today = today or dt.date.today()
    prev = {}
    if os.path.exists(state_path):
        try:
            prev = json.load(open(state_path, encoding='utf-8'))
        except Exception:
            prev = {}
    result, changes = {}, []
    for s in sites:
        st, detail, ms = probe(s['url'], s.get('expect'))
        key = s['name']
        cur = st
        if s.get('ssl_host'):
            try:
                grade, days = ssl_grade(fetch_cert_expiry(s['ssl_host']), today)
                detail += f' · SSL {grade}(D-{days})'
                if grade != 'OK':
                    cur = f'{st}/SSL_{grade}'
            except Exception as e:
                detail += f' · SSL 확인 실패({type(e).__name__})'
        result[key] = (cur, detail, ms)
        old = prev.get(key)
        if old != cur and not (old is None and cur == 'UP'):   # 최초 관측 UP은 조용
            mark = '✅ 회복' if cur == 'UP' else '🚨'
            changes.append(f'{mark} [{key}] {old or "-"} → {cur}: {detail}')
    json.dump({k: v[0] for k, v in result.items()}, open(state_path, 'w', encoding='utf-8'),
              ensure_ascii=False)
    if notify:
        for c in changes:
            notify(c)
    return result, changes


# ── 검증 데모: 로컬 서버 3종 ───────────────────────────────────────
def serve_demo(port, mode):
    """mode = ok / err500 / slow / maint(점검 페이지 200)"""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            try:
                if mode == 'slow':
                    time.sleep(TIMEOUT + 1.5)
                if mode == 'err500':
                    self.send_response(500); self.end_headers(); return
                body = ('<h1>점검 중입니다</h1>' if mode == 'maint'
                        else '<h1>우리 서비스</h1><p>정상 운영</p>').encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.end_headers()
                self.wfile.write(body)
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                pass                                        # 프로브가 타임아웃으로 끊은 뒤의 사후 write
    srv = ThreadingHTTPServer(('127.0.0.1', port), H)
    srv.mode = mode
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main_demo():
    if os.path.exists(STATE):
        os.remove(STATE)
    s_ok = serve_demo(8811, 'ok')
    s_err = serve_demo(8812, 'err500')
    s_slow = serve_demo(8813, 'slow')
    s_maint = serve_demo(8814, 'maint')
    time.sleep(0.2)
    sites = [dict(name='정상몰', url='http://127.0.0.1:8811/', expect='정상 운영'),
             dict(name='죽은API', url='http://127.0.0.1:8812/'),
             dict(name='느린서버', url='http://127.0.0.1:8813/'),
             dict(name='점검몰', url='http://127.0.0.1:8814/', expect='정상 운영')]
    alerts = []

    # 1차 틱: ①UP ②500 DOWN ③타임아웃 DOWN ④200 착시 DOWN
    r1, ch1 = tick(sites, notify=alerts.append)
    ok1 = (r1['정상몰'][0] == 'UP' and r1['정상몰'][2] < 1000)
    ok2 = (r1['죽은API'][0] == 'DOWN' and '500' in r1['죽은API'][1])
    ok3 = (r1['느린서버'][0] == 'DOWN' and '타임아웃' in r1['느린서버'][1])
    ok4 = (r1['점검몰'][0] == 'DOWN' and '키워드' in r1['점검몰'][1])
    first_alerts = len(alerts)                              # DOWN 3건 알림(최초 UP은 조용)

    # ⑤ 상태 변화만: 같은 상태 재검사 = 알림 0 → 죽은API 회복 = 회복 알림 1
    r2, ch2 = tick(sites, notify=alerts.append)
    same_silent = (ch2 == [])
    s_err.shutdown()
    s_err.server_close()                                    # ★1차 검거: shutdown()만으론 소켓이 살아
    time.sleep(0.3)                                         #   같은 포트 재기동 연결이 죽은 리스너로 감
    s_ok2 = serve_demo(8812, 'ok')                          # 같은 포트로 정상 서버 재기동
    time.sleep(0.3)
    sites2 = [dict(name='죽은API', url='http://127.0.0.1:8812/')]
    r3, ch3 = tick(sites2, notify=alerts.append)
    ok5 = (first_alerts == 3 and same_silent
           and len(ch3) == 1 and '회복' in ch3[0] and r3['죽은API'][0] == 'UP')

    # ⑥ SSL 판정 경계 주입 + 실인증서 1건 실측
    today = dt.date(2026, 9, 4)
    ok6a = (ssl_grade(today + dt.timedelta(days=45), today)[0] == 'OK'
            and ssl_grade(today + dt.timedelta(days=20), today)[0] == 'WARN'
            and ssl_grade(today + dt.timedelta(days=30), today)[0] == 'WARN'   # 경계=포함
            and ssl_grade(today + dt.timedelta(days=3), today)[0] == 'CRIT'
            and ssl_grade(today + dt.timedelta(days=7), today)[0] == 'CRIT'
            and ssl_grade(today - dt.timedelta(days=1), today)[0] == 'EXPIRED')
    try:
        exp = fetch_cert_expiry('www.python.org')
        real = f'www.python.org 만료일 {exp} 파싱 성공'
        ok6b = (exp > dt.date.today())
    except Exception as e:
        real, ok6b = f'실연결 실패 {type(e).__name__}', False
    ok6 = ok6a and ok6b

    # ⑦ 재현성: 같은 상태 재프로브 = 같은 판정
    r4, _ = tick(sites2)
    ok7 = (r4['죽은API'][0] == 'UP')
    for srv in (s_ok, s_slow, s_maint, s_ok2):
        srv.shutdown()
        srv.server_close()

    L = [f'# 업타임·SSL 감시 검증 리포트 ({dt.datetime.now():%Y-%m-%d %H:%M})',
         '- 데모 = 로컬 서버 4종(정상 / 500 / 응답지연 / ★점검 페이지 200) + SSL 경계 주입 + 실인증서 실측',
         '', '| 검증 | 결과 |', '|---|---|',
         f'| ① 정상 = UP·응답시간 기록({r1["정상몰"][2]}ms) | {"PASS" if ok1 else "★FAIL"} |',
         f'| ② HTTP 500 = DOWN(사유 명시) | {"PASS" if ok2 else "★FAIL"} |',
         f'| ③ 응답 지연 = DOWN(타임아웃) | {"PASS" if ok3 else "★FAIL"} |',
         f'| ④ ★200 착시(점검 페이지) = DOWN(키워드 검사) | {"PASS" if ok4 else "★FAIL"} |',
         f'| ⑤ 상태 변화만 알림(최초 DOWN 3건 · 재검사 0건 · 회복 1건) | {"PASS" if ok5 else "★FAIL"} |',
         f'| ⑥ SSL 판정 경계(D-45 OK/D-30·20 WARN/D-7·3 CRIT/만료) + 실측({real}) | {"PASS" if ok6 else "★FAIL"} |',
         f'| ⑦ 재현성 | {"PASS" if ok7 else "★FAIL"} |',
         '', '- ※ URL·기대 키워드·타임아웃·SSL 단계 = 설정값. 크론 1줄 운영, 알림 채널 훅 교체식.',
         '- ※ 200 착시 검사 = 상태 코드만 믿지 않고 본문 내용까지 — "살아 있다"의 기준을 사용자 화면으로.']
    rep = os.path.join(HERE, 'pulse_verify.md')
    open(rep, 'w', encoding='utf-8').write('\n'.join(L))
    print('\n'.join(L))
    return ok1 and ok2 and ok3 and ok4 and ok5 and ok6 and ok7


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    ok = main_demo()
    sys.exit(0 if ok else 1)
