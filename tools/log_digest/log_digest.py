#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""log_digest.py — 로그 에러 요약기 (핑거프린트 그룹핑·신규 검출·급증 감지)

수만 줄 로그를 사람이 못 읽는 이유는 양이 아니라 **중복** — 같은 에러가 ID·숫자만 바뀌며 수천 번
반복된다. 이 도구는 가변부를 지운 핑거프린트로 그룹핑해 "수만 줄 → 한 화면"으로 만들고,
운영에서 진짜 중요한 두 신호를 표시한다:
  - ★NEW: 이전 실행 스냅샷에 없던 처음 보는 에러 — 배포 직후 새 에러를 그날 잡는다
  - ★SPIKE: 이전 대비 급증(기본 5배↑)한 그룹 — 조용히 굴러가던 에러의 폭발

구조:
  - 파싱: 라인 → (시각, 레벨, 메시지) — 로그 포맷 정규식 설정식
  - 핑거프린트: 숫자→{n} · 16진ID→{hex} · 따옴표 문자열→{s} 치환 → 템플릿 (규칙 확장 설정식)
  - 집계: 그룹별 건수·첫/마지막 발생·예시 원문 · ERROR만(레벨 필터 설정식)
  - 스냅샷 비교(core Watcher 계보): 신규·급증 판정 · 요약 md 리포트

검증(--make-demo) = 정답지 선작성: 에러 5그룹(변형 476건)+INFO 노이즈 2,000줄 합성 →
  ①그룹핑 정확(변형 수백=1그룹·총수 보존) ②가변부 일반화(템플릿에 구체값 0) ③노이즈 분리
  ④2차 로그에 ★신규 1그룹 심음 → 그것만 NEW ⑤★급증(3→60건) 심음 → 그것만 SPIKE(오탐 0)
  ⑥첫/마지막 발생 시각 수기 + 재현성.
