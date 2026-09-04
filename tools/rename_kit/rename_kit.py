#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rename_kit.py — 파일명 일괄 정리 (규칙 파이프·충돌 검출·왕복 롤백)

"IMG_3847.jpg 2천 개" 같은 폴더를 규칙으로 정리한다. 일괄 리네임의 공포 = 잘못 바꾸면 못 돌린다
— 그래서 이 도구의 본체는 리네임이 아니라 **안전**이다:

  - ★검수 모드 기본: 실행하면 변경 계획표만 출력, 파일은 손대지 않음(--apply 명시 필요)
  - ★충돌 검출: 두 파일이 같은 새 이름이 되면 **적용 자체를 거부**(하나라도 겹치면 전체 중단 —
    반쯤 바뀐 폴더가 최악)
  - ★왕복 롤백: 적용 시 대장(원래 이름) 기록 → --undo 한 번에 전부 원위치, 내용 해시로 무결 증명
규칙 파이프(설정식·순서 적용): ①날짜 프리픽스(mtime) ②정규식 캡처→템플릿 ③특수문자 정리
  ④공백→_ ⑤확장자 소문자 ⑥일련번호

검증(--make-demo): ①계획 = 정답 매핑 전수 ②검수 모드 무변화 ③★충돌 심음(2파일→같은 이름)
  = 적용 거부·무변화 ④적용 후 이름 정확+내용 해시 불변 ⑤--undo 왕복(이름·해시 초기와 전수 일치)
  ⑥멱등(정리된 폴더 재실행 = 변경 0).
