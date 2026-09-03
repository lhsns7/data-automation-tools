#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""job_sentinel.py — 작업 스케줄러 감시 (하트비트·침묵 실패 검거·상태 변화 알림)

크론·스케줄 작업이 늘수록 진짜 위험은 실패가 아니라 **침묵 실패** — 작업이 죽어도 아무도 모른다.
각 작업이 끝에 하트비트 한 줄을 남기게 하고, 감시기가 등록부와 대조해 무소식을 검거한다.
(수집기·서버 크론을 몇 달 운영하며 겪은 문제의 일반화.)

구조:
  - 작업 등록부(jobs.json): 작업명 · 기대 주기(분) · 유예(분)
  - 하트비트: 작업 끝에서 `--beat 작업명 --ok/--fail [--note …]` 1줄 (또는 beat() 호출) → SQLite 기록
  - 감시(check): 등록부 × 최신 하트비트 →
      OK(주기+유예 안 성공) · ★LATE(주기+유예 초과 무소식 = 침묵 실패) ·
      FAIL(실패 보고) · NEW(하트비트 0건 — 등록만 되고 한 번도 안 돎, 묵살 금지)
  - ★상태 변화 알림: 바뀔 때만 울림(OK→LATE, 회복 LATE→OK 포함) — 매번 울리면 무시하게 된다(경보 피로)
  - 알림 채널 = 훅 교체식(콘솔/파일/이메일/슬랙/텔레그램)

검증(--make-demo) = **가상 시계 주입**(실제 대기 0, 결정적):
  ①정상 3작업 = 전부 OK ②하트비트 끊고 시간 전진 → 그 작업만 LATE ③실패 보고 → FAIL+사유 보존
  ④상태 변화 때만 알림(유지 재검사 = 알림 0, 회복 = 회복 알림) ⑤미실행 작업 = NEW 표기
  ⑥이력 유실 0(기록 수 = 호출 수) + 재검사 재현성.
