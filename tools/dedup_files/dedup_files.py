#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dedup_files.py — 중복 파일 탐지·안전 정리 (해시 기준·격리·복구 왕복)

공유폴더·다운로드 폴더에 같은 파일이 이름만 다르게 쌓인다. 정리 도구의 진짜 문제는 탐지가 아니라
**삭제의 공포** — 그래서 이 도구는 지우지 않는다:

  - 탐지: 크기 사전 필터 → SHA-256 내용 해시 그룹핑(이름 무관) — 1바이트만 달라도 중복 아님
  - 보존 규칙(명시·설정식): 그룹당 1개는 반드시 남김 — ①보호 폴더 안 사본 우선 ②없으면 가장
    오래된 파일(원본 추정)
  - ★기본 = 검수 모드: 정리 계획만 리포트, 파일은 손대지 않음. 실제 정리는 --apply 명시 필요
  - ★--apply = 삭제가 아니라 **격리 폴더 이동**(원경로 대장 기록) → --restore 로 전부 원위치 복구
  - 빈 파일은 중복 처리하지 않음(보고만) — 빈 파일 무더기를 "중복"으로 지우는 사고 방지

검증(--make-demo): ①심은 중복 3그룹 정확 탐지 + **1바이트 차이 파일 오탐 0** ②빈 파일 제외
  ③검수 모드 = 파일시스템 무변화 ④보존 규칙(보호 폴더 우선·최고(最古) 보존) ⑤격리 이동(삭제 0·
  대장 기록) ⑥★복구 왕복(--restore → 초기 상태 해시 전수 일치) ⑦격리 후 재스캔 = 중복 0.
