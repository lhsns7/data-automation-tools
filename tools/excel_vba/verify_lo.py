# -*- coding: utf-8 -*-
"""verify_lo.py — macros.bas의 VBA 매크로를 LibreOffice(VBA 호환)로 실증.

★반드시 LibreOffice 번들 파이썬으로 실행:
    "C:\\Program Files\\LibreOffice\\program\\python.exe" verify_lo.py

흐름: soffice --headless 소켓 기동 → 매크로마다 새 Calc 문서 1개 → 테스트 데이터 →
      문서 Basic 라이브러리에 'Option VBASupport 1' + macros.bas 주입 → Do*() 실행 → 결과 대조.
★각 매크로를 독립 문서로(한 시점 문서 1개) 돌려 ActiveSheet 모호성을 없앤다.
"""
import os, sys, time, subprocess, glob

HERE = os.path.dirname(os.path.abspath(__file__))
BAS = os.path.join(HERE, 'macros.bas')
PORT = 2002


def find_soffice():
    for p in (r'C:\Program Files\LibreOffice\program\soffice.exe',
              r'C:\Program Files (x86)\LibreOffice\program\soffice.exe'):
        if os.path.exists(p):
            return p
    hits = glob.glob(r'C:\Program Files*\LibreOffice\program\soffice.exe')
    return hits[0] if hits else None


def load_module_code():
    lines = open(BAS, encoding='utf-8').read().splitlines()
    body = [ln for ln in lines if not ln.startswith('Attribute ')]
    return 'Option VBASupport 1\n' + '\n'.join(body)


def connect(ctx_local, retries=40):
    resolver = ctx_local.ServiceManager.createInstanceWithContext(
        'com.sun.star.bridge.UnoUrlResolver', ctx_local)
    url = f'uno:socket,host=localhost,port={PORT};urp;StarOffice.ComponentContext'
    for _ in range(retries):
        try:
            return resolver.resolve(url)
        except Exception:
            time.sleep(0.5)
    raise RuntimeError('soffice 연결 실패')


def main():
    import uno

    soffice = find_soffice()
    if not soffice:
        print('LibreOffice 미설치'); return 2
    print('soffice:', soffice)
    proc = subprocess.Popen([soffice, '--headless', '--norestore', '--invisible', '--nologo',
        f'--accept=socket,host=localhost,port={PORT};urp;StarOffice.ComponentContext'])
    try:
        ctx = connect(uno.getComponentContext())
        smgr = ctx.ServiceManager
        desktop = smgr.createInstanceWithContext('com.sun.star.frame.Desktop', ctx)

        def new_calc():
            return desktop.loadComponentFromURL('private:factory/scalc', '_blank', 0, ())

        def set_grid(sheet, rows):
            for r, row in enumerate(rows):
                for c, v in enumerate(row):
                    cell = sheet.getCellByPosition(c, r)
                    if isinstance(v, (int, float)):
                        cell.setValue(v)
                    else:
                        cell.setString(str(v))

        def run_macro(doc, func, args=()):
            # ★ActiveWorkbook + ActiveSheet 둘 다 대상 문서를 가리키게: 프레임 활성 + 활성 시트 명시.
            #   (첫 매크로 실행 시 활성 시트가 미설정이면 VBA ActiveSheet가 빈 시트를 봄.)
            try:
                ctrl = doc.getCurrentController()
                desktop.setActiveFrame(ctrl.getFrame())
                ctrl.setActiveSheet(doc.Sheets.getByIndex(0))
            except Exception:
                pass
            libs = doc.BasicLibraries
            if not libs.hasByName('ExcelTools'):
                libs.createLibrary('ExcelTools')
            lib = libs.getByName('ExcelTools')
            code = load_module_code()
            if lib.hasByName('ExcelTools'):
                lib.replaceByName('ExcelTools', code)
            else:
                lib.insertByName('ExcelTools', code)
            provider = smgr.createInstanceWithContext(
                'com.sun.star.script.provider.MasterScriptProviderFactory', ctx).createScriptProvider(doc)
            script = provider.getScript(
                f'vnd.sun.star.script:ExcelTools.ExcelTools.{func}?language=Basic&location=document')
            return script.invoke(args, (), ())[0]

        passed = [0]; failed = [0]

        def check(name, cond, got, exp):
            ok = bool(cond)
            passed[0] += ok; failed[0] += (not ok)
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}: got={got} exp={exp}")

        # ── 테스트 A: 그룹 집계 (맨 처음 — 문서 1개·초기 상태) ──
        doc = new_calc()
        set_grid(doc.Sheets.getByIndex(0),
                 [['지역', '매출'], ['서울', 10], ['부산', 5], ['서울', 20], ['부산', 7]])
        groups = run_macro(doc, 'DoSummarizeAB')
        check('Summarize 그룹수', int(groups) == 2, groups, 2)
        if doc.Sheets.hasByName('요약'):
            sm = doc.Sheets.getByName('요약')
            check('Summarize 서울합계', sm.getCellByPosition(1, 1).getValue() == 30,
                  sm.getCellByPosition(1, 1).getValue(), 30)
            check('Summarize 부산합계', sm.getCellByPosition(1, 2).getValue() == 12,
                  sm.getCellByPosition(1, 2).getValue(), 12)
        else:
            check('Summarize 요약시트생성', False, '없음', '요약시트')
        doc.close(False)

        # ── 테스트 B: 데이터 정리 ──
        doc = new_calc()
        set_grid(doc.Sheets.getByIndex(0),
                 [['k', 'v'], ['x', 1], ['x', 1], ['', ''], ['y', 2], ['  z  ', 3]])
        removed = run_macro(doc, 'DoCleanData')
        check('CleanData 삭제행수', int(removed) == 2, removed, 2)
        s0 = doc.Sheets.getByIndex(0)
        check('CleanData 공백트림', s0.getCellByPosition(0, 3).getString() == 'z',
              s0.getCellByPosition(0, 3).getString(), 'z')
        doc.close(False)

        # ── 테스트 C: 시트 통합 ──
        doc = new_calc()
        sh = doc.Sheets
        set_grid(sh.getByIndex(0), [['이름', '값'], ['a', 1], ['b', 2]])
        sh.insertNewByName('S2', 1)
        set_grid(sh.getByIndex(1), [['이름', '값'], ['c', 3]])
        n = run_macro(doc, 'DoMergeSheets')
        check('MergeSheets 반환행수', int(n) == 3, n, 3)
        check('MergeSheets 통합시트생성', doc.Sheets.hasByName('통합'), doc.Sheets.hasByName('통합'), True)
        if doc.Sheets.hasByName('통합'):
            t = doc.Sheets.getByName('통합')
            check('MergeSheets 헤더보존', t.getCellByPosition(0, 0).getString() == '이름',
                  t.getCellByPosition(0, 0).getString(), '이름')
        doc.close(False)

        print(f'\n결과: PASS {passed[0]} / FAIL {failed[0]}')
        return 0 if failed[0] == 0 else 1
    finally:
        try:
            desktop.terminate()
        except Exception:
            pass
        proc.terminate()


if __name__ == '__main__':
    sys.exit(main())