"""
import os, sys, json, sqlite3, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, 'sentinel.db')
JOBS = os.path.join(HERE, 'jobs.json')


def open_db(path=DB):
    con = sqlite3.connect(path)
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('''CREATE TABLE IF NOT EXISTS beats(
        id INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT NOT NULL, ts TEXT NOT NULL,
        ok INTEGER NOT NULL, note TEXT)''')
    con.execute('''CREATE TABLE IF NOT EXISTS states(job TEXT PRIMARY KEY, status TEXT NOT NULL)''')
    return con


def beat(con, job, ok=True, note='', now=None):
    now = now or dt.datetime.now()
    con.execute('INSERT INTO beats(job, ts, ok, note) VALUES(?,?,?,?)',
                (job, now.isoformat(' ', 'seconds'), 1 if ok else 0, note))
    con.commit()


def check(con, jobs, now=None, notify=None):
    """등록부 대조 → {작업: (상태, 상세)}. 상태 변화만 notify(메시지). 상태 = OK/LATE/FAIL/NEW."""
    now = now or dt.datetime.now()
    result = {}
    for j in jobs:
        name, period, grace = j['name'], j['period_min'], j.get('grace_min', 5)
        row = con.execute('SELECT ts, ok, note FROM beats WHERE job=? ORDER BY id DESC LIMIT 1',
                          (name,)).fetchone()
        if row is None:
            result[name] = ('NEW', '하트비트 0건 — 아직 한 번도 실행 보고가 없음')
            continue
        ts, ok, note = dt.datetime.fromisoformat(row[0]), bool(row[1]), row[2] or ''
        age_min = (now - ts).total_seconds() / 60
        if age_min > period + grace:
            result[name] = ('LATE', f'무소식 {age_min:.0f}분 (기대 {period}+유예 {grace}분) — 침묵 실패 의심')
        elif not ok:
            result[name] = ('FAIL', f'실패 보고: {note or "(사유 없음)"}')
        else:
            result[name] = ('OK', f'{age_min:.0f}분 전 성공')
    # ★상태 변화만 알림 (경보 피로 방지) — 회복(→OK)도 알림
    changes = []
    for name, (st, detail) in result.items():
        prev = con.execute('SELECT status FROM states WHERE job=?', (name,)).fetchone()
        prev = prev[0] if prev else None
        if prev != st:
            con.execute('INSERT OR REPLACE INTO states VALUES(?,?)', (name, st))
            if prev is not None or st != 'OK':          # 최초 관측이 OK면 조용히
                mark = '✅ 회복' if st == 'OK' else ('🔕' if st == 'NEW' else '🚨')
                changes.append(f'{mark} [{name}] {prev or "-"} → {st}: {detail}')
    con.commit()
    if notify:
        for msg in changes:
            notify(msg)
    return result, changes


def report_text(result):
    L = []
    for name, (st, detail) in sorted(result.items()):
        icon = {'OK': '·', 'LATE': '★', 'FAIL': '★', 'NEW': '?'}[st]
        L.append(f'  {icon} [{st:4s}] {name} — {detail}')
    return '\n'.join(L)


# ── 검증 데모 (가상 시계) ──────────────────────────────────────────
def main_demo():
    for p in (DB, DB + '-wal', DB + '-shm'):
        if os.path.exists(p):
            os.remove(p)
    jobs = [{'name': '수집기A', 'period_min': 60, 'grace_min': 10},
            {'name': '백업', 'period_min': 1440, 'grace_min': 60},
            {'name': '리포트발송', 'period_min': 60, 'grace_min': 10}]
    json.dump(jobs, open(JOBS, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    con = open_db()
    alerts = []
    notify = alerts.append
    t0 = dt.datetime(2026, 9, 3, 9, 0)

    # ① 정상: 3작업 모두 하트비트 → 전부 OK (최초 관측 OK = 알림 없음)
    for name in ('수집기A', '백업', '리포트발송'):
        beat(con, name, ok=True, now=t0)
    r1, ch1 = check(con, jobs, now=t0 + dt.timedelta(minutes=5), notify=notify)
    ok1 = (all(st == 'OK' for st, _ in r1.values()) and ch1 == [] and alerts == [])

    # ② 침묵 실패: 수집기A만 하트비트 끊고 80분 전진(60+10 초과) — 나머지는 계속 비트
    t1 = t0 + dt.timedelta(minutes=80)
    beat(con, '백업', ok=True, now=t1 - dt.timedelta(minutes=10))
    beat(con, '리포트발송', ok=True, now=t1 - dt.timedelta(minutes=10))
    r2, ch2 = check(con, jobs, now=t1, notify=notify)
    ok2 = (r2['수집기A'][0] == 'LATE' and r2['백업'][0] == 'OK' and r2['리포트발송'][0] == 'OK'
           and len(ch2) == 1 and '수집기A' in ch2[0] and 'LATE' in ch2[0])

    # ③ 실패 보고: 리포트발송이 beat(ok=False, note) → FAIL + 사유 보존
    beat(con, '리포트발송', ok=False, note='SMTP 연결 거부', now=t1 + dt.timedelta(minutes=1))
    r3, ch3 = check(con, jobs, now=t1 + dt.timedelta(minutes=2), notify=notify)
    ok3 = (r3['리포트발송'][0] == 'FAIL' and 'SMTP 연결 거부' in r3['리포트발송'][1]
           and len(ch3) == 1 and 'FAIL' in ch3[0])

    # ④ 경보 피로 방지: 같은 상태로 재검사 → 알림 0 / 수집기A 회복 → 회복 알림 1
    n_before = len(alerts)
    r4a, ch4a = check(con, jobs, now=t1 + dt.timedelta(minutes=3), notify=notify)
    same_silent = (ch4a == [] and len(alerts) == n_before)
    beat(con, '수집기A', ok=True, now=t1 + dt.timedelta(minutes=4))
    r4b, ch4b = check(con, jobs, now=t1 + dt.timedelta(minutes=5), notify=notify)
    ok4 = (same_silent and r4b['수집기A'][0] == 'OK'
           and len(ch4b) == 1 and '회복' in ch4b[0])

    # ⑤ NEW: 등록만 된 작업 추가 → NEW 표기(+알림)
    jobs2 = jobs + [{'name': '신규작업', 'period_min': 30, 'grace_min': 5}]
    r5, ch5 = check(con, jobs2, now=t1 + dt.timedelta(minutes=6), notify=notify)
    ok5 = (r5['신규작업'][0] == 'NEW' and any('신규작업' in c for c in ch5))

    # ⑥ 이력 유실 0(비트 기록 수 = 호출 수 7) + 재검사 재현성
    n_beats = con.execute('SELECT COUNT(*) FROM beats').fetchone()[0]
    r6a, _ = check(con, jobs2, now=t1 + dt.timedelta(minutes=7))
    r6b, _ = check(con, jobs2, now=t1 + dt.timedelta(minutes=7))
    ok6 = (n_beats == 7 and r6a == r6b)
    con.close()

    L = [f'# 작업 감시 검증 리포트 ({dt.datetime.now():%Y-%m-%d %H:%M})',
         '- 데모 = 작업 3종 등록 · **가상 시계 주입**(실대기 0·결정적) · 무소식/실패/회복/신규 전부 심음',
         '', '| 검증 | 결과 |', '|---|---|',
         f'| ① 정상 3작업 = 전부 OK (최초 OK는 조용) | {"PASS" if ok1 else "★FAIL"} |',
         f'| ② ★침묵 실패(80분 무소식 > 60+10) → 그 작업만 LATE | {"PASS" if ok2 else "★FAIL"} |',
         f'| ③ 실패 보고 → FAIL + 사유 보존("SMTP 연결 거부") | {"PASS" if ok3 else "★FAIL"} |',
         f'| ④ ★경보 피로 방지(같은 상태 재검사 = 알림 0 · 회복 = 회복 알림 1) | {"PASS" if ok4 else "★FAIL"} |',
         f'| ⑤ 미실행 등록 작업 = NEW 표기(묵살 금지) | {"PASS" if ok5 else "★FAIL"} |',
         f'| ⑥ 하트비트 이력 유실 0({n_beats}/7) + 재검사 재현성 | {"PASS" if ok6 else "★FAIL"} |',
         '', '## 최종 상태판', '```', report_text(r5), '```',
         '', '- ※ 알림 = 상태가 바뀔 때만(경보 피로 방지, 회복 포함). 채널 = 훅 교체식.',
         '- ※ 연동 = 각 작업 끝에 하트비트 1줄(`--beat 작업명 --ok`). 감시 자체는 크론/스케줄러 1개로 돈다.']
    rep = os.path.join(HERE, 'sentinel_verify.md')
    open(rep, 'w', encoding='utf-8').write('\n'.join(L))
    print('\n'.join(L))
    return ok1 and ok2 and ok3 and ok4 and ok5 and ok6


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    if '--beat' in sys.argv:       # 실사용: python job_sentinel.py --beat 작업명 [--fail] [--note 사유]
        name = sys.argv[sys.argv.index('--beat') + 1]
        note = sys.argv[sys.argv.index('--note') + 1] if '--note' in sys.argv else ''
        con = open_db()
        beat(con, name, ok='--fail' not in sys.argv, note=note)
        con.close()
        print(f'하트비트 기록: {name} ({"실패" if "--fail" in sys.argv else "성공"})')
        sys.exit(0)
    if '--check' in sys.argv:      # 실사용: python job_sentinel.py --check (크론 1개로 주기 실행)
        jobs = json.load(open(JOBS, encoding='utf-8'))
        con = open_db()
        result, changes = check(con, jobs, notify=lambda m: print('알림:', m))
        print(report_text(result))
        con.close()
        sys.exit(0 if all(st == 'OK' for st, _ in result.values()) else 1)
    ok = main_demo()
    sys.exit(0 if ok else 1)
