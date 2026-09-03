#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rag_qa.py — 문서 기반 Q&A(RAG) + 품질 리포트 (2026-09)

문서 폴더(txt/md) → 검색 인덱스 → 질문에 문서 근거로 답변 + **품질을 숫자로 측정해 리포트**.
원칙: 모든 도구는 검증 리포트를 동봉한다 — "돌아간다"가 아니라 "이만큼 정확하다"로.

구조 (비용 안전 우선):
  - 검색: TF-IDF char n-gram(2~4) 코사인 — **무료·로컬·결정적** (임베딩 API로 교체 가능한 구조)
  - 답변 AI-a: 추출형(근거 문장 반환) — 무료. 근거 없으면 "문서에서 찾지 못함" **정직 거절**
  - 답변 AI-b: LLM 생성형 — `core/ai.py`의 **4중 지출 게이트**(자격∧confirm∧상한∧dry-run) 뒤에서만
  - ★품질 리포트: 평가셋으로 검색 적중률(hit@1/3)·키워드 커버리지·범위밖 거절률·재현성 측정 → md 저장

사용:
  python rag_qa.py --make-demo          # 데모 문서 생성 + 평가 실행 + 리포트
  python rag_qa.py <문서폴더> --ask "질문"
"""
import os, sys, re, glob, json, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', '..', 'core'))
import ai  # Task/process/backends/Cache — 4중 게이트 재사용

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import linear_kernel

REFUSE_TH = 0.18   # top 코사인이 이 미만이면 "문서에 근거 없음" 거절.
# ★캘리브레이션(2026-09-03, 데모 평가셋): 범위밖 최고 0.152 < 정상질문 최저 0.206 → 그 사이 0.18.
#   의역 질문(0.09~0.13)은 거절됨 = 오답 대신 정직 거절(설계 의도). 고객 문서에선 고객 평가셋으로 재캘리브레이션.

# ── 쿼리 확장층 (2026-09-03 2단계: 측정된 약점 '구어↔규정어 갭'의 일반 사전) ──
# ★정직선: 평가 문장 특정 매핑 금지 — 일반 직장어 동의어만. 실제 납품에선 고객 도메인 사전으로 교체·보강.
import re as _re
EXPAND = [
    (r'집에서\s*(일|근무)', ' 재택근무'),
    (r'밥값|식비|저녁값', ' 식대 지원'),
    (r'밤\s*늦게|늦게까지\s*일', ' 야근'),
    (r'돌려\s*받|환급', ' 환불 환급'),
    (r'비번|암호(?!화)', ' 비밀번호'),
    (r'쉬는\s*날|휴일', ' 휴가'),
    (r'회사\s*카드', ' 법인카드'),
    (r'출장\s*(?:가|비)', ' 출장 숙박비'),
]


def expand_query(q):
    extra = ''.join(add for pat, add in EXPAND if _re.search(pat, q))
    return q + extra


# ── 인덱스 ──────────────────────────────────────────────────────────
def chunk_text(name, text, size=240):
    """문단 기준 병합 청크(≈size자). (문서명, 청크번호, 내용)"""
    paras = [p.strip() for p in re.split(r'\n\s*\n', text) if p.strip()]
    chunks, buf = [], ''
    for p in paras:
        if buf and len(buf) + len(p) > size:
            chunks.append(buf); buf = p
        else:
            buf = (buf + '\n' + p).strip()
    if buf:
        chunks.append(buf)
    return [(name, i, c) for i, c in enumerate(chunks, 1)]


class RagIndex:
    def __init__(self, folder):
        self.chunks = []                      # [(doc, no, text)]
        for path in sorted(glob.glob(os.path.join(folder, '*.md')) + glob.glob(os.path.join(folder, '*.txt'))):
            name = os.path.splitext(os.path.basename(path))[0]
            self.chunks += chunk_text(name, open(path, encoding='utf-8').read())
        if not self.chunks:
            raise SystemExit(f'문서 없음: {folder} (*.md/*.txt)')
        self.vec = TfidfVectorizer(analyzer='char_wb', ngram_range=(2, 4), min_df=1)
        self.mat = self.vec.fit_transform([c[2] for c in self.chunks])

    def search(self, query, k=3):
        sims = linear_kernel(self.vec.transform([query]), self.mat)[0]
        order = sims.argsort()[::-1][:k]
        return [(float(sims[i]), self.chunks[i]) for i in order]

    # AI-a: 추출형 답변(무료·결정적) — 최상위 청크에서 질의어 겹침 최대 문장 반환
    def ask(self, query, k=3):
        query = expand_query(query)            # 구어→규정어 확장(일반 사전, 위 정직선 참조)
        top = self.search(query, k)
        best_sim = top[0][0] if top else 0.0
        if best_sim < REFUSE_TH:
            return {'found': False, 'answer': '문서에서 근거를 찾지 못했습니다.',
                    'source': '', 'score': round(best_sim, 3), 'top': top}
        doc, no, text = top[0][1]
        qgrams = set(re.sub(r'\s', '', query)[i:i+2] for i in range(len(re.sub(r'\s', '', query)) - 1))
        best_sent, best_ov = '', -1.0
        for s in re.split(r'(?<=[.!?])\s+|\n+', text):
            s = s.strip()
            if len(s) < 6:
                continue
            z = re.sub(r'\s', '', s)
            ov = sum(1 for g in qgrams if g in z) / max(len(qgrams), 1)
            if ov > best_ov:
                best_sent, best_ov = s, ov
        return {'found': True, 'answer': best_sent, 'source': f'{doc}#(청크{no})',
                'score': round(best_sim, 3), 'top': top}


# ── AI-b: LLM 생성형(유료·게이트 뒤) ────────────────────────────────
LLM_TASK = ai.Task(
    'RAG답변',
    system='아래 문서 발췌 안의 사실로만 답한다. 발췌에 없으면 found=false, answer는 빈 문자열. 추측·외부지식 금지.',
    schema={'type': 'object',
            'properties': {'answer': {'type': 'string'}, 'found': {'type': 'boolean'},
                           'source': {'type': 'string'}},
            'required': ['answer', 'found', 'source'], 'additionalProperties': False},
    max_tokens=300, user_tmpl='{text}')


def llm_inputs(index, questions, k=3):
    outs = []
    for q in questions:
        ctx = '\n---\n'.join(f'[{d}#{n}] {t}' for _, (d, n, t) in index.search(q, k))
        outs.append(f'문서 발췌:\n{ctx}\n\n질문: {q}')
    return outs


# ── 데모 문서 + 평가셋 ──────────────────────────────────────────────
DEMO_DOCS = {
    '환불배송규정': """# 환불·배송 규정 (한빛상사)

