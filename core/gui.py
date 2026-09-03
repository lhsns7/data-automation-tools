#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gui.py — 데스크톱 자동화(매크로) 골격 (2026-08)

대부분의 매크로는 **좌표를 박아둔다** — 해상도·배율·창 위치가 바뀌면 조용히 엉뚱한 곳을 누른다.
이 모듈의 원칙:
  ① 좌표보다 **이미지·창 기준**. 좌표는 최후수단이며 쓸 때 기준 해상도를 함께 기록한다.
  ② **찾을 때까지 기다린다**(timeout) — sleep 고정값 금지. 느린 PC에서 깨지는 1순위 원인.
  ③ 실패하면 **화면을 캡처해 증거를 남긴다**. "왜 안 됐는지 모름"을 없앤다.
  ④ **한글은 클립보드로 입력**한다 — pyautogui.write()는 한글을 못 친다(빈칸/깨짐).
  ⑤ 비상정지: 마우스를 화면 좌상단으로 밀면 즉시 중단(FAILSAFE) + ESC 감시.
  ⑥ 시작 전 카운트다운 — 사람이 손을 뗄 시간을 준다.
"""
import os, sys, time, datetime as dt

import pyautogui, pyperclip
pyautogui.FAILSAFE = True      # 좌상단(0,0)으로 마우스 이동 시 예외 발생 → 비상정지
pyautogui.PAUSE = 0.12

SCREEN = pyautogui.size()


class MacroError(RuntimeError):
    pass


class Macro:
    """매크로 실행 문맥. 로그·스크린샷·안전장치를 한 곳에서 관리."""

    def __init__(self, log, shot_dir=None, confidence=0.85):
        self.L = log
        self.shot_dir = shot_dir or os.path.join(os.path.dirname(getattr(log, 'path', '.')) or '.', '_shots')
        self.confidence = confidence
        self.steps = 0

    # ---------- 준비 ----------
    def countdown(self, sec=5, msg='자동화를 시작합니다. 마우스와 키보드에서 손을 떼 주세요.'):
        print(f'\n  {msg}')
        print(f'  중단하려면 마우스를 화면 왼쪽 위 끝으로 밀어 주세요.\n')
        for i in range(sec, 0, -1):
            print(f'  {i}...', end='', flush=True); time.sleep(1)
        print('  시작\n')
        self.L.info(f'매크로 시작 (해상도 {SCREEN.width}x{SCREEN.height})')

    def shot(self, tag='fail'):
        os.makedirs(self.shot_dir, exist_ok=True)
        p = os.path.join(self.shot_dir, f'{dt.datetime.now():%Y%m%d_%H%M%S}_{tag}.png')
        try:
            pyautogui.screenshot().save(p); self.L.warn(f'화면 캡처 저장: {p}')
        except Exception as e:
            self.L.error(f'화면 캡처 실패 {type(e).__name__}')
            p = None
        return p

    # ---------- 찾기 ----------
    def find(self, image, timeout=10, region=None, required=True):
        """화면에서 이미지가 나타날 때까지 기다렸다 위치를 준다. 고정 sleep 대신 이걸 쓴다."""
        end = time.time() + timeout
        last = None
        while time.time() < end:
            try:
                box = pyautogui.locateOnScreen(image, confidence=self.confidence, region=region)
                if box:
                    return pyautogui.center(box)
            except Exception as e:
                last = e
            time.sleep(0.35)
        if required:
            self.shot(f'notfound_{os.path.basename(str(image)).split(".")[0]}')
            raise MacroError(f'화면에서 찾지 못함: {image} ({timeout}초 대기){" / " + type(last).__name__ if last else ""}')
        return None

    def click_image(self, image, timeout=10, clicks=1, offset=(0, 0)):
        pt = self.find(image, timeout)
        pyautogui.click(pt.x + offset[0], pt.y + offset[1], clicks=clicks)
        self.steps += 1
        self.L.info(f'클릭 {os.path.basename(str(image))} @({pt.x},{pt.y})')
        return pt

    # ---------- 창 ----------
    def active_title(self):
        import pygetwindow as gw
        try:
            w = gw.getActiveWindow()
            return (w.title or '') if w else ''
        except Exception:
            return ''

    def wait_window(self, title_contains, timeout=15, activate=True):
        """창을 찾고, ★활성화가 실제로 됐는지 확인한다.
        2026-08-29 검거: activate() 실패를 조용히 넘기면 포커스가 다른 창에 있는 채로 입력이 진행된다.
        그 상태에서 Ctrl+V·Ctrl+S는 물론 Alt+F4까지 남의 창으로 가서 **사용자 작업 창이 닫혔다.**
        그래서 활성 창 제목을 대조해 확인될 때까지만 진행하고, 안 되면 실패로 끝낸다."""
        import pygetwindow as gw
        end = time.time() + timeout
        w = None
        while time.time() < end:
            wins = [x for x in gw.getAllWindows() if title_contains in (x.title or '')]
            if wins:
                w = wins[0]; break
            time.sleep(0.3)
        if w is None:
            self.shot('nowindow')
            raise MacroError(f'창을 찾지 못함: 제목에 "{title_contains}" 포함 ({timeout}초 대기)')
        if not activate:
            return w
        for _ in range(4):
            try:
                if w.isMinimized: w.restore()
                w.activate()
            except Exception:
                try: w.minimize(); w.restore()      # activate가 막힐 때의 우회
                except Exception: pass
            time.sleep(0.35)
            if title_contains in self.active_title():
                self.L.info(f'창 활성화 확인: {(w.title or "")[:40]}')
                return w
        self.shot('noactivate')
        raise MacroError(f'창을 찾았지만 활성화되지 않음: "{title_contains}" (현재 활성 창: "{self.active_title()[:40]}")')

    def assert_focus(self, title_contains):
        """입력 직전 안전 확인 — 포커스가 어긋났으면 아무 키도 보내지 않고 즉시 멈춘다."""
        cur = self.active_title()
        if title_contains not in cur:
            self.shot('focuslost')
            raise MacroError(f'입력 직전 포커스가 벗어남 (기대 "{title_contains}", 현재 "{cur[:40]}") — 입력을 보내지 않고 중단')
        return True

    # ---------- 입력 ----------
    def type_ko(self, text, interval=0.0, focus=None):
        """★한글 입력은 클립보드 경유. pyautogui.write()는 한글을 입력하지 못한다(빈칸으로 나감)."""
        if focus: self.assert_focus(focus)
        old = None
        try:
            old = pyperclip.paste()
        except Exception:
            pass
        pyperclip.copy(text); time.sleep(0.12)
        pyautogui.hotkey('ctrl', 'v'); time.sleep(0.15 + interval)
        self.steps += 1
        if old is not None:
            try: pyperclip.copy(old)     # 사용자의 원래 클립보드를 돌려놓는다
            except Exception: pass

    def type_en(self, text, interval=0.01):
        pyautogui.write(text, interval=interval); self.steps += 1

    DANGEROUS = {('alt', 'f4'), ('ctrl', 'w'), ('ctrl', 'q'), ('alt', 'F4')}

    def hotkey(self, *keys, focus=None):
        """★창을 닫는 단축키는 포커스가 어긋나면 남의 창을 닫는다(2026-08-29 실제 발생).
        닫기 계열은 focus= 로 대상 창을 명시해 확인한 뒤에만 보낸다. 가능하면 프로세스 종료를 쓸 것."""
        k = tuple(str(x).lower() for x in keys)
        if k in {('alt', 'f4'), ('ctrl', 'w'), ('ctrl', 'q')}:
            if focus is None:
                raise MacroError(f'닫기 단축키 {k}는 focus= 로 대상 창을 지정해야 합니다(오작동 시 남의 창이 닫힘)')
            self.assert_focus(focus)
        elif focus is not None:
            self.assert_focus(focus)
        pyautogui.hotkey(*keys); self.steps += 1; time.sleep(0.15)

    def press(self, key, times=1):
        for _ in range(times):
            pyautogui.press(key); time.sleep(0.08)
        self.steps += 1

    # ---------- 검증 ----------
    def expect_content(self, path, expected, timeout=10):
        """★저장된 내용이 기대와 '정확히 같은지' 확인한다 (2026-08-29 전수 대조에서 검거).
        파일이 생겼는지만 보면 두 가지를 놓친다:
          ① 자동화 중 사용자가 친 글자가 섞여 들어간 경우 (앞에 'qhao' 가 붙어 저장됨)
          ② 다른 행이 같은 파일명으로 덮어쓴 경우 (조용한 데이터 유실)
        무작위 표본 검사로는 둘 다 놓쳤고, 전수 대조에서만 잡혔다."""
        end = time.time() + timeout
        got = None
        while time.time() < end:
            if os.path.exists(path) and os.path.getsize(path) > 0:
                try:
                    got = open(path, encoding='utf-8').read().strip()
                    if got == expected.strip():
                        self.L.info(f'내용 확인: {os.path.basename(path)} ({len(got)}자)')
                        return True
                except Exception:
                    pass
            time.sleep(0.3)
        self.shot('mismatch')
        if got is None:
            raise MacroError(f'결과 파일이 생기지 않음: {path} ({timeout}초 대기)')
        raise MacroError('저장된 내용이 기대와 다름: ' + os.path.basename(path)
                         + ' | 기대: ' + expected.strip()[:60] + ' | 실제: ' + got[:60])

    def expect_file(self, path, timeout=10, min_bytes=1):
        """결과 파일이 실제로 생겼는지 확인 — 매크로가 '눌렀다'와 '됐다'는 다르다."""
        end = time.time() + timeout
        while time.time() < end:
            if os.path.exists(path) and os.path.getsize(path) >= min_bytes:
                self.L.info(f'결과 확인: {path} ({os.path.getsize(path)}바이트)')
                return True
            time.sleep(0.3)
        self.shot('nofile')
        raise MacroError(f'결과 파일이 생기지 않음: {path} ({timeout}초 대기)')


def guard(fn):
    """비상정지·예외를 사람이 읽을 수 있는 메시지로 바꾸는 래퍼."""
    def wrap(*a, **kw):
        try:
            return fn(*a, **kw)
        except pyautogui.FailSafeException:
            print('\n  [중단] 비상정지 - 마우스가 화면 왼쪽 위로 이동했습니다.')
            return 130
        except MacroError as e:
            print(f'\n  [실패] {e}')
            print('  같은 폴더의 _shots 안에 그 순간 화면이 저장되어 있습니다.')
            return 1
        except KeyboardInterrupt:
            print('\n  [중단] 사용자가 중지했습니다.')
            return 130
    return wrap
