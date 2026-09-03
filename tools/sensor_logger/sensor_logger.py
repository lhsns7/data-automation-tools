#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sensor_logger.py — 측정값 스트림 수집 → SQLite 저장 + 무결성 감시 (2026-09)

센서·장비·로그처럼 주기적으로 들어오는 측정값을 **SQLite에 유실 없이 저장**하고,
**결측 구간·범위 이상값·정체(stuck, 센서 고착 의심)**를 자동 감지해 리포트한다.

정직 설계(어댑터 분리):
  - 데모 = SimSource(가상 센서 2채널, **결함을 알고 심음**). 실물 연결 = 파일 테일/시리얼 어댑터(장비 필요 명시).
  - 이상값도 **저장은 하고 플래그만**(묵살 금지 — 원본 보존).
  - 재실행 안전: (채널, ts) PK로 중복 0.

검증(--make-demo): 결함을 **의도적으로 심고**(결측 3구간·스파이크 5·정체 1) 감시기가
  정확히 그 개수·위치만 잡는지 대조 + 저장 유실 0 + 재적재 중복 0 + 재현성.
"""
import os, sys, sqlite3, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, 'sensor.db')

INTERVAL = 1.0          # 기대 수신 주기(초) — 설정값
GAP_TOL = 1.5           # 이 배수 넘는 간격 = 결측 구간
STUCK_N = 30            # 연속 동일값 이 횟수 이상 = 정체 의심
LIMITS = {'temp': (-10.0, 120.0), 'dist': (0.0, 2000.0)}   # 채널별 정상 범위


# ── 저장 ────────────────────────────────────────────────────────────
def open_db(path=DB):
    con = sqlite3.connect(path)
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('''CREATE TABLE IF NOT EXISTS samples(
        channel TEXT NOT NULL, ts REAL NOT NULL, value REAL NOT NULL,
        PRIMARY KEY(channel, ts))''')
    return con


def ingest(con, samples):
    """[(channel, ts, value)] 저장. 반환 = 신규 저장 수(중복은 PK로 무시)."""
    before = con.execute('SELECT COUNT(*) FROM samples').fetchone()[0]
    con.executemany('INSERT OR IGNORE INTO samples VALUES(?,?,?)', samples)
    con.commit()
    return con.execute('SELECT COUNT(*) FROM samples').fetchone()[0] - before


# ── 감시 ────────────────────────────────────────────────────────────
def analyze(con, interval=INTERVAL, gap_tol=GAP_TOL, stuck_n=STUCK_N, limits=LIMITS):
    out = {}
    for (ch,) in con.execute('SELECT DISTINCT channel FROM samples'):
        rows = con.execute('SELECT ts, value FROM samples WHERE channel=? ORDER BY ts', (ch,)).fetchall()
        gaps, outliers, stuck = [], [], []
        lo, hi = limits.get(ch, (float('-inf'), float('inf')))
        run_val, run_start, run_len = None, None, 0
        prev = None
        for ts, v in rows:
            if prev is not None and ts - prev > interval * gap_tol:
                gaps.append((prev, ts, round(ts - prev, 1)))
            prev = ts
            if not (lo <= v <= hi):
                outliers.append((ts, v))
            if v == run_val:
                run_len += 1
            else:
                if run_len >= stuck_n:
                    stuck.append((run_start, run_len, run_val))
                run_val, run_start, run_len = v, ts, 1
        if run_len >= stuck_n:
            stuck.append((run_start, run_len, run_val))
        out[ch] = {'n': len(rows), 'gaps': gaps, 'outliers': outliers, 'stuck': stuck}
    return out


def report_text(res):
    L = []
    for ch, r in sorted(res.items()):
        L.append(f"[{ch}] 수신 {r['n']:,} · 결측 {len(r['gaps'])}구간 · 이상값 {len(r['outliers'])} · 정체 {len(r['stuck'])}")
        for a, b, sec in r['gaps']:
            L.append(f"  · 결측 {sec}초: ts {a:.0f} → {b:.0f}")
        for ts, v in r['outliers'][:10]:
            L.append(f"  · 이상값 ts {ts:.0f} = {v} (범위 밖, 저장·플래그)")
        for ts, n, v in r['stuck']:
            L.append(f"  · 정체 ts {ts:.0f}부터 {n}회 연속 {v} (센서 고착 의심)")
    return '\n'.join(L)


# ── 데모: 결함을 알고 심는 가상 센서 ────────────────────────────────
def sim_samples(t0=1_700_000_000.0, n=600):
    """2채널 1초 주기 n샘플. ★심는 결함(위치 고정): temp 결측 2구간+스파이크 3, dist 결측 1구간+스파이크 2+정체 1."""
    import math
    planted = {'gaps': {'temp': [(120, 150), (400, 420)], 'dist': [(250, 280)]},
               'spikes': {'temp': [60, 200, 333], 'dist': [90, 510]},
               'stuck': {'dist': (350, 40)}}          # ts 350부터 40회 동일값
    samples = []
    for i in range(n):
        ts = t0 + i
        # temp
        if not any(a <= i < b for a, b in planted['gaps']['temp']):
            v = round(25 + 3 * math.sin(i / 30), 2)
            if i in planted['spikes']['temp']:
                v = 500.0                              # 범위(120) 밖
            samples.append(('temp', ts, v))
        # dist
        if not any(a <= i < b for a, b in planted['gaps']['dist']):
            s0, sn = planted['stuck']['dist']
            if s0 <= i < s0 + sn:
                v = 777.0                              # 정체(동일값 지속)
            else:
                v = round(1000 + 200 * math.sin(i / 45), 1)
                if i in planted['spikes']['dist']:
                    v = -50.0                          # 범위(0) 밖
            samples.append(('dist', ts, v))
    return samples, planted


def main_demo():
    if os.path.exists(DB):
        os.remove(DB)
    con = open_db()
    samples, planted = sim_samples()
    stored = ingest(con, samples)
    dup = ingest(con, samples)                          # 재적재 → 중복 0 기대
    res = analyze(con)
    res2 = analyze(con)
    same = (res == res2)

    # 심은 결함 vs 감지 대조 (개수 + 위치)
    g_t = res['temp']['gaps']; g_d = res['dist']['gaps']
    gaps_ok = (len(g_t) == 2 and len(g_d) == 1
               and abs(g_t[0][0] - (1_700_000_000 + 119)) < 2 and abs(g_d[0][0] - (1_700_000_000 + 249)) < 2)
    out_ok = (len(res['temp']['outliers']) == 3 and len(res['dist']['outliers']) == 2)
    stuck_ok = (len(res['dist']['stuck']) == 1 and res['dist']['stuck'][0][1] == 40
                and len(res['temp']['stuck']) == 0)
    loss_ok = (stored == len(samples) and dup == 0)

    now = dt.datetime.now()
    L = [f'# 센서 로거 검증 리포트 ({now:%Y-%m-%d %H:%M})',
         f'- 데모: 가상 2채널 × 600초, **결함을 알고 심음**(결측 3구간 · 범위밖 5 · 정체 1) → 감시기가 그것만 정확히 잡는지',
         '', '| 검증 | 결과 |', '|---|---|',
         f'| ① 저장 유실 0 | 발생 {len(samples)} = 저장 {stored} → {"PASS" if loss_ok else "★FAIL"} |',
         f'| ② 재적재 중복 0 (PK) | 재적재 신규 {dup}건 → {"PASS" if dup == 0 else "★FAIL"} |',
         f'| ③ 결측 구간 검출(심은 3) | temp {len(g_t)} + dist {len(g_d)} = {len(g_t)+len(g_d)}, 위치 일치 → {"PASS" if gaps_ok else "★FAIL"} |',
         f'| ④ 범위 이상값 검출(심은 5) | temp {len(res["temp"]["outliers"])} + dist {len(res["dist"]["outliers"])} → {"PASS" if out_ok else "★FAIL"} (저장은 유지·플래그만) |',
         f'| ⑤ 정체 검출(심은 1: 40회 연속) | dist {len(res["dist"]["stuck"])}건({res["dist"]["stuck"][0][1] if res["dist"]["stuck"] else 0}회) · temp 오탐 {len(res["temp"]["stuck"])} → {"PASS" if stuck_ok else "★FAIL"} |',
         f'| ⑥ 재현성(2회 동일) | {"OK" if same else "★불일치"} |',
         '', '## 감시 리포트 출력 예', '```', report_text(res), '```',
         '', '- ※ 데모 = 가상 센서(어댑터 분리). 실물 연결 = 장비의 파일/시리얼 출력에 어댑터 1회 맞춤(하드웨어 필요).']
    rep = os.path.join(HERE, 'sensor_verify.md')
    open(rep, 'w', encoding='utf-8').write('\n'.join(L))
    print('\n'.join(L[:16]))
    print(f'\n저장: {rep}')
    con.close()
    return loss_ok and dup == 0 and gaps_ok and out_ok and stuck_ok and same


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    ok = main_demo()
    sys.exit(0 if ok else 1)
