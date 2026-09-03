#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""macro_filler — 표(CSV/엑셀)의 각 행을 데스크톱 프로그램에 반복 입력하는 매크로 (2026-08)

여기서는 검증 가능한 대상으로 **메모장**을 씁니다. 실제 주문에서는 이 자리에 고객사 프로그램 창이 들어가고,
창 찾기·입력·저장·확인 절차는 그대로 재사용합니다.

  python macro_filler.py            # config.txt 대로 실행
  python macro_filler.py --dry      # 화면을 건드리지 않고 계획만 출력 (검수용)

안전장치: 시작 전 카운트다운 · 마우스를 화면 왼쪽 위로 밀면 즉시 중단 · 실패 시 그 순간 화면 캡처 저장.
"""
import os, sys, csv, time, subprocess, datetime as dt

BASE = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, 'frozen', False) else __file__))
for p in (os.path.join(BASE, '..', '..', 'core'), os.path.join(BASE, '_lib')):
    if os.path.isdir(p): sys.path.insert(0, p)
for _s in (sys.stdout, sys.stderr):
    try: _s.reconfigure(encoding='utf-8', errors='replace')
    except Exception: pass

from xlsx import write_workbook


class _Log:  # 경량 로거(파일+콘솔)
    def __init__(self, path, name):
        self.path, self.name = path, name
    def _w(self, lv, m):
        line = f'{lv} [{self.name}] {m}'
        print(' ', line)
        try:
            import datetime as _dt
            open(self.path, 'a', encoding='utf-8').write(f'{_dt.datetime.now():%Y-%m-%d %H:%M:%S} {line}\n')
        except Exception:
            pass
    def info(self, m): self._w('INFO', m)
    def warn(self, m): self._w('WARN', m)
    def error(self, m): self._w('ERROR', m)

CONFIG = os.path.join(BASE, 'config.txt')
LOG = os.path.join(BASE, 'macro_filler.log')
L = _Log(LOG, 'macro')

DEFAULT_CONFIG = """# 반복 입력 매크로 설정 - 메모장으로 열어 값만 바꾸고 저장하세요.

# 입력할 목록 파일 (이 폴더 기준). 첫 줄은 제목 줄이어야 합니다.
목록파일 = 입력목록.csv

# 결과를 저장할 폴더 (비우면 이 폴더 안의 결과 폴더)
저장폴더 =

# 한 건을 입력할 때 쓸 서식. {열이름} 자리에 그 행의 값이 들어갑니다.
서식 = [{일자}] {회사명} / {담당자} / {내용}

# 파일 이름 서식 (확장자 제외)
파일명서식 = {일자}_{회사명}

# 한 건 처리 후 쉬는 시간(초). 느린 PC나 무거운 프로그램이면 늘리세요.
간격 = 0.6

