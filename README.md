# data-automation-tools

데이터 수집·자동화·문서 처리 도구 모음입니다. 공통 원칙 하나로 만들었습니다:

> **모든 도구는 검증 리포트를 동봉한다 — "돌아간다"가 아니라 "이만큼 정확하다"로.**

각 도구 폴더에 실행 가능한 코드와 함께, 데모 데이터로 돌린 검증 리포트(`*_verify.md` / `*_report.md`)가 들어 있습니다.

## 도구

| 도구 | 무엇을 하나 | 검증 결과 (데모 기준) |
|---|---|---|
| [`tools/rag_qa`](tools/rag_qa) | 문서 폴더 → 근거 출처를 표시하는 Q&A 챗봇. 문서에 없는 질문은 **추측하지 않고 거절** | 검색 적중 15/15 · **오답 0건** · 범위밖 거절 4/4 |
| [`tools/notion_slack`](tools/notion_slack) | 노션 DB 변경 감지 → 슬랙 알림 커넥터. 첫 실행 알림폭탄 방지·중복 0·전송 실패 시 보류 후 재송 | 시나리오 **8/8 PASS** (대량·전송실패·상태손상 포함) |
| [`tools/settle_report`](tools/settle_report) | 거래내역 → 파트너별 정산서. 수수료·환불·원천징수(소액부징수)·부가세까지 규칙 계산 | **2단 대사 0원** · 독립 손검산 일치 · 조작 검출 |
| [`tools/csv_dashboard`](tools/csv_dashboard) | CSV → 서버·설치 없이 열리는 단일 HTML 대시보드 (KPI·차트·검색 테이블) | 수치 독립 재집계 전수 대조 · 실브라우저 렌더 검증 |
| [`tools/pdf_excel`](tools/pdf_excel) | 거래명세서 PDF 묶음 → 정리 엑셀. 품목합계 vs 인쇄합계 자동 대조 | 왕복 대조 문서 12/12 · 품목 **70/70** (멀티페이지 포함) |
| [`tools/macro_filler`](tools/macro_filler) | 표(CSV)의 각 행을 데스크톱 프로그램에 반복 입력하는 매크로. 창 확인·포커스 검사·비상정지·저장 내용 대조 | **100건 실측 99/100** · 경계값 24/24 · 오입력 0 (실패 시 입력 안 보내고 중단) |
| [`tools/ai_structure`](tools/ai_structure) | 지저분한 CSV → 분류·정규화·집계·요약 서식 엑셀 (규칙→학습ML→LLM 3티어) | 재현성 OK · 유료 호출 게이트 차단 증명 · 분류 근거 기록 |
| [`tools/excel_vba`](tools/excel_vba) | 엑셀 VBA 매크로 3종 (시트 통합·데이터 정리·그룹 집계) | LibreOffice 검증 **8/8** ([VERIFY.md](tools/excel_vba/VERIFY.md)) |
| [`tools/weekly_report`](tools/weekly_report) | 판매 내역 → 주간 성과 리포트 (전주 대비·채널별·상위 상품) | 독립 재집계 일치 · **주차 경계(일/월) 정확 분리** · 없는 주 "데이터 없음" 정직 표기 |
| [`tools/sensor_logger`](tools/sensor_logger) | 측정값 스트림 → SQLite 저장 + 결측·이상값·정체 자동 감시 | **심어둔 결함 9/9 검출·오탐 0** · 유실 0 · 재실행 중복 0 |
| [`tools/webform_filler`](tools/webform_filler) | CSV 목록을 웹 폼에 자동 입력·제출 (검수 모드 내장) | 서버 기록과 **필드 전수 대조 30/30** · 필수 누락은 제출 금지·격리 |

## 공용 모듈 (`core/`)

- `xlsx.py` — 서식 엑셀 출력 (요약 시트, 앞자리 0 보존)
- `ai.py` — LLM 호출층. **승인·호출상한·자격 게이트를 전부 통과해야만 유료 호출** + 캐시·사전 비용 견적(dry-run)
- `ml.py` — 학습 분류기 (TF-IDF + XGBoost, 홀드아웃 평가, 저장/로드)
- `gui.py` — 데스크톱 매크로 안전 골격 (이미지·창 기준, 포커스 검사, 비상정지, 결과 내용 대조)
- `watch.py` — 변경 감시 엔진 (스냅샷→diff→중복 제거→실패 시 보류 재송)

## 실행

```bash
# 각 도구는 데모 생성 + 검증까지 한 번에 돕니다
python tools/rag_qa/rag_qa.py --make-demo
python tools/rag_qa/rag_qa.py --serve          # 챗 UI (http://127.0.0.1:8765)
python tools/notion_slack/notion_watch.py --verify
python tools/settle_report/settle_report.py --make-demo
python tools/csv_dashboard/csv_dashboard.py --make-demo
python tools/pdf_excel/pdf_excel.py --make-demo
```

요구사항: Python 3.10+ · 도구별 추가 패키지 — `scikit-learn`(rag_qa), `playwright`(csv_dashboard·pdf_excel 데모 생성), `pypdf`(pdf_excel), `openpyxl`(엑셀 출력), `anthropic`(rag_qa LLM 티어, 선택).

## 설계 원칙

1. **검증 동봉** — 데모 데이터로 정확도·재현성·경계값을 측정한 리포트를 코드와 같이 둡니다.
2. **정직 거절** — 근거가 없으면 아는 척하지 않습니다 (RAG 거절 설계, 불량 행 격리 표기).
3. **비용 안전** — 유료 API는 승인·상한·견적 게이트 없이는 호출 자체가 차단됩니다.
4. **의존 최소** — 가능한 표준 라이브러리로. 대시보드는 외부 CDN 없이 파일 하나로 완결.

## License

MIT