"""
import os, sys, re, json, hashlib, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
LEDGER_NAME = '_rename_ledger.json'

RULES = dict(                                              # 규칙 파이프(설정값)
    date_prefix=True,                                      # mtime → YYYYMMDD_
    pattern=(r'^IMG[_-]?(\d+)', r'사진_\1'),                # 정규식 캡처 → 템플릿
    clean_chars=True,                                      # 특수문자 → 제거, 공백 → _
    lower_ext=True,
)


def new_name(path, rules=RULES):
    d = os.path.dirname(path)
    base = os.path.basename(path)
    stem, ext = os.path.splitext(base)
    if rules.get('pattern'):
        stem = re.sub(rules['pattern'][0], rules['pattern'][1], stem)
    if rules.get('clean_chars'):
        stem = re.sub(r'[#@!$%^&~]+', '', stem)            # 괄호는 보존(한글 파일명 관용) — 계약 명시
        stem = re.sub(r'\s+', '_', stem.strip())
    if rules.get('lower_ext'):
        ext = ext.lower()
    if rules.get('date_prefix'):
        day = dt.datetime.fromtimestamp(os.path.getmtime(path)).strftime('%Y%m%d')
        if not stem.startswith(day + '_'):                 # 멱등: 이미 붙었으면 다시 안 붙임
            stem = f'{day}_{stem}'
    return os.path.join(d, stem + ext)


def plan(folder, rules=RULES):
    """→ (변경 목록 [(old, new)], 충돌 목록). 충돌 = 서로 다른 원본이 같은 새 이름."""
    changes, targets = [], {}
    for fn in sorted(os.listdir(folder)):
        p = os.path.join(folder, fn)
        if not os.path.isfile(p) or fn == LEDGER_NAME:
            continue
        np_ = new_name(p, rules)
        if np_ != p:
            changes.append((p, np_))
        targets.setdefault(os.path.basename(np_), []).append(fn)
    conflicts = {t: srcs for t, srcs in targets.items() if len(srcs) > 1}
    return changes, conflicts


def apply_plan(folder, changes):
    """적용 + 대장 기록. ★호출 전 충돌 0 확인 필수(도구 CLI는 자동으로 거부)."""
    ledger_path = os.path.join(folder, LEDGER_NAME)
    ledger = json.load(open(ledger_path, encoding='utf-8')) if os.path.exists(ledger_path) else {}
    for old, new in changes:
        os.rename(old, new)
        ledger[os.path.basename(new)] = os.path.basename(old)
    json.dump(ledger, open(ledger_path, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    return len(changes)


def undo(folder):
    """대장 기준 전부 원래 이름으로 복구."""
    ledger_path = os.path.join(folder, LEDGER_NAME)
    if not os.path.exists(ledger_path):
        return 0
    ledger = json.load(open(ledger_path, encoding='utf-8'))
    n = 0
    for cur, orig in list(ledger.items()):
        p = os.path.join(folder, cur)
        if os.path.exists(p):
            os.rename(p, os.path.join(folder, orig))
            del ledger[cur]
            n += 1
    json.dump(ledger, open(ledger_path, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    return n


# ── 검증 데모 ───────────────────────────────────────────────────────
def tree_state(folder):
    out = {}
    for fn in sorted(os.listdir(folder)):
        p = os.path.join(folder, fn)
        if os.path.isfile(p) and fn != LEDGER_NAME:
            out[fn] = hashlib.sha256(open(p, 'rb').read()).hexdigest()
    return out


def make_demo(conflict=False):
    import shutil
    # ★1차 검거: 충돌 데모가 같은 폴더를 rmtree로 재생성 → 본 시나리오(④~⑥)가 충돌 세트 위에서
    #   돌았음. 폴더 분리로 수리.
    folder = os.path.join(HERE, 'demo_files_conflict' if conflict else 'demo_files')
    if os.path.isdir(folder):
        shutil.rmtree(folder)
    os.makedirs(folder)
    W = lambda fn, content: open(os.path.join(folder, fn), 'w', encoding='utf-8').write(content)
    W('IMG_3847.JPG', 'photo-a')
    W('IMG_3848.JPG', 'photo-b')
    W('회의록 (최종) #2.TXT', 'meeting notes')
    W('report v1.PDF', 'report body')
    W('데이터정리.csv', 'a,b,c')
    if conflict:                                           # ★충돌: 정리 후 같은 이름이 되는 쌍
        W('IMG-3847.jpg', 'photo-a-duplicate-name')        # IMG_3847과 함께 '사진_3847'로 수렴
    t = dt.datetime(2026, 9, 4, 10, 0).timestamp()
    for fn in os.listdir(folder):
        os.utime(os.path.join(folder, fn), (t, t))
    return folder


def main_demo():
    # ①② 계획 = 정답 매핑 · 검수 모드 무변화
    folder = make_demo()
    state0 = tree_state(folder)
    changes, conflicts = plan(folder)
    got = {os.path.basename(o): os.path.basename(n) for o, n in changes}
    want = {'IMG_3847.JPG': '20260904_사진_3847.jpg',
            'IMG_3848.JPG': '20260904_사진_3848.jpg',
            '회의록 (최종) #2.TXT': '20260904_회의록_(최종)_2.txt',
            'report v1.PDF': '20260904_report_v1.pdf',
            '데이터정리.csv': '20260904_데이터정리.csv'}
    ok1 = (got == want and not conflicts)
    ok2 = (tree_state(folder) == state0)                   # plan만으론 무변화

    # ③ ★충돌: 정리 후 같은 이름이 되는 쌍 심음 → 적용 거부(계획 단계 검출)·무변화
    folder_c = make_demo(conflict=True)
    state_c0 = tree_state(folder_c)
    _, conflicts_c = plan(folder_c)
    refused = bool(conflicts_c)                            # CLI는 충돌 시 apply 자체를 안 부름
    ok3 = (refused and len(conflicts_c) == 1 and tree_state(folder_c) == state_c0)

    # ④ 적용: 이름 정확 + 내용 해시 불변
    n_applied = apply_plan(folder, changes)
    state1 = tree_state(folder)
    ok4 = (n_applied == 5 and set(state1) == set(want.values())
           and sorted(state1.values()) == sorted(state0.values()))   # 내용 해시 집합 보존

    # ⑤ --undo 왕복: 이름·해시 초기와 전수 일치
    n_undone = undo(folder)
    ok5 = (n_undone == 5 and tree_state(folder) == state0)

    # ⑥ 멱등: 적용→재계획 = 변경 0(날짜 프리픽스 중복 부착 없음)
    changes2, _ = plan(folder)
    apply_plan(folder, changes2)
    changes3, _ = plan(folder)
    ok6 = (changes3 == [])

    L = [f'# 파일명 정리 검증 리포트 ({dt.datetime.now():%Y-%m-%d %H:%M})',
         '- 데모 = 지저분한 파일 5종(+★충돌 쌍 별도) — 규칙 파이프(날짜·패턴·특수문자·확장자)',
         '', '| 검증 | 결과 |', '|---|---|',
         f'| ① 변경 계획 = 정답 매핑 전수(5/5) | {"PASS" if ok1 else "★FAIL"} |',
         f'| ② 검수 모드(기본) = 파일 무변화 | {"PASS" if ok2 else "★FAIL"} |',
         f'| ③ ★충돌 검출(2파일→같은 이름) = 적용 거부·무변화 | {"PASS" if ok3 else "★FAIL"} |',
         f'| ④ 적용: 이름 정확 + 내용 해시 집합 보존 | {"PASS" if ok4 else "★FAIL"} |',
         f'| ⑤ ★--undo 왕복(이름·해시 초기 상태 전수 일치) | {"PASS" if ok5 else "★FAIL"} |',
         f'| ⑥ 멱등(재실행 = 변경 0 · 날짜 중복 부착 없음) | {"PASS" if ok6 else "★FAIL"} |',
         '', '- ※ 계약: 검수 모드 기본 · 충돌 시 전체 거부(반쯤 바뀐 폴더 방지) · 대장 기반 왕복 복구.',
         '- ※ 규칙 파이프 = 설정값(정규식 템플릿·날짜·문자 정리 조합).']
    rep = os.path.join(HERE, 'rename_verify.md')
    open(rep, 'w', encoding='utf-8').write('\n'.join(L))
    print('\n'.join(L))
    return ok1 and ok2 and ok3 and ok4 and ok5 and ok6


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    args = sys.argv[1:]
    if args and os.path.isdir(args[0]):
        folder = args[0]
        if '--undo' in args:
            print(f'복구: {undo(folder)}개 원래 이름으로')
            sys.exit(0)
        changes, conflicts = plan(folder)
        for o, n in changes:
            print(f'  {os.path.basename(o)}  →  {os.path.basename(n)}')
        if conflicts:
            print(f'🚫 충돌 {len(conflicts)}건 — 적용 거부(규칙을 조정하세요):')
            for t, srcs in conflicts.items():
                print(f'   {t}  ←  {", ".join(srcs)}')
            sys.exit(2)
        if '--apply' in args:
            print(f'적용: {apply_plan(folder, changes)}개 (복구는 --undo)')
        else:
            print(f'(검수 모드 — {len(changes)}개 변경 예정. 실제 적용은 --apply)')
        sys.exit(0)
    ok = main_demo()
    sys.exit(0 if ok else 1)
