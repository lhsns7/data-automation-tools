# 검증 기록 — 엑셀 VBA 매크로 3종

- 매크로: 시트 통합(MergeSheets) · 데이터 정리(CleanData) · 그룹 집계(GroupSummary)
- 검증 환경: LibreOffice 26.8 (headless, `verify_lo.py`는 LibreOffice 내장 파이썬으로 실행)
- 결과: **검증 시나리오 8/8 PASS** (2026-08-31 실측)
- VBA→LibreOffice Basic 이식 시 확인한 함정 3건을 코드에 반영:
  ① Collection의 키 없는 Add 오작동 → 배열 사용
  ② ReDim 안의 IIf() → 크래시 → 분리
  ③ headless에서 ActiveSheet 미설정 → setActiveSheet 명시
- 실행: `soffice --headless` 환경에서 `verify_lo.py` (일반 파이썬에서는 uno 모듈이 없어 실행 불가)
