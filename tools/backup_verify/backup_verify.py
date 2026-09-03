#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backup_verify.py — 복원까지 증명하는 폴더 백업 (해시 대장·복원 검증·세대 관리)

폴더를 zip 아카이브로 백업하고, **백업 직후 실제로 복원해 해시 대장과 전수 대조**한다.
백업의 유일한 진실 = "복원되는가" — 복원 검증 없는 백업은
디스크를 차지하는 희망사항일 뿐이다.

기능:
  - 백업: 소스 폴더 → 타임스탬프 zip + **해시 대장**(파일별 경로·크기·SHA-256, JSON)
  - ★복원 검증: 백업 직후 임시 폴더에 풀어 대장과 전수 대조(해시·누락·여분) — 통과해야 "백업 완료"
  - 변경 리포트: 직전 대장과 비교 — 추가/수정/삭제 목록
  - ★대량 변경 경보: 한 번에 파일 50%+가 바뀌면 경보(랜섬웨어·오조작 신호 — 백업이 오염본으로
    덮이기 전에 사람을 부른다)
  - 세대 관리: 최신 N개 보관, 오래된 아카이브 자동 정리
  - 재검(--check): 보관 중인 아카이브를 나중에 다시 복원 대조 — 보관 부패 검출

검증(--make-demo): ①백업→복원 전수 대조(한글 파일명·서브폴더·빈/큰 파일 포함)
  ②아카이브 1바이트 조작 → 재검이 부패 검출 ③심은 변경(수정3·추가1·삭제1) 정확 리포트
  ④무변경 재백업 = 변경 0 ⑤심은 대량 변경(60%) → 경보 발동 ⑥세대 3개 정책 → 오래된 것만 정리.