# 시작 전 준비 시간(초)
카운트다운 = 5
"""

SAMPLE = [['일자', '회사명', '담당자', '내용'],
          ['2026-08-29', '가나상사', '김민수', '견적서 발송 요청'],
          ['2026-08-29', '다라전자', '이서연', '납기 일정 확인'],
          ['2026-08-30', '마바물산', '박지훈', '계약서 검토 회신']]


def make_list(path, n):
    """검증용 목록 생성. ★평범한 행만 넣으면 검증이 안 된다 — 파일명 금지문자·긴 문자열·빈값·영문숫자·특수기호를 섞는다."""
    import random
    random.seed(20260829)
    앞 = ['가나', '다라', '마바', '사아', '자차', '카타', '파하', '한빛', '새롬', '너울']
    뒤 = ['상사', '전자', '물산', '테크', '시스템즈', '엔지니어링', '솔루션', '産業']
    이름 = ['김민수', '이서연', '박지훈', '최유진', '정하늘', 'John Smith', '王小明', '오']
    내용 = ['견적서 발송 요청', '납기 일정 확인', '계약서 검토 회신', '세금계산서 발행 요청',
            '샘플 배송 문의', 'A/B 테스트 결과 공유', '단가 재협상 (2026년 2분기 기준) — 회신 요망']
    rows = [['일자', '회사명', '담당자', '내용']]
    for i in range(n):
        d = (dt.date(2026, 8, 1) + dt.timedelta(days=i % 28)).isoformat()
        c = random.choice(앞) + random.choice(뒤)
        # 경계값 주입
        if i % 17 == 0:  c = c + '/' + random.choice(뒤)                      # 파일명 금지문자
        if i % 23 == 0:  c = c + ' ' + '주식회사' * 6                          # 아주 긴 이름
        if i % 29 == 0:  c = c + ' <특수:문자*?>'                              # 금지문자 모둠
        nm = random.choice(이름) if i % 13 else ''                             # 빈값
        tx = random.choice(내용)
        if i % 19 == 0:  tx = tx + '  줄바꿈없는긴내용 ' + 'x' * 60
        rows.append([d, c, nm, tx])
    with open(path, 'w', encoding='utf-8-sig', newline='') as f:
        csv.writer(f).writerows(rows)
    return len(rows) - 1


def load_config():
    if not os.path.exists(CONFIG):
        open(CONFIG, 'w', encoding='utf-8').write(DEFAULT_CONFIG)
        L.info('config.txt 를 새로 만들었습니다.')
    cfg = {}
    for line in open(CONFIG, encoding='utf-8'):
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1); cfg[k.strip()] = v.strip()
    return cfg


def load_rows(path):
    if not os.path.exists(path):
        with open(path, 'w', encoding='utf-8-sig', newline='') as f:
            csv.writer(f).writerows(SAMPLE)
        L.info(f'예시 목록 파일을 만들었습니다: {path}')
    with open(path, encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def safe_name(s):
    for ch in '\\/:*?"<>|':
        s = s.replace(ch, '_')
    return s.strip()[:80]


def main(dry=False):
    cfg = load_config()
    for a in sys.argv:
        if a.startswith('--make-list'):
            n = int(a.split('=')[1]) if '=' in a else 100
            path = os.path.join(BASE, cfg.get('목록파일') or '입력목록.csv')
            made = make_list(path, n)
            print(f'\n  검증용 목록 {made}건을 만들었습니다: {path}')
            print('  (파일명 금지문자·아주 긴 이름·빈 담당자·영문/한자 이름·긴 내용이 섞여 있습니다)')
            return 0
    rows = load_rows(os.path.join(BASE, cfg.get('목록파일') or '입력목록.csv'))
    outdir = cfg.get('저장폴더') or os.path.join(BASE, '결과')
    os.makedirs(outdir, exist_ok=True)
    tmpl = cfg.get('서식') or '{내용}'
    fname = cfg.get('파일명서식') or '{일자}'
    gap = float(cfg.get('간격') or 0.6)
    cnt = int(cfg.get('카운트다운') or 5)

    print(f'\n  목록 {len(rows)}건 / 저장 위치 {outdir}')
    plan = []
    for i, r in enumerate(rows, 1):
        try:
            text = tmpl.format(**r); nm = safe_name(fname.format(**r))
        except KeyError as e:
            raise SystemExit(f'  [설정 오류] 서식에 쓴 이름 {e} 이 목록 파일의 제목 줄에 없습니다.\n'
                             f'  목록 파일의 열 이름: {list(rows[0].keys())}')
        plan.append((i, nm, text, os.path.join(outdir, nm + '.txt')))

    if dry:
        print('\n  [검수 모드] 화면을 건드리지 않고 계획만 보여줍니다.\n')
        for i, nm, text, path in plan:
            print(f'  {i:>3}. {nm}.txt  <-  {text[:60]}')
        print(f'\n  총 {len(plan)}건. 실제 실행은 --dry 없이 다시 실행하세요.')
        return 0

    import gui
    M = gui.Macro(L, shot_dir=os.path.join(BASE, '_shots'))
    M.countdown(cnt)

    def one(nm, text, path):
        """★2026-08-29 100건 검증에서 잡은 사고를 반영한 순서.
        문제였던 것: 포커스를 못 잡은 채 입력이 진행되고, 닫기 단축키(Alt+F4)가 남의 창으로 가서 사용자의 콘솔이 닫혔다.
        고친 것: ①활성화를 확인될 때까지만 진행 ②입력 직전 포커스 재확인 ③저장을 확인한 뒤에 닫는다
                 ④닫기는 단축키가 아니라 프로세스 종료로 — 키가 빗나갈 여지를 없앤다."""
        open(path, 'w', encoding='utf-8').close()          # 빈 파일 먼저 → 저장 대화상자 없이 Ctrl+S로 끝남
        key = nm[:24]                                       # 창 제목은 길면 잘리므로 앞부분으로 대조
        proc = subprocess.Popen(['notepad.exe', path])
        try:
            M.wait_window(key, timeout=15)                  # 활성화까지 확인됨(안 되면 여기서 실패)
            time.sleep(0.2)
            M.type_ko(text, focus=key)                      # 한글은 클립보드 경유 + 입력 직전 포커스 재확인
            M.hotkey('ctrl', 's', focus=key)
            M.expect_content(path, text, timeout=8)         # ★닫기 전에 '내용까지' 대조한다 (파일 존재만 보면 오염·덮어쓰기를 놓친다)
        finally:
            try: proc.terminate()                           # 단축키 대신 프로세스 종료
            except Exception: pass
            time.sleep(0.2)

    ok = fail = skip = 0
    times = []
    t0 = time.time()
    for i, nm, text, path in plan:
        # ★재개: 이미 같은 내용으로 처리된 건은 건너뛴다 → 중간에 끊겨도 이어서 실행 가능
        if os.path.exists(path):
            try:
                if open(path, encoding='utf-8').read().strip() == text.strip():
                    skip += 1; print(f'  {i:>3}/{len(plan)}  {nm[:24]}  건너뜀(이미 완료)'); continue
            except Exception: pass
        t1 = time.time()
        try:
            one(nm, text, path); ok += 1; times.append(time.time() - t1)
            print(f'  {i:>3}/{len(plan)}  {nm[:24]}  완료 ({time.time()-t1:.1f}s)')
        except Exception as e:
            L.warn(f'{nm} 1차 실패 {type(e).__name__}: {str(e)[:70]} — 재시도')
            time.sleep(1.0)
            try:
                one(nm, text, path); ok += 1; times.append(time.time() - t1)
                print(f'  {i:>3}/{len(plan)}  {nm[:24]}  완료(재시도)')
            except Exception as e2:
                fail += 1
                L.error(f'{nm} 실패 {type(e2).__name__}: {str(e2)[:90]}')
                print(f'  {i:>3}/{len(plan)}  {nm[:24]}  실패 ({type(e2).__name__})')
        time.sleep(gap)

    def verdict(p, text):
        """★리포트의 '성공'은 파일 존재가 아니라 내용 일치로 판정한다."""
        if not os.path.exists(p) or os.path.getsize(p) == 0: return '실패'
        try:
            return '성공' if open(p, encoding='utf-8').read().strip() == text.strip() else '내용 불일치'
        except Exception:
            return '읽기 실패'

    sec = time.time() - t0
    stamp = dt.datetime.now()
    report = os.path.join(outdir, f'처리결과_{stamp:%Y%m%d_%H%M}.xlsx')
    write_workbook(report,
                   {'처리내역': (['번호', '파일명', '입력한 내용', '결과', '파일경로'],
                                [[i, nm, text, verdict(p, text), p] for i, nm, text, p in plan])},
                   summary={'실행 일시': stamp.strftime('%Y-%m-%d %H:%M:%S'),
                            '대상 프로그램': '메모장 (실제 주문에서는 고객사 프로그램)',
                            '처리 건수': f'{len(plan)}건 (신규 {ok} / 실패 {fail} / 건너뜀 {skip})',
                            '성공률': f'{100*ok/max(ok+fail,1):.1f}%',
                            '내용 전수 대조': f"{sum(1 for i,nm,t,p in plan if verdict(p,t)=='성공')}/{len(plan)} 일치",
                            '소요 시간': f'{sec:.1f}초',
                            '건당 시간': (f'최소 {min(times):.1f}s / 중앙 {sorted(times)[len(times)//2]:.1f}s / 최대 {max(times):.1f}s' if times else '-'),
                            '사람이 했다면': f'약 {len(plan)*30/60:.0f}분 (건당 30초 가정)',
                            '화면 해상도': f'{gui.SCREEN.width}x{gui.SCREEN.height}',
                            '비고': '실패 건은 _shots 폴더의 화면 캡처로 원인을 확인할 수 있습니다.'})
    L.info(f'완료 성공 {ok} 실패 {fail} / {sec:.1f}초 · 리포트 {report}')
    print(f'\n  완료. 성공 {ok}건 / 실패 {fail}건 / {sec:.1f}초')
    print(f'  처리 결과: {report}')
    return 0 if fail == 0 else 1


if __name__ == '__main__':
    print('=' * 62)
    print('  반복 입력 매크로   (설정 파일: config.txt)')
    print('=' * 62)
    code = gui_guard = 0
    try:
        import gui as _g
        code = _g.guard(main)('--dry' in sys.argv)
    except ImportError:
        code = main('--dry' in sys.argv)
    except Exception as ex:
        L.error(f'중단 {type(ex).__name__}: {ex}')
        print(f'\n  [오류] {type(ex).__name__}: {ex}')
        code = 1
    if getattr(sys, 'frozen', False) or os.environ.get('MACRO_PAUSE'):
        input('\n  창을 닫으려면 Enter 를 누르세요...')
    sys.exit(code)