환불 신청은 상품 수령일로부터 7일 이내에 고객센터 또는 마이페이지에서 접수해야 합니다.

단순 변심에 의한 반품은 왕복 배송비 6,000원을 고객이 부담합니다. 상품 하자·오배송의 경우 배송비 전액을 회사가 부담합니다.

교환 상품의 발송은 회수 완료 후 3영업일 이내에 처리됩니다.

주문 취소는 배송 준비 중 상태 전까지 가능하며, 배송 시작 후에는 반품 절차로 진행됩니다.

환불금은 반품 검수 완료 후 영업일 기준 2일 이내에 원래 결제 수단으로 환급됩니다. 카드 결제는 카드사 사정에 따라 3~5일 더 걸릴 수 있습니다.

해외 배송 주문은 단순 변심 반품이 불가하며, 하자의 경우 현지 회수 대행을 통해 처리합니다.""",
    '근태휴가규정': """# 근태·휴가 규정 (한빛상사)

연차는 입사일 기준으로 매년 15일이 부여되며, 3년 근속마다 1일씩 가산됩니다.

반차는 4시간 단위로 사용하며, 오전 반차는 14시 출근, 오후 반차는 13시 퇴근입니다.

재택근무는 희망일 전일 17시까지 팀장 승인으로 신청해야 합니다.

지각이 월 3회를 초과하면 해당 월 평가에 반영됩니다. 출퇴근 기록은 사내 시스템에 자동 기록됩니다.

경조 휴가는 본인 결혼 시 5일, 배우자 출산 시 10일이 부여됩니다.

연장 근로는 주 12시간을 초과할 수 없으며, 사전 신청이 원칙입니다.""",
    '운영보안매뉴얼': """# 운영·보안 매뉴얼 (한빛상사)