※ 데모 = 로컬 폴더. 실서비스 = 대상 폴더·보관 위치(NAS·클라우드 동기화 폴더 등)·주기(스케줄러) 설정.
"""
import os, sys, json, shutil, zipfile, hashlib, tempfile, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
ALERT_RATIO = 0.5          # 이 비율 이상 변경 = 대량 변경 경보(설정값)


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def scan(src):
    """소스 폴더 → {상대경로: (size, sha256)} (경로 구분자 '/' 통일)"""
    out = {}
    for root, _, files in os.walk(src):
        for fn in files:
            p = os.path.join(root, fn)
            rel = os.path.relpath(p, src).replace(os.sep, '/')
            out[rel] = (os.path.getsize(p), sha256(p))
    return out


def diff_manifest(old, new):
    """이전/현재 대장 비교 → (추가, 수정, 삭제, 경보 여부)"""
    added = sorted(set(new) - set(old))
    deleted = sorted(set(old) - set(new))
    modified = sorted(k for k in set(old) & set(new) if old[k][1] != new[k][1])
    base = max(len(old), 1)
    alert = (len(modified) + len(deleted)) / base >= ALERT_RATIO
    return added, modified, deleted, alert


def restore_verify(archive, manifest):
    """아카이브를 임시 폴더에 실제 복원 → 대장과 전수 대조.
    반환 (일치수, 불일치목록, 누락목록, 여분목록, 부패여부)."""
    tmp = tempfile.mkdtemp(prefix='restore_')
    try:
        try:
            with zipfile.ZipFile(archive) as z:
                z.extractall(tmp)
        except Exception:
            return 0, [], [], [], True                    # 풀리지도 않음 = 부패
        got = scan(tmp)
        bad = [k for k in set(manifest) & set(got) if got[k][1] != manifest[k][1]]
        missing = sorted(set(manifest) - set(got))
        extra = sorted(set(got) - set(manifest))
        okn = sum(1 for k in set(manifest) & set(got) if got[k][1] == manifest[k][1])
        return okn, sorted(bad), missing, extra, bool(bad or missing)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def prune(dest, keep):
    """오래된 세대 정리(최신 keep개 보관). 아카이브+대장 쌍으로 삭제."""
    zips = sorted(f for f in os.listdir(dest) if f.startswith('backup_') and f.endswith('.zip'))
    removed = []
    for old in zips[:-keep] if keep > 0 else []:
        os.remove(os.path.join(dest, old))
        mf = old[:-4] + '.manifest.json'
        if os.path.exists(os.path.join(dest, mf)):
            os.remove(os.path.join(dest, mf))
        removed.append(old)
    return removed


def backup(src, dest, keep=3, stamp=None):
    """백업 1회 실행 → dict(archive, manifest_path, n, added, modified, deleted, alert,
    restore_ok, restore_n). ★복원 검증 통과 전에는 '완료'라 부르지 않는다."""
    os.makedirs(dest, exist_ok=True)
    stamp = stamp or dt.datetime.now().strftime('%Y%m%d_%H%M%S')
    archive = os.path.join(dest, f'backup_{stamp}.zip')
    mani_path = archive[:-4] + '.manifest.json'
    manifest = scan(src)

    # 직전 대장과 비교(변경 리포트 + 대량 변경 경보)
    prevs = sorted(f for f in os.listdir(dest) if f.endswith('.manifest.json') and f != os.path.basename(mani_path))
    added, modified, deleted, alert = [], [], [], False
    if prevs:
        old = {k: tuple(v) for k, v in json.load(
            open(os.path.join(dest, prevs[-1]), encoding='utf-8')).items()}
        added, modified, deleted, alert = diff_manifest(old, manifest)

    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
        for rel in sorted(manifest):
            z.write(os.path.join(src, rel.replace('/', os.sep)), rel)
    json.dump(manifest, open(mani_path, 'w', encoding='utf-8'), ensure_ascii=False, indent=0)

    okn, bad, missing, extra, corrupted = restore_verify(archive, manifest)
    pruned = prune(dest, keep)
    return dict(archive=archive, manifest_path=mani_path, n=len(manifest),
                added=added, modified=modified, deleted=deleted, alert=alert,
                restore_ok=(not corrupted and not extra and okn == len(manifest)),
                restore_n=okn, pruned=pruned)


# ── 검증 데모 ───────────────────────────────────────────────────────
def _w(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if isinstance(content, bytes):
        open(path, 'wb').write(content)
    else:
        open(path, 'w', encoding='utf-8').write(content)


def make_demo_tree(src):
    if os.path.isdir(src):
        shutil.rmtree(src)
    import random
    rng = random.Random(20260903)
    for i in range(10):
        _w(os.path.join(src, f'문서/보고서_{i:02d}.md'), f'# 주간 보고 {i}\n내용 {i*7}\n' * 20)
    for i in range(5):
        _w(os.path.join(src, f'data/시트_{i}.csv'), '날짜,금액\n' + '\n'.join(
            f'2026-08-{d+1:02d},{(d+1)*1000+i}' for d in range(30)))
    _w(os.path.join(src, '설정/config 백업.txt'), 'mode=운영\nlimit=100\n')
    _w(os.path.join(src, '빈파일.txt'), '')
    _w(os.path.join(src, 'bin/모델.bin'), bytes(rng.randrange(256) for _ in range(1 << 20)))  # 1MB
    _w(os.path.join(src, '메모.txt'), '한글 파일명·경로 시험\n')
    _w(os.path.join(src, 'data/원장.csv'), '계정,금액\n매출,1000\n')
    return scan(src)


def main_demo():
    src = os.path.join(HERE, 'demo_src')
    dest = os.path.join(HERE, 'demo_backups')
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    base = make_demo_tree(src)
    n0 = len(base)                                         # 20개 파일

    # ① 백업 1 → 복원 전수 대조
    b1 = backup(src, dest, keep=3, stamp='20260903_000001')
    ok1 = (b1['restore_ok'] and b1['restore_n'] == n0 == b1['n'])

    # ② 아카이브 1바이트 조작 → 재검이 부패 검출
    corrupt = os.path.join(dest, 'corrupt_copy.zip')
    shutil.copy(b1['archive'], corrupt)
    with open(corrupt, 'r+b') as f:
        f.seek(os.path.getsize(corrupt) // 2)
        byte = f.read(1)
        f.seek(-1, 1)
        f.write(bytes([byte[0] ^ 0xFF]))
    mani1 = {k: tuple(v) for k, v in json.load(open(b1['manifest_path'], encoding='utf-8')).items()}
    _, bad2, miss2, _, corrupted2 = restore_verify(corrupt, mani1)
    ok2 = corrupted2
    os.remove(corrupt)

    # ③ 심은 변경(수정 3·추가 1·삭제 1) → 백업 2가 정확히 리포트 (5/20 = 25% → 경보 없음)
    MOD = ['문서/보고서_02.md', 'data/시트_1.csv', '설정/config 백업.txt']
    for m in MOD:
        p = os.path.join(src, m.replace('/', os.sep))
        open(p, 'a', encoding='utf-8').write('\n[수정됨]\n')
    _w(os.path.join(src, '문서/신규_공지.md'), '새 파일\n')
    os.remove(os.path.join(src, '메모.txt'))
    b2 = backup(src, dest, keep=3, stamp='20260903_000002')
    ok3 = (b2['modified'] == sorted(MOD) and b2['added'] == ['문서/신규_공지.md']
           and b2['deleted'] == ['메모.txt'] and not b2['alert'] and b2['restore_ok'])

    # ④ 무변경 재백업 → 변경 0
    b3 = backup(src, dest, keep=3, stamp='20260903_000003')
    ok4 = (not b3['added'] and not b3['modified'] and not b3['deleted']
           and not b3['alert'] and b3['restore_ok'])

    # ⑤ 심은 대량 변경(60%) → 경보 발동
    cur = scan(src)
    targets = sorted(cur)[:int(len(cur) * 0.6)]
    for t in targets:
        p = os.path.join(src, t.replace('/', os.sep))
        _w(p, '암호화된 척 하는 내용 XXXX\n')
    b4 = backup(src, dest, keep=3, stamp='20260903_000004')
    ok5 = (b4['alert'] and len(b4['modified']) == len(targets) and b4['restore_ok'])

    # ⑥ 세대 3개 정책: 4회 백업 후 최신 3개만 남고 가장 오래된 것 정리
    zips = sorted(f for f in os.listdir(dest) if f.endswith('.zip'))
    ok6 = (zips == ['backup_20260903_000002.zip', 'backup_20260903_000003.zip',
                    'backup_20260903_000004.zip'] and b4['pruned'] == ['backup_20260903_000001.zip'])

    L = [f'# 백업 검증 리포트 ({dt.datetime.now():%Y-%m-%d %H:%M})',
         f'- 데모 = {n0}개 파일(한글 파일명·서브폴더·빈 파일·1MB 바이너리) · 백업 4세대 · 결함/변경 심기',
         '', '| 검증 | 결과 |', '|---|---|',
         f'| ① 백업→★실제 복원 전수 대조(해시) | {b1["restore_n"]}/{n0} 일치 → {"PASS" if ok1 else "★FAIL"} |',
         f'| ② 아카이브 1바이트 조작 → 재검(--check) 부패 검출 | {"PASS" if ok2 else "★FAIL"} |',
         f'| ③ 심은 변경(수정3·추가1·삭제1) 정확 리포트 | {"PASS" if ok3 else "★FAIL"} |',
         f'| ④ 무변경 재백업 = 변경 0 | {"PASS" if ok4 else "★FAIL"} |',
         f'| ⑤ ★심은 대량 변경(60%) → 경보 발동(랜섬웨어·오조작 신호) | {"PASS" if ok5 else "★FAIL"} |',
         f'| ⑥ 세대 3개 정책(가장 오래된 것만 정리) | {"PASS" if ok6 else "★FAIL"} |',
         '', '- ※ 계약: **복원 검증을 통과하기 전에는 "백업 완료"라 부르지 않는다.**',
         '- ※ 데모 = 로컬 폴더. 실서비스 = 대상·보관 위치(NAS/클라우드 동기화 폴더)·주기(스케줄러)·경보 채널 설정.']
    rep = os.path.join(HERE, 'backup_verify_report.md')
    open(rep, 'w', encoding='utf-8').write('\n'.join(L))
    print('\n'.join(L))
    return ok1 and ok2 and ok3 and ok4 and ok5 and ok6


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    if '--check' in sys.argv:      # 보관 아카이브 재검: --check 아카이브.zip 대장.json
        arc = sys.argv[sys.argv.index('--check') + 1]
        mani = {k: tuple(v) for k, v in json.load(
            open(sys.argv[sys.argv.index('--check') + 2], encoding='utf-8')).items()}
        okn, bad, missing, extra, corrupted = restore_verify(arc, mani)
        print(f'복원 대조: 일치 {okn}/{len(mani)} · 훼손 {len(bad)} · 누락 {len(missing)} · 여분 {len(extra)}'
              f' → {"★부패/불일치" if corrupted or extra else "정상(복원 가능 증명)"}')
        sys.exit(1 if corrupted or extra else 0)
    ok = main_demo()
    sys.exit(0 if ok else 1)