"""
import os, sys, re, json, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
SNAP = os.path.join(HERE, 'digest_snapshot.json')
LINE_PAT = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+(\w+)\s+(.*)$')  # 포맷(설정식)
SPIKE_X = 5.0                                              # 급증 임계 배수(설정식)


def fingerprint(msg):
    """가변부 제거 → 템플릿. 순서 중요: hex/UUID 먼저(숫자 치환이 먼저면 hex가 깨짐)."""
    s = re.sub(r'\b[0-9a-f]{8,}\b', '{hex}', msg, flags=re.I)
    s = re.sub(r'"[^"]*"|\'[^\']*\'', '{s}', s)
    s = re.sub(r'\d+(?:\.\d+)*', '{n}', s)
    return re.sub(r'\s+', ' ', s).strip()


def digest(log_path, level='ERROR'):
    """→ {fp: dict(count, first, last, example)}"""
    groups = {}
    for line in open(log_path, encoding='utf-8'):
        m = LINE_PAT.match(line.rstrip('\n'))
        if not m or m.group(2) != level:
            continue
        ts, _, msg = m.groups()
        fp = fingerprint(msg)
        g = groups.setdefault(fp, dict(count=0, first=ts, last=ts, example=msg))
        g['count'] += 1
        g['last'] = ts
        if ts < g['first']:
            g['first'] = ts
    return groups


def compare(groups, snap_path=SNAP):
    """이전 스냅샷 대비 → (신규 fp set, 급증 fp set). 스냅샷 갱신."""
    prev = {}
    if os.path.exists(snap_path):
        try:
            prev = json.load(open(snap_path, encoding='utf-8'))
        except Exception:
            prev = {}
    new = {fp for fp in groups if fp not in prev}
    spike = {fp for fp, g in groups.items()
             if fp in prev and prev[fp] > 0 and g['count'] / prev[fp] >= SPIKE_X}
    json.dump({fp: g['count'] for fp, g in groups.items()},
              open(snap_path, 'w', encoding='utf-8'), ensure_ascii=False)
    return new, spike


def report_text(groups, new, spike, total_lines):
    n_err = sum(g['count'] for g in groups.values())
    L = [f'로그 {total_lines:,}줄 → 에러 {n_err:,}건 → **그룹 {len(groups)}개** (한 화면)',
         f'★NEW {len(new)} · ★SPIKE {len(spike)}', '']
    for fp, g in sorted(groups.items(), key=lambda x: -x[1]['count']):
        tags = ('🆕' if fp in new else '') + ('📈' if fp in spike else '')
        L.append(f"  {tags}[{g['count']:>5,}] {fp}")
        L.append(f"         첫 {g['first']} · 마지막 {g['last']} · 예: {g['example'][:70]}")
    return '\n'.join(L)


# ── 검증 데모 (정답지 선작성) ──────────────────────────────────────
def write_log(path, day, extra_new=False, g5_count=3):
    """정답지: G1 120 · G2 300 · G3 45 · G4 8 · G5 g5_count (+신규 12) + INFO 2,000"""
    lines = []
    T = lambda i: f'{day} {9 + (i % 12):02d}:{i % 60:02d}:{(i * 7) % 60:02d}'
    for i in range(120):
        lines.append(f'{T(i)} ERROR DB connection timeout host=10.0.0.{i % 250} retry={i % 5}')
    for i in range(300):
        lines.append(f'{T(i)} ERROR user {10000 + i} not found in session cache')
    for i in range(45):
        lines.append(f'{T(i)} ERROR payment failed order={7000 + i} code={i % 9}')
    for i in range(8):
        lines.append(f'{T(i)} ERROR disk usage {80 + i}% on /dev/sda{i % 3}')
    for i in range(g5_count):
        lines.append(f'{T(i)} ERROR unexpected token in config line {i + 1}')
    if extra_new:
        for i in range(12):
            lines.append(f'{T(i)} ERROR null pointer in cart module item={i}')
    for i in range(2000):
        lines.append(f'{T(i)} INFO request handled path=/api/v{i % 3}/list in {i % 90}ms')
    lines.sort()                                            # 시각순(첫/마지막 검증 의미 부여)
    open(path, 'w', encoding='utf-8').write('\n'.join(lines) + '\n')
    return len(lines)


def main_demo():
    if os.path.exists(SNAP):
        os.remove(SNAP)
    log1 = os.path.join(HERE, 'demo_day1.log')
    log2 = os.path.join(HERE, 'demo_day2.log')
    n1 = write_log(log1, '2026-09-03')
    n2 = write_log(log2, '2026-09-04', extra_new=True, g5_count=60)   # ★신규 1그룹 + G5 3→60(20배)

    # 1일차: 스냅샷 구축
    g1 = digest(log1)
    new1, spike1 = compare(g1)
    # ① 그룹핑: 정확히 5그룹, 건수 = 정답(120·300·45·8·3), 총수 보존 476
    counts1 = sorted(g['count'] for g in g1.values())
    ok1 = (len(g1) == 5 and counts1 == [3, 8, 45, 120, 300]
           and sum(counts1) == 476)
    # ② 가변부 일반화: 120 IP 변형 = 1그룹, 템플릿에 숫자 잔존 0 (dotted IP는 통째로 {n} — 1차
    #    검증 검거: 기대값을 '10.0.0.{n}'로 잘못 상정, 실제 계약 = "구체값 잔존 0"이 맞음)
    fp_db = next(fp for fp in g1 if 'DB connection' in fp)
    ok2 = (fp_db == 'DB connection timeout host={n} retry={n}'
           and g1[fp_db]['count'] == 120 and not re.search(r'\d', fp_db))
    # ③ 노이즈 분리: INFO 2,000줄 → 에러 집계 0 (총 에러 476 = 심은 에러수)
    ok3 = (sum(g['count'] for g in g1.values()) == n1 - 2000)
    # 2일차: 신규·급증
    g2 = digest(log2)
    new2, spike2 = compare(g2)
    # ④ 신규: 'null pointer' 1그룹만 NEW (기존 5그룹 아님)
    ok4 = (len(new2) == 1 and 'null pointer' in next(iter(new2)))
    # ⑤ 급증: G5(3→60, 20배)만 SPIKE — G2 등 동일 건수 그룹 오탐 0
    ok5 = (len(spike2) == 1 and 'unexpected token' in next(iter(spike2)))
    # ⑥ 첫/마지막 발생 수기(정렬된 로그의 min/max 시각) + 재현성
    fp_user = next(fp for fp in g1 if 'user {n} not found' in fp)
    all_ts = [l[:19] for l in open(log1, encoding='utf-8') if 'not found' in l]
    ok6 = (g1[fp_user]['first'] == min(all_ts) and g1[fp_user]['last'] == max(all_ts)
           and digest(log1) == g1)

    rep_txt = report_text(g2, new2, spike2, n2)
    L = [f'# 로그 요약 검증 리포트 ({dt.datetime.now():%Y-%m-%d %H:%M})',
         f'- 데모 = 정답지 선작성: 1일차 {n1:,}줄(에러 5그룹 476건+INFO 2,000) → 2일차 ★신규 1그룹·★급증(3→60) 심음',
         '', '| 검증 | 결과 |', '|---|---|',
         f'| ① 그룹핑 정확(5그룹 · 건수 [3,8,45,120,300] · 총수 보존) | {"PASS" if ok1 else "★FAIL"} |',
         f'| ② 가변부 일반화(IP 120변형 = 1그룹 · 템플릿에 구체값 0) | {"PASS" if ok2 else "★FAIL"} |',
         f'| ③ 노이즈 분리(INFO 2,000줄 → 집계 0) | {"PASS" if ok3 else "★FAIL"} |',
         f'| ④ ★신규 검출(심은 null pointer만 NEW, 기존 5그룹 오탐 0) | {"PASS" if ok4 else "★FAIL"} |',
         f'| ⑤ ★급증 감지(3→60건 20배만 SPIKE, 오탐 0) | {"PASS" if ok5 else "★FAIL"} |',
         f'| ⑥ 첫/마지막 발생 시각 수기 + 재현성 | {"PASS" if ok6 else "★FAIL"} |',
         '', '## 2일차 요약 실물(도구 출력 그대로)', '```', rep_txt, '```',
         '', '- ※ 로그 포맷 정규식·레벨 필터·핑거프린트 규칙·급증 임계 = 설정값(고객 로그에 1회 맞춤).']
    rep = os.path.join(HERE, 'log_digest_verify.md')
    open(rep, 'w', encoding='utf-8').write('\n'.join(L))
    print('\n'.join(L[:14]))
    print(rep_txt[:600])
    return ok1 and ok2 and ok3 and ok4 and ok5 and ok6


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]) and not sys.argv[1].endswith('.py'):
        # 실사용: python log_digest.py 로그파일 → 요약 출력(+스냅샷 비교)
        g = digest(sys.argv[1])
        new, spike = compare(g)
        n_lines = sum(1 for _ in open(sys.argv[1], encoding='utf-8'))
        print(report_text(g, new, spike, n_lines))
        sys.exit(1 if (new or spike) else 0)
    ok = main_demo()
    sys.exit(0 if ok else 1)