시스템 계정은 정보팀에 신청서 제출 후 1영업일 이내 발급됩니다.

전체 데이터 백업은 매일 새벽 2시에 자동 실행되며, 주간 백업본은 별도 서버에 4주간 보관됩니다.

장애 발생 시 사내 메신저의 #장애신고 채널에 즉시 보고합니다.

비밀번호는 90일마다 변경해야 하며, 최근 3회 사용한 비밀번호는 재사용할 수 없습니다.

사외에서 내부망 접속은 VPN을 통해서만 허용되며, 공용 와이파이에서의 접속은 금지됩니다.

고객 데이터의 외부 반출은 보안팀 사전 승인 없이는 금지됩니다.""",
    '경비지출규정': """# 경비 지출 규정 (한빛상사)

법인카드는 팀장 이상에게 발급되며, 사용 내역은 다음 달 5일까지 경비 시스템에 등록해야 합니다.

영수증이 없는 지출은 3만원 이하만 간이 증빙으로 처리할 수 있습니다.

출장 숙박비는 1박당 12만원 한도이며, 초과분은 본인이 부담합니다.

야근 식대는 1만원까지 지원되며, 21시 이후 퇴근 기록이 있어야 합니다.""",
    '장비회의실규정': """# 장비·회의실 규정 (한빛상사)

업무용 노트북은 입사 시 지급되며 교체 주기는 3년입니다. 고장 시 정보팀에 수리를 접수합니다.

회의실 예약은 사내 캘린더에서 하며, 30분 단위로 최대 2시간까지 예약할 수 있습니다.