"""
import os, sys, json, shutil, hashlib, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
QUAR_NAME = '_격리(중복정리)'
LEDGER = 'quarantine_ledger.json'


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def scan_dupes(root, protect_dirs=()):
    """→ (groups, empties). groups = [dict(hash, keep, extras[, size])] — keep 결정 포함."""
    quar_root = os.path.join(root, QUAR_NAME)
    by_size = {}
    for r, ds, fs in os.walk(root):
        if os.path.abspath(r).startswith(os.path.abspath(quar_root)):
            continue                                        # 격리 폴더는 스캔 제외
        for fn in fs:
            p = os.path.join(r, fn)
            by_size.setdefault(os.path.getsize(p), []).append(p)
    groups, empties = [], []
    for size, paths in sorted(by_size.items()):
        if size == 0:
            empties += paths                                # 빈 파일 = 중복 처리 안 함(보고만)
            continue
        if len(paths) < 2:
            continue
        by_hash = {}
        for p in paths:
            by_hash.setdefault(sha256(p), []).append(p)
        for hh, ps in by_hash.items():
            if len(ps) < 2:
                continue
            protected = [p for p in ps if any(
                os.path.abspath(p).startswith(os.path.abspath(d)) for d in protect_dirs)]
            if protected:
                keep = sorted(protected)[0]                 # ①보호 폴더 사본 우선 보존
            else:
                keep = min(ps, key=os.path.getmtime)        # ②가장 오래된 것 = 원본 추정
            extras = sorted(p for p in ps if p != keep)
            groups.append(dict(hash=hh, size=size, keep=keep, extras=extras))
    return groups, sorted(empties)


def apply_quarantine(root, groups):
    """extras → 격리 폴더 이동(삭제 0) + 대장(원경로) 기록. 반환 이동 수."""
    quar = os.path.join(root, QUAR_NAME)
    os.makedirs(quar, exist_ok=True)
    ledger_path = os.path.join(quar, LEDGER)
    ledger = json.load(open(ledger_path, encoding='utf-8')) if os.path.exists(ledger_path) else {}
    moved = 0
    for g in groups:
        for p in g['extras']:
            qname = f'{len(ledger):04d}_{os.path.basename(p)}'
            shutil.move(p, os.path.join(quar, qname))
            ledger[qname] = dict(orig=os.path.relpath(p, root), hash=g['hash'],
                                 at=dt.datetime.now().isoformat(' ', 'seconds'))
            moved += 1
    json.dump(ledger, open(ledger_path, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    return moved


def restore_all(root):
    """격리 전부 원위치 복구(대장 기준). 반환 복구 수."""
    quar = os.path.join(root, QUAR_NAME)
    ledger_path = os.path.join(quar, LEDGER)
    if not os.path.exists(ledger_path):
        return 0
    ledger = json.load(open(ledger_path, encoding='utf-8'))
    n = 0
    for qname, meta in list(ledger.items()):
        src = os.path.join(quar, qname)
        dst = os.path.join(root, meta['orig'])
        if os.path.exists(src):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(src, dst)
            del ledger[qname]
            n += 1
    json.dump(ledger, open(ledger_path, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    return n


def report_text(groups, empties):
    waste = sum(g['size'] * len(g['extras']) for g in groups)
    L = [f'중복 그룹 {len(groups)}개 · 정리 가능 {sum(len(g["extras"]) for g in groups)}개'
         f' · 낭비 용량 {waste:,}바이트 · 빈 파일 {len(empties)}개(중복 처리 안 함)']
    for g in groups:
        L.append(f'  ● 보존: {g["keep"]}  ({g["size"]:,}B)')
        L += [f'     - 정리 대상: {p}' for p in g['extras']]
    return '\n'.join(L)


# ── 검증 데모 ───────────────────────────────────────────────────────
def tree_state(root):
    """트리 전체 상태 = {상대경로: 해시} (격리 폴더 제외) — 복구 왕복 대조용"""
    out = {}
    quar = os.path.abspath(os.path.join(root, QUAR_NAME))
    for r, ds, fs in os.walk(root):
        if os.path.abspath(r).startswith(quar):
            continue
        for fn in fs:
            p = os.path.join(r, fn)
            out[os.path.relpath(p, root)] = sha256(p) if os.path.getsize(p) else ''
    return out


def make_demo(root):
    if os.path.isdir(root):
        shutil.rmtree(root)
    W = lambda rel, content: (os.makedirs(os.path.dirname(os.path.join(root, rel)), exist_ok=True)
                              if os.path.dirname(rel) else None,
                              open(os.path.join(root, rel), 'w', encoding='utf-8').write(content))
    # 그룹A(3벌): 최고본 = 문서/원본_보고서.md (mtime 가장 과거로 심음)
    A = '분기 보고서 내용 ' * 200
    W('문서/원본_보고서.md', A)
    W('다운로드/보고서 (1).md', A)
    W('다운로드/보고서-복사본.md', A)
    # 그룹B(2벌)
    B = '계약 조건 정리 ' * 150
    W('문서/계약정리.txt', B)
    W('백업재료/계약정리_옛날.txt', B)
    # 그룹C(4벌, 사본 하나가 보호 폴더 '보관소' 안)
    C = 'X' * 5000 + '핵심 데이터'
    W('보관소/마스터.dat', C)
    W('작업/사본1.dat', C)
    W('작업/사본2.dat', C)
    W('다운로드/사본3.dat', C)
    # ★1바이트 차이(오탐 검증): 그룹A와 거의 같지만 다른 내용
    W('문서/보고서_수정본.md', A[:-1] + '!')
    # 빈 파일 2개(같은 해시 — 중복 처리 금지)
    W('메모/빈파일1.txt', '')
    W('메모/빈파일2.txt', '')
    # 유일 파일들
    for i in range(5):
        W(f'기타/자료_{i}.txt', f'유일한 내용 {i} ' * 50)
    # mtime 심기: 그룹A 원본을 가장 과거로 / 그룹B는 백업재료 쪽이 원본(더 과거)
    t = dt.datetime(2026, 9, 1, 9, 0).timestamp()
    os.utime(os.path.join(root, '문서/원본_보고서.md'), (t - 86400 * 30,) * 2)
    os.utime(os.path.join(root, '백업재료/계약정리_옛날.txt'), (t - 86400 * 60,) * 2)
    planted = {
        'A': dict(keep='문서/원본_보고서.md', extras={'다운로드/보고서 (1).md', '다운로드/보고서-복사본.md'}),
        'B': dict(keep='백업재료/계약정리_옛날.txt', extras={'문서/계약정리.txt'}),
        'C': dict(keep='보관소/마스터.dat', extras={'작업/사본1.dat', '작업/사본2.dat', '다운로드/사본3.dat'}),
    }
    return planted


def main_demo():
    root = os.path.join(HERE, 'demo_tree')
    planted = make_demo(root)
    protect = [os.path.join(root, '보관소')]
    state0 = tree_state(root)

    # ①② 탐지: 그룹 3·구성 정확·1바이트 차이 오탐 0·빈 파일 제외
    groups, empties = scan_dupes(root, protect)
    got = {}
    for g in groups:
        got[os.path.relpath(g['keep'], root).replace(os.sep, '/')] = \
            {os.path.relpath(p, root).replace(os.sep, '/') for p in g['extras']}
    want = {v['keep']: v['extras'] for v in planted.values()}
    ok1 = (len(groups) == 3 and got == want
           and not any('보고서_수정본' in str(g) for g in groups))
    ok2 = (len(empties) == 2)

    # ③ 검수 모드(기본) = 무변화
    ok3 = (tree_state(root) == state0)

    # ④ 보존 규칙: C=보호 폴더 우선 · A/B=최고(最古) 보존 (①의 want에 내장 — 명시 재확인)
    ok4 = ('보관소/마스터.dat' in got and '백업재료/계약정리_옛날.txt' in got
           and '문서/원본_보고서.md' in got)

    # ⑤ --apply = 격리 이동(삭제 0): 6개 이동 · 보존본 잔존 · 대장 기록
    moved = apply_quarantine(root, groups)
    quar = os.path.join(root, QUAR_NAME)
    ledger = json.load(open(os.path.join(quar, LEDGER), encoding='utf-8'))
    keeps_alive = all(os.path.exists(os.path.join(root, k)) for k in want)
    extras_gone = all(not os.path.exists(os.path.join(root, e)) for es in want.values() for e in es)
    n_quar_files = sum(1 for f in os.listdir(quar) if f != LEDGER)
    ok5 = (moved == 6 and n_quar_files == 6 and len(ledger) == 6
           and keeps_alive and extras_gone)

    # ⑦ 격리 후 재스캔 = 중복 0 (⑥ 복구 전에 측정)
    groups2, _ = scan_dupes(root, protect)
    ok7 = (len(groups2) == 0)

    # ⑥ ★복구 왕복: --restore → 초기 상태 해시 전수 일치
    restored = restore_all(root)
    ok6 = (restored == 6 and tree_state(root) == state0)

    L = [f'# 중복 파일 정리 검증 리포트 ({dt.datetime.now():%Y-%m-%d %H:%M})',
         '- 데모 = 파일 17개 트리 — 중복 3그룹(3+2+4벌)·★1바이트 차이 파일·빈 파일 2·보호 폴더 심음',
         '', '| 검증 | 결과 |', '|---|---|',
         f'| ① 탐지 정확(3그룹 구성원 전수) + ★1바이트 차이 오탐 0 | {"PASS" if ok1 else "★FAIL"} |',
         f'| ② 빈 파일 2개 = 중복 처리 안 함(보고만) | {"PASS" if ok2 else "★FAIL"} |',
         f'| ③ 검수 모드(기본) = 파일시스템 무변화(해시 전수) | {"PASS" if ok3 else "★FAIL"} |',
         f'| ④ 보존 규칙(보호 폴더 우선 · 최고본 보존) | {"PASS" if ok4 else "★FAIL"} |',
         f'| ⑤ --apply = 삭제 0·격리 이동 {moved}/6 · 대장 기록 · 보존본 전부 잔존 | {"PASS" if ok5 else "★FAIL"} |',
         f'| ⑥ ★복구 왕복(--restore → 초기 해시 전수 일치) | {"PASS" if ok6 else "★FAIL"} |',
         f'| ⑦ 격리 후 재스캔 = 중복 0 | {"PASS" if ok7 else "★FAIL"} |',
         '', '- ※ 계약: **지우지 않는다** — 기본 검수 모드 · 적용 = 격리 이동(대장) · --restore 왕복 복구 증명.',
         '- ※ 보존 규칙·보호 폴더 = 설정값. 빈 파일은 중복 정리 대상에서 제외(보고만).']
    rep = os.path.join(HERE, 'dedup_verify.md')
    open(rep, 'w', encoding='utf-8').write('\n'.join(L))
    print('\n'.join(L))
    return ok1 and ok2 and ok3 and ok4 and ok5 and ok6 and ok7


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    args = sys.argv[1:]
    if args and os.path.isdir(args[0]):
        # 실사용: python dedup_files.py 대상폴더 [--protect 폴더] [--apply | --restore]
        root = args[0]
        protect = [args[args.index('--protect') + 1]] if '--protect' in args else []
        if '--restore' in args:
            print(f'복구: {restore_all(root)}개 원위치')
            sys.exit(0)
        groups, empties = scan_dupes(root, protect)
        print(report_text(groups, empties))
        if '--apply' in args:
            print(f'격리 이동: {apply_quarantine(root, groups)}개 → {os.path.join(root, QUAR_NAME)}')
        else:
            print('\n(검수 모드 — 파일은 손대지 않았습니다. 실제 정리는 --apply, 복구는 --restore)')
        sys.exit(0)
    ok = main_demo()
    sys.exit(0 if ok else 1)