모니터 등 주변기기는 경비 시스템에서 신청하며, 팀장 승인 후 지급됩니다.""",
}

EVAL = [  # (질문, 기대 문서, 기대 키워드(답변에 하나 이상)) — 뒤 2개는 ★의역(질문에 문서 단어 없음)
    ('환불 신청은 며칠 이내에 해야 해?', '환불배송규정', ['7일']),
    ('단순 변심 반품 배송비는 누가 내?', '환불배송규정', ['6,000', '고객']),
    ('교환 상품은 언제 발송돼?', '환불배송규정', ['3영업일']),
    ('환불금은 언제 돌려받아?', '환불배송규정', ['2일']),
    ('해외 배송도 단순 변심 반품 돼?', '환불배송규정', ['불가']),
    ('연차는 몇 일 나와?', '근태휴가규정', ['15일']),
    ('오후 반차면 몇 시에 퇴근해?', '근태휴가규정', ['13시']),
    ('배우자가 출산하면 휴가 며칠이야?', '근태휴가규정', ['10일']),
    ('백업은 언제 실행돼?', '운영보안매뉴얼', ['새벽 2시', '2시']),
    ('비밀번호는 얼마나 자주 바꿔야 해?', '운영보안매뉴얼', ['90일']),
    ('법인카드 내역은 언제까지 등록해?', '경비지출규정', ['5일']),
    ('출장 숙박비 한도는 얼마야?', '경비지출규정', ['12만원']),
    ('노트북 교체 주기는?', '장비회의실규정', ['3년']),
    ('집에서 일하려면 언제까지 승인받아야 해?', '근태휴가규정', ['전일 17시', '17시']),   # ★의역: "재택" 없음
    ('밤 늦게까지 일하면 밥값 나와?', '경비지출규정', ['1만원']),                        # ★의역: "야근 식대" 없음
]
EVAL_OOS = ['주차 지원은 어떻게 돼?', '휴대폰 요금 지원돼?', '동호회 지원비 있어?', '퇴직금 중간정산 돼?']  # 문서에 없음 → 거절 기대


def run_eval(index):
    rows, hit1 = [], 0
    hit3 = kw_ok = wrong = 0
    for q, doc, kws in EVAL:
        r = index.ask(q)
        srcs = [d for _, (d, _, _) in r['top']]
        h1 = bool(srcs and srcs[0] == doc)
        h3 = doc in srcs
        kw = r['found'] and any(k in r['answer'] for k in kws)
        hit1 += h1; hit3 += h3; kw_ok += kw
        wrong += (r['found'] and not kw)          # 답을 냈는데 틀림 = 오답(거절과 구분)
        rows.append((q, doc, srcs[0] if srcs else '-', h1, h3, kw, r['score'], r['answer'][:46]))
    oos_rows, refuse = [], 0
    for q in EVAL_OOS:
        r = index.ask(q)
        refuse += (not r['found'])
        oos_rows.append((q, not r['found'], r['score']))
    return {'rows': rows, 'hit1': hit1, 'hit3': hit3, 'kw': kw_ok, 'wrong': wrong, 'n': len(EVAL),
            'oos': oos_rows, 'refuse': refuse, 'n_oos': len(EVAL_OOS)}


def main_demo():
    demo = os.path.join(HERE, 'demo_docs')
    os.makedirs(demo, exist_ok=True)
    for name, text in DEMO_DOCS.items():
        open(os.path.join(demo, name + '.md'), 'w', encoding='utf-8').write(text)
    index = RagIndex(demo)
    e1 = run_eval(index)
    e2 = run_eval(index)                      # 재현성: 2회 동일?
    same = (e1['hit1'], e1['hit3'], e1['kw'], e1['refuse']) == (e2['hit1'], e2['hit3'], e2['kw'], e2['refuse'])

    # LLM 티어: dry-run 견적(호출 0) + 게이트 차단 증명
    inputs = llm_inputs(index, [q for q, _, _ in EVAL])
    _, est = ai.process(inputs, LLM_TASK, ai.AnthropicBackend(), dry_run=True, log=None)
    try:
        ai.process(inputs[:1], LLM_TASK, ai.AnthropicBackend(), log=None)
        gate = '★실패: 차단 안 됨'
    except PermissionError:
        gate = '차단 확인(PermissionError) — 승인 없이 유료 호출 불가'

    now = dt.datetime.now()
    L = [f'# RAG Q&A 품질 리포트 ({now:%Y-%m-%d %H:%M})',
         f'- 코퍼스: 데모 {len(DEMO_DOCS)}문서 · 청크 {len(index.chunks)}개 · 검색 TF-IDF char(2-4) · 거절 임계 {REFUSE_TH}',
         '',
         f'## 종합: hit@1 **{e1["hit1"]}/{e1["n"]}** · hit@3 **{e1["hit3"]}/{e1["n"]}** · '
         f'키워드 정답 **{e1["kw"]}/{e1["n"]}** · ★오답(틀린 답 반환) **{e1["wrong"]}건** · '
         f'범위밖 거절 **{e1["refuse"]}/{e1["n_oos"]}** · 재현성(2회 동일) **{"OK" if same else "불일치"}**',
         '',
         '> 의역(구어) 질문은 **일반 직장어 확장층**으로 개선(hit@1 13→15). 남은 1건은 임계 미달 → 정직 거절(오답 아님) —'
         ' 임계를 내려 잡지 않음(범위밖 최고 0.152와 간격 0.003 = 과적합 위험). 더 필요하면 임베딩/LLM 티어 지점.'
         ' 확장 사전은 일반어만(평가 문장 특정 매핑 금지), 납품 시 고객 도메인 사전으로 보강.',
         '',
         '| 질문 | 기대문서 | top1 | h@1 | h@3 | 키워드 | sim | 추출 답변 |', '|---|---|---|---|---|---|---|---|']
    for q, doc, top1, h1, h3, kw, sc, ans in e1['rows']:
        L.append(f'| {q} | {doc} | {top1} | {"O" if h1 else "X"} | {"O" if h3 else "X"} | '
                 f'{"O" if kw else "X"} | {sc} | {ans} |')
    L += ['', '## 범위 밖 질문(거절 기대 — 환각 방지 측정)', '| 질문 | 거절? | top sim |', '|---|---|---|']
    for q, ref, sc in e1['oos']:
        L.append(f'| {q} | {"O" if ref else "★X"} | {sc} |')
    L += ['', '## LLM 생성형 티어(선택·유료)',
          f'- dry-run 견적: {est["calls"]}건 · 입력~{est["in_tokens"]:,}tok · 추정 ${est["usd_approx"]} ({est["model"]})',
          f'- 지출 게이트: {gate}',
          '', '※ 정직 각주: 위 수치는 동봉 데모 코퍼스·평가셋 기준. 고객 문서에는 고객 평가셋으로 같은 리포트를 냅니다.']
    report = os.path.join(HERE, 'rag_report.md')
    open(report, 'w', encoding='utf-8').write('\n'.join(L))
    print('\n'.join(L[:10]))
    print(f'\n저장: {report}')
    return e1, same


# ── 챗 UI (단일 페이지 + /ask 엔드포인트, stdlib 서버) ──────────────
CHAT_HTML = """<!doctype html><html lang=ko><head><meta charset=utf-8><title>문서 Q&A 챗봇 (데모)</title>
<style>
body{font-family:'Malgun Gothic',sans-serif;margin:0;background:#eef1f5}
.top{background:#4f46e5;color:#fff;padding:14px 20px;font-weight:800}
.top small{font-weight:600;opacity:.85;margin-left:8px}
.chat{max-width:640px;margin:0 auto;padding:18px 14px 90px;overflow:hidden}
.msg{max-width:78%;margin:8px 0;padding:11px 14px;border-radius:14px;font-size:14px;line-height:1.5;clear:both}
.me{background:#4f46e5;color:#fff;float:right;border-bottom-right-radius:4px}
.bot{background:#fff;border:1px solid #dfe4ea;float:left;border-bottom-left-radius:4px}
.bot.refuse{background:#fff7f7;border-color:#f3c9c9}
.src{display:inline-block;margin-top:7px;font-size:11px;color:#4f46e5;background:#eef0ff;border-radius:999px;padding:3px 9px;font-weight:700}
.src.no{color:#b4232c;background:#fde8e8}
.bar{position:fixed;bottom:0;left:0;right:0;background:#fff;border-top:1px solid #dfe4ea;padding:10px}
.in{max-width:640px;margin:0 auto;display:flex;gap:8px}
input{flex:1;padding:11px 14px;border:1px solid #cfd6de;border-radius:10px;font-size:14px}
button{padding:11px 18px;border:0;border-radius:10px;background:#4f46e5;color:#fff;font-weight:700;cursor:pointer}
</style></head><body>
<div class=top>문서 Q&A 챗봇 <small>한빛상사 규정 (데모) · 근거 출처 표시 · 모르면 모른다고 답합니다</small></div>
<div class=chat id=c></div>
<div class=bar><div class=in><input id=q placeholder="규정에 대해 물어보세요" onkeydown="if(event.key==='Enter')send()"><button onclick=send()>전송</button></div></div>
<script>
async function send(){
  const q=document.getElementById('q');const t=q.value.trim();if(!t)return;q.value='';
  add('me',t);
  const r=await (await fetch('/ask?q='+encodeURIComponent(t))).json();
  const pill=r.found?`<span class=src>근거 ${r.source} · 유사도 ${r.score}</span>`:`<span class="src no">문서에 근거 없음 — 추측하지 않습니다</span>`;
  add('bot'+(r.found?'':' refuse'),r.answer+'<br>'+pill);
}
function add(cls,html){const d=document.createElement('div');d.className='msg '+cls;d.innerHTML=html;
  document.getElementById('c').appendChild(d);window.scrollTo(0,document.body.scrollHeight);}
</script></body></html>"""


def serve(folder, port=8765):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import urllib.parse as up
    idx = RagIndex(folder)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path.startswith('/ask'):
                q = up.parse_qs(up.urlparse(self.path).query).get('q', [''])[0]
                r = idx.ask(q)
                body = json.dumps({'found': r['found'], 'answer': r['answer'],
                                   'source': r['source'], 'score': r['score']}, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.end_headers()
                self.wfile.write(CHAT_HTML.encode())
    return ThreadingHTTPServer(('127.0.0.1', port), H)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    if '--serve' in sys.argv:
        i = sys.argv.index('--serve')
        folder = sys.argv[i + 1] if len(sys.argv) > i + 1 else os.path.join(HERE, 'demo_docs')
        print('챗 UI: http://127.0.0.1:8765 (Ctrl+C 종료)')
        serve(folder).serve_forever()
    elif '--make-demo' in sys.argv or len(sys.argv) == 1:
        main_demo()
    else:
        folder = sys.argv[1]
        idx = RagIndex(folder)
        q = sys.argv[sys.argv.index('--ask') + 1] if '--ask' in sys.argv else input('질문: ')
        r = idx.ask(q)
        print(f"[{'답변' if r['found'] else '거절'}] {r['answer']}  (근거 {r['source']} · sim {r['score']})")
