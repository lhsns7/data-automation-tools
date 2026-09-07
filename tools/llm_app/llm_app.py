#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""llm_app.py — LLM 서비스 골격: 문서 업로드 → 근거 기반 채팅 + 비용 게이트 + 사용량 회계 + 평가 리포트 (2026-09-07)

"우리 문서로 답하는 AI 서비스 만들어 주세요" 유형(AI 챗봇·GPT 연동 서비스)의 뼈대. 표준 라이브러리 + SQLite + TF-IDF(scikit-learn).
본체 = LLM 서비스가 실제로 망하는 자리 4곳을 구조로 막는 것:
  ★비용: 사용자별 일일 호출 상한·월 요금 상한·사전 견적·캐시 — 상한 밖 호출은 발생 자체가 0
  ★정직: 문서에 근거 없으면 "찾지 못함" 거절(오답 대신) · 답변마다 출처 청크 표시
  ★격리: 대화는 본인만(다른 사용자 대화 = 403) · 관리자는 사용량만
  ★측정: 평가셋(질문·정답 키워드·범위밖)으로 적중률·거절 정확도를 숫자로 — 개선 후 재측정 가능
답변 2모드: 추출형(무료·결정적, 기본) / 생성형(core/ai 4중 게이트 뒤 — 데모는 모의 백엔드로 호출·회계만 검증, 실서비스는 API 키 주입).

검증(main_demo, 로컬 서버 실구동):
  ①인증·권한(미로그인→로그인, user의 관리자 페이지·업로드 → 403) ②업로드→인덱스(청크 수=독립 계산, 정답셋 hit@3)
  ③정직 거절(범위밖 3문 전부 거절=오답 0, 정상 5문 출처 표시) ④★비용 게이트(일일 상한 3 → 4번째 거절·호출 0 / 캐시 히트 호출 0 / 월 상한 초과 → 첫 호출부터 거절)
  ⑤사용량 회계 항등(Σ호출=백엔드 카운터, 관리자 화면 합=테이블 합) ⑥대화 격리(타인 대화 403·순서 보존)
  ⑦평가 리포트(hit@3·거절률 = 독립 재계산) ⑧재현성·읽기 무부작용
실행: python llm_app.py --serve [포트]  (데모 계정 admin/admin1234 · user1/user1234 · user2/user2234)
※ 상한·모델·청크 크기·거절 임계는 설정값. 실서비스 = HTTPS 프록시 뒤 + ANTHROPIC_API_KEY + 관리자 승인(llm_enabled).
"""
import os, sys, re, io, json, html, hmac, hashlib, secrets, sqlite3, threading, datetime as dt
import urllib.parse, urllib.request, urllib.error, http.cookiejar

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', '..', 'core'))
import ai
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import linear_kernel

DB = os.path.join(HERE, 'llm_app.db')
DOCS = os.path.join(HERE, 'docs')
REFUSE_TH = 0.18            # 최상위 코사인이 이 미만이면 거절 (rag_qa 캘리브레이션 값, 고객 문서로 재조정)
DEFAULTS = {'daily_calls': '20', 'monthly_usd_cap': '5.0', 'llm_enabled': '0', 'refuse_th': str(REFUSE_TH), 'system_prompt': '아래 문서 발췌 안의 사실로만 답한다. 발췌에 없으면 찾지 못했다고 답한다.', 'k': '3'}
_INDEX = {'obj': None, 'lock': threading.Lock()}


# ── DB ──────────────────────────────────────────────────────────────
def open_db(path=DB):
    con = sqlite3.connect(path, timeout=10); con.row_factory = sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.executescript('''
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, pw_hash TEXT NOT NULL, salt TEXT NOT NULL, role TEXT NOT NULL, created TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, user_id INT NOT NULL, csrf TEXT NOT NULL, expires TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS docs(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL, chars INT NOT NULL, chunks INT NOT NULL, sha TEXT NOT NULL, uploaded_by TEXT NOT NULL, uploaded TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS convs(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INT NOT NULL, title TEXT NOT NULL, created TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS msgs(id INTEGER PRIMARY KEY AUTOINCREMENT, conv_id INT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, source TEXT, mode TEXT, ts TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS usage(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, user_id INT NOT NULL, mode TEXT NOT NULL, called INT NOT NULL, cached INT NOT NULL, refused TEXT, in_tokens INT NOT NULL, out_tokens INT NOT NULL, usd REAL NOT NULL);
    ''')
    for k, v in DEFAULTS.items():
        con.execute('INSERT OR IGNORE INTO settings VALUES(?,?)', (k, v))
    con.commit(); return con


def now(): return dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
def setting(con, k): return con.execute('SELECT value FROM settings WHERE key=?', (k,)).fetchone()[0]
def pw_hash(pw, salt): return hashlib.pbkdf2_hmac('sha256', pw.encode(), bytes.fromhex(salt), 200_000).hex()


def add_user(con, u, p, role):
    salt = secrets.token_hex(16)
    con.execute('INSERT INTO users(username, pw_hash, salt, role, created) VALUES(?,?,?,?,?)', (u, pw_hash(p, salt), salt, role, now())); con.commit()


def check_login(con, u, p):
    r = con.execute('SELECT * FROM users WHERE username=?', (u,)).fetchone()
    return r if r and hmac.compare_digest(r['pw_hash'], pw_hash(p, r['salt'])) else None


def new_session(con, uid):
    tok, csrf = secrets.token_hex(32), secrets.token_hex(16)
    con.execute('INSERT INTO sessions VALUES(?,?,?,?)', (tok, uid, csrf, (dt.datetime.now() + dt.timedelta(hours=8)).strftime('%Y-%m-%d %H:%M:%S'))); con.commit()
    return tok


def load_session(con, tok):
    if not tok: return None
    s = con.execute('SELECT s.csrf, s.expires, u.id AS uid, u.username, u.role FROM sessions s JOIN users u ON u.id=s.user_id WHERE token=?', (tok,)).fetchone()
    return dict(csrf=s['csrf'], uid=s['uid'], username=s['username'], role=s['role']) if s and s['expires'] >= now() else None


# ── 인덱스 (TF-IDF char n-gram, 무료·결정적) ─────────────────────────
def chunk_text(name, text, size=240):
    paras = [p.strip() for p in re.split(r'\n\s*\n', text) if p.strip()]
    chunks, buf = [], ''
    for p in paras:
        if buf and len(buf) + len(p) > size: chunks.append(buf); buf = p
        else: buf = (buf + '\n' + p).strip()
    if buf: chunks.append(buf)
    return [(name, i, c) for i, c in enumerate(chunks, 1)]


class Index:
    def __init__(self, folder):
        self.chunks = []
        for path in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
            if path.lower().endswith(('.md', '.txt')):
                self.chunks += chunk_text(os.path.splitext(path)[0], open(os.path.join(folder, path), encoding='utf-8', errors='replace').read())
        self.vec = self.mat = None
        if self.chunks:
            self.vec = TfidfVectorizer(analyzer='char_wb', ngram_range=(2, 4), min_df=1)
            self.mat = self.vec.fit_transform([c[2] for c in self.chunks])

    def search(self, q, k=3):
        if not self.chunks: return []
        sims = linear_kernel(self.vec.transform([q]), self.mat)[0]
        return [(float(sims[i]), self.chunks[i]) for i in sims.argsort()[::-1][:k]]

    def extract(self, q, k=3, th=REFUSE_TH):
        """추출형 답변: 최상위 청크에서 질의어 겹침 최대 문장. 임계(th) 미만 = 정직 거절."""
        top = self.search(q, k); best = top[0][0] if top else 0.0
        if best < th:
            return {'found': False, 'answer': '문서에서 근거를 찾지 못했습니다.', 'source': '', 'score': round(best, 3), 'top': top}
        doc, no, text = top[0][1]
        z = re.sub(r'\s', '', q); grams = set(z[i:i + 2] for i in range(len(z) - 1))
        best_s, best_ov = '', -1.0
        for s in re.split(r'(?<=[.!?])\s+|\n+', text):
            s = s.strip()
            if len(s) < 6: continue
            zz = re.sub(r'\s', '', s); ov = sum(1 for g in grams if g in zz) / max(len(grams), 1)
            if ov > best_ov: best_s, best_ov = s, ov
        return {'found': True, 'answer': best_s, 'source': f'{doc}#청크{no}', 'score': round(best, 3), 'top': top}


def get_index(rebuild=False):
    with _INDEX['lock']:
        if rebuild or _INDEX['obj'] is None: _INDEX['obj'] = Index(DOCS)
        return _INDEX['obj']


def register_doc(con, name, text, user):
    os.makedirs(DOCS, exist_ok=True)
    safe = re.sub(r'[^\w가-힣.\- ]', '_', name)[:80]
    open(os.path.join(DOCS, safe), 'w', encoding='utf-8').write(text)
    n = len(chunk_text(os.path.splitext(safe)[0], text))
    con.execute('INSERT OR REPLACE INTO docs(name, chars, chunks, sha, uploaded_by, uploaded) VALUES(?,?,?,?,?,?)',
                (safe, len(text), n, hashlib.sha256(text.encode()).hexdigest()[:12], user, now())); con.commit()
    get_index(rebuild=True); return n


# ── 생성형 백엔드 (게이트 뒤) ───────────────────────────────────────
LLM_TASK = ai.Task('문서QA', system=DEFAULTS['system_prompt'],
                   schema={'type': 'object', 'properties': {'answer': {'type': 'string'}, 'found': {'type': 'boolean'}, 'source': {'type': 'string'}},
                           'required': ['answer', 'found', 'source'], 'additionalProperties': False}, max_tokens=300)


class DemoLLM:
    """모의 생성형 백엔드: 호출 수를 세고 발췌 첫 문장으로 답을 조립. 무료 — 게이트·회계 검증용. 실서비스 = ai.AnthropicBackend."""
    kind = 'mock-llm'; free = True
    def __init__(self): self.calls = 0
    def run_one(self, text, task):
        self.calls += 1
        m = re.search(r'\[(.+?)#(\d+)\]\s*(.+?)(?:\n---|\n\n질문)', text, re.S)
        return {'answer': '[모의 생성] ' + (m.group(3).strip().split('\n')[0][:120] if m else ''), 'found': bool(m), 'source': f'{m.group(1)}#청크{m.group(2)}' if m else ''}


BACKEND = {'obj': DemoLLM(), 'cache': None}


def month_usd(con):
    return con.execute("SELECT COALESCE(SUM(usd),0) FROM usage WHERE ts >= ?", (dt.datetime.now().strftime('%Y-%m-01'),)).fetchone()[0]


def answer(con, user, q, mode):
    """질문 → 답. mode=extract(무료) / llm(게이트: 일일 상한·월 상한·캐시). 사용량 1행 기록."""
    idx = get_index(); k = int(setting(con, 'k')); th = float(setting(con, 'refuse_th'))
    if mode != 'llm':
        r = idx.extract(q, k, th)
        con.execute('INSERT INTO usage(ts,user_id,mode,called,cached,refused,in_tokens,out_tokens,usd) VALUES(?,?,?,?,?,?,?,?,?)', (now(), user['uid'], 'extract', 0, 0, None, 0, 0, 0.0)); con.commit()
        return r
    top = idx.search(q, k)
    if not top or top[0][0] < th:                                 # 근거 없으면 LLM도 부르지 않는다(비용 0·오답 0)
        con.execute('INSERT INTO usage(ts,user_id,mode,called,cached,refused,in_tokens,out_tokens,usd) VALUES(?,?,?,?,?,?,?,?,?)', (now(), user['uid'], 'llm', 0, 0, '근거없음', 0, 0, 0.0)); con.commit()
        return {'found': False, 'answer': '문서에서 근거를 찾지 못했습니다.', 'source': '', 'score': round(top[0][0] if top else 0, 3)}
    today = con.execute("SELECT COUNT(*) FROM usage WHERE user_id=? AND mode='llm' AND called=1 AND ts >= ?", (user['uid'], dt.date.today().isoformat())).fetchone()[0]
    cap = int(setting(con, 'daily_calls'))
    ctx = '\n---\n'.join(f'[{d}#{n}] {t}' for _, (d, n, t) in top)
    prompt = f'문서 발췌:\n{ctx}\n\n질문: {q}'
    est = ai.estimate([prompt], LLM_TASK)
    cache = BACKEND['cache']; hit = cache.get(prompt, LLM_TASK) if cache is not None else None
    if hit is not None:
        con.execute('INSERT INTO usage(ts,user_id,mode,called,cached,refused,in_tokens,out_tokens,usd) VALUES(?,?,?,?,?,?,?,?,?)', (now(), user['uid'], 'llm', 0, 1, None, 0, 0, 0.0)); con.commit()
        return dict(hit, score=round(top[0][0], 3), cached=True)
    refused = None
    if today >= cap: refused = f'일일 상한 {cap}회 초과'
    elif month_usd(con) + est['usd_approx'] > float(setting(con, 'monthly_usd_cap')): refused = f'월 요금 상한 ${setting(con, "monthly_usd_cap")} 초과 예상'
    if refused:
        con.execute('INSERT INTO usage(ts,user_id,mode,called,cached,refused,in_tokens,out_tokens,usd) VALUES(?,?,?,?,?,?,?,?,?)', (now(), user['uid'], 'llm', 0, 0, refused, 0, 0, 0.0)); con.commit()
        return {'found': False, 'answer': f'생성형 답변 거절: {refused}. 추출형 답변을 이용하거나 관리자에게 문의하세요.', 'source': '', 'score': round(top[0][0], 3), 'refused': refused}
    be = BACKEND['obj']
    results, rep = ai.process([prompt], LLM_TASK, be, cache=cache, confirm_spend=True, max_calls=1, log=_Quiet())
    r = results[0]; usd = est['usd_approx'] if not getattr(be, 'free', False) else 0.0
    con.execute('INSERT INTO usage(ts,user_id,mode,called,cached,refused,in_tokens,out_tokens,usd) VALUES(?,?,?,?,?,?,?,?,?)',
                (now(), user['uid'], 'llm', rep['called'], rep['cached'], None, est['in_tokens'], est['out_tokens_max'], usd)); con.commit()
    return dict(r, score=round(top[0][0], 3))


class _Quiet:
    def info(self, m): pass


# ── 평가 ───────────────────────────────────────────────────────────
def run_eval(idx, ev, k=3, th=REFUSE_TH):
    """평가셋 {'eval':[{q,doc,keywords}], 'oos':[q]} → 수치 + 거절 임계 캘리브레이션(범위밖 최고점·정상 최저점·제안 임계). rag_audit 형식 호환."""
    rows, hit3, cov = ev.get('eval', []), 0, 0.0
    detail, pos_scores = [], []
    for r in rows:
        top = idx.search(r['q'], k); docs = [d for _, (d, _, _) in top]
        h = r['doc'] in docs; hit3 += h
        a = idx.extract(r['q'], k, th); text = a['answer']; pos_scores.append(a['score'])
        c = sum(1 for kw in r.get('keywords', []) if kw in text) / max(len(r.get('keywords', [])), 1); cov += c
        detail.append((r['q'], h, round(c, 2), a['found']))
    oos = ev.get('oos', []); oos_scores = [idx.extract(q, k, th)['score'] for q in oos]; refused = sum(1 for sc in oos_scores if sc < th)
    oos_max = max(oos_scores) if oos_scores else 0.0; pos_min = min(pos_scores) if pos_scores else 0.0
    suggested = round((oos_max + pos_min) / 2, 3) if pos_min > oos_max else None
    return {'n': len(rows), 'hit3': hit3, 'hit3_rate': round(hit3 / max(len(rows), 1), 3), 'coverage': round(cov / max(len(rows), 1), 3),
            'oos_n': len(oos), 'oos_refused': refused, 'oos_refuse_rate': round(refused / max(len(oos), 1), 3), 'detail': detail,
            'th': th, 'oos_max': oos_max, 'pos_min': pos_min, 'suggested_th': suggested}


# ── HTML ───────────────────────────────────────────────────────────
CSS = """body{font-family:'Malgun Gothic',system-ui,sans-serif;margin:0;background:#f3f5f9;color:#1c2028}nav{background:#111827;color:#fff;padding:10px 22px;display:flex;gap:18px;align-items:center}
nav a{color:#e5e7eb;text-decoration:none;font-weight:700}nav .u{margin-left:auto;font-size:13px;color:#cbd5e1}main{max-width:1100px;margin:22px auto;padding:0 16px}
h1{font-size:20px;margin:0 0 12px}.btn{padding:8px 14px;border:0;border-radius:8px;background:#7c3aed;color:#fff;font-weight:700;cursor:pointer;text-decoration:none;font-size:14px;display:inline-block}
.btn.gray{background:#64748b}.btn.sm{padding:5px 10px;font-size:13px}input,select,textarea{padding:8px 10px;border:1px solid #cfd6de;border-radius:8px;font-size:14px;font-family:inherit}
.card{background:#fff;border:1px solid #e2e6ec;border-radius:12px;padding:18px}.grid{display:grid;grid-template-columns:260px 1fr;gap:16px}
.convs a{display:block;padding:8px 10px;border-radius:8px;color:#1c2028;text-decoration:none}.convs a.on{background:#ede9fe;font-weight:700}
.msg{margin:10px 0;padding:12px 14px;border-radius:12px;max-width:80%;white-space:pre-wrap;line-height:1.5}.me{background:#ede9fe;margin-left:auto}.bot{background:#fff;border:1px solid #e2e6ec}
.src{font-size:12px;color:#6b7280;margin-top:6px}.no{color:#991b1b;font-weight:700}table{width:100%;border-collapse:collapse;background:#fff}th{background:#eef2f7;text-align:left;font-size:13px;padding:8px}td{padding:8px;border-bottom:1px solid #eef0f4;font-size:14px}
td.num{text-align:right}.muted{color:#6b7280;font-size:13px}label{display:block;font-size:13px;font-weight:700;margin:10px 0 4px}.flash{padding:10px 14px;border-radius:8px;margin-bottom:12px;font-weight:700;background:#dcfce7;color:#166534}.flash.bad{background:#fee2e2;color:#991b1b}"""
esc = lambda v: html.escape('' if v is None else str(v))


def layout(title, body, user=None, msg=None):
    nav = '<a href="/chat">채팅</a>' + ('<a href="/admin">관리자</a>' if user and user['role'] == 'admin' else '')
    who = (f'<span class=u>{esc(user["username"])} · {esc(user["role"])} <form method=post action=/logout style="display:inline"><input type=hidden name=_csrf value="{user["csrf"]}"><button class="btn gray sm">로그아웃</button></form></span>') if user else ''
    fl = f'<div class="flash {"bad" if msg.startswith("!") else ""}">{esc(msg.lstrip("!"))}</div>' if msg else ''
    return (f'<!doctype html><html lang=ko><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>{esc(title)} · 문서 AI</title><style>{CSS}</style></head>'
            f'<body><nav><a href="/chat" style="font-size:16px">문서 AI 어시스턴트</a>{nav}{who}</nav><main>{fl}{body}</main></body></html>')


LOGIN_HTML = ('<div class=card style="max-width:420px;margin:60px auto"><h1>로그인</h1>{err}<form method=post action=/login><input type=hidden name=next value="{next}">'
              '<label>아이디</label><input name=username style="width:100%;box-sizing:border-box"><label>비밀번호</label><input type=password name=password style="width:100%;box-sizing:border-box">'
              '<div style="margin-top:14px"><button class=btn>로그인</button></div></form><p class=muted>데모: admin/admin1234 · user1/user1234</p></div>')


def page_chat(con, user, conv_id, msg=None):
    convs = con.execute('SELECT * FROM convs WHERE user_id=? ORDER BY id DESC', (user['uid'],)).fetchall()
    left = f'<form method=post action=/chat/new><input type=hidden name=_csrf value="{user["csrf"]}"><button class="btn sm">+ 새 대화</button></form><div class=convs style="margin-top:10px">' + \
           ''.join(f'<a class="{"on" if c["id"] == conv_id else ""}" href="/chat/{c["id"]}">{esc(c["title"])}</a>' for c in convs) + '</div>'
    right = '<div class=card><p class=muted>왼쪽에서 대화를 고르거나 새 대화를 시작하세요.</p></div>'
    if conv_id:
        ms = con.execute('SELECT * FROM msgs WHERE conv_id=? ORDER BY id', (conv_id,)).fetchall()
        body = ''.join((f'<div class="msg me">{esc(m["content"])}</div>' if m['role'] == 'user' else
                        f'<div class="msg bot">{esc(m["content"])}<div class=src>{("출처: " + esc(m["source"])) if m["source"] else "<span class=no>근거 없음 → 거절</span>"} · {esc(m["mode"])}</div></div>') for m in ms)
        llm_on = setting(con, 'llm_enabled') == '1'
        right = (f'<div class=card style="min-height:420px">{body or "<p class=muted>질문을 입력하세요. 답변마다 출처 청크가 표시되고, 문서에 근거가 없으면 거절합니다.</p>"}</div>'
                 f'<form method=post action="/chat/{conv_id}/send" style="display:flex;gap:8px;margin-top:10px"><input type=hidden name=_csrf value="{user["csrf"]}">'
                 f'<input name=q style="flex:1" placeholder="문서에 대해 질문" autofocus><select name=mode><option value=extract>추출형(무료)</option>'
                 f'<option value=llm {"" if llm_on else "disabled"}>생성형{"" if llm_on else "(관리자 미승인)"}</option></select><button class=btn>보내기</button></form>')
    return layout('채팅', f'<div class=grid><div class=card>{left}</div><div>{right}</div></div>', user, msg)


def page_admin(con, user, msg=None, eval_res=None):
    docs = con.execute('SELECT * FROM docs ORDER BY id').fetchall()
    usage = con.execute('''SELECT u.username, COUNT(*) n, SUM(called) called, SUM(cached) cached, SUM(CASE WHEN refused IS NOT NULL THEN 1 ELSE 0 END) refused,
                           SUM(in_tokens) tin, SUM(out_tokens) tout, ROUND(SUM(usd),4) usd FROM usage g JOIN users u ON u.id=g.user_id GROUP BY u.username ORDER BY u.username''').fetchall()
    s = {k: setting(con, k) for k in DEFAULTS}
    body = (f'<h1>관리자</h1><div class=grid style="grid-template-columns:1fr 1fr"><div class=card><h3>문서 ({len(docs)})</h3><table><tr><th>이름</th><th>글자</th><th>청크</th><th>업로드</th></tr>'
            + ''.join(f'<tr><td>{esc(d["name"])}</td><td class=num>{d["chars"]:,}</td><td class=num>{d["chunks"]}</td><td class=muted>{d["uploaded"][:16]} · {esc(d["uploaded_by"])}</td></tr>' for d in docs)
            + f'</table><form method=post action=/admin/upload enctype=multipart/form-data style="margin-top:10px"><input type=hidden name=_csrf value="{user["csrf"]}"><input type=file name=file> <button class="btn sm">업로드(.md/.txt)</button></form></div>'
            f'<div class=card><h3>비용 게이트 설정</h3><form method=post action=/admin/settings><input type=hidden name=_csrf value="{user["csrf"]}">'
            f'<label>사용자별 일일 생성형 호출 상한</label><input name=daily_calls value="{esc(s["daily_calls"])}"><label>월 요금 상한(USD)</label><input name=monthly_usd_cap value="{esc(s["monthly_usd_cap"])}">'
            f'<label>거절 임계(최상위 유사도 미만이면 거절)</label><input name=refuse_th value="{esc(s["refuse_th"])}">'
            f'<label>생성형 활성화</label><select name=llm_enabled><option value=0 {"selected" if s["llm_enabled"] == "0" else ""}>끔(추출형만)</option><option value=1 {"selected" if s["llm_enabled"] == "1" else ""}>켬</option></select>'
            f'<label>시스템 프롬프트</label><textarea name=system_prompt rows=3 style="width:100%;box-sizing:border-box">{esc(s["system_prompt"])}</textarea>'
            f'<div style="margin-top:10px"><button class="btn sm">저장</button> <span class=muted>이번 달 누적 ${month_usd(con):.4f}</span></div></form></div></div>'
            f'<div class=card style="margin-top:16px"><h3>사용량 (사용자별)</h3><table><tr><th>사용자</th><th>요청</th><th>호출</th><th>캐시</th><th>거절</th><th>입력tok</th><th>출력tok(상한)</th><th>USD</th></tr>'
            + ''.join(f'<tr><td>{esc(r["username"])}</td><td class=num>{r["n"]}</td><td class=num>{r["called"]}</td><td class=num>{r["cached"]}</td><td class=num>{r["refused"]}</td><td class=num>{r["tin"]:,}</td><td class=num>{r["tout"]:,}</td><td class=num>{r["usd"]}</td></tr>' for r in usage)
            + f'</table><p class=muted>백엔드: {BACKEND["obj"].kind} · 호출 누계 {getattr(BACKEND["obj"], "calls", "-")}</p></div>'
            f'<div class=card style="margin-top:16px"><h3>평가</h3><form method=post action=/admin/eval enctype=multipart/form-data><input type=hidden name=_csrf value="{user["csrf"]}"><input type=file name=file> <button class="btn sm">평가셋 실행(JSON: eval[q,doc,keywords]·oos[])</button></form>'
            + (f'<p><b>hit@3 {eval_res["hit3"]}/{eval_res["n"]} ({eval_res["hit3_rate"]:.0%}) · 키워드 커버리지 {eval_res["coverage"]:.0%} · 범위밖 거절 {eval_res["oos_refused"]}/{eval_res["oos_n"]} ({eval_res["oos_refuse_rate"]:.0%})</b></p>'
               f'<p class=muted>캘리브레이션: 현재 임계 {eval_res["th"]} · 범위밖 최고점 {eval_res["oos_max"]} · 정상 최저점 {eval_res["pos_min"]} → ' + (f'제안 임계 <b>{eval_res["suggested_th"]}</b> (설정에서 적용)' if eval_res['suggested_th'] else '<b>겹침 — 문서/평가셋 보강 필요</b>') + '</p>'
               + '<table><tr><th>질문</th><th>hit@3</th><th>커버리지</th><th>답변</th></tr>' + ''.join(f'<tr><td>{esc(q)}</td><td>{"✔" if h else "✘"}</td><td class=num>{c}</td><td>{"답변" if f else "거절"}</td></tr>' for q, h, c, f in eval_res['detail']) + '</table>' if eval_res else '')
            + '</div>')
    return layout('관리자', body, user, msg)


# ── 서버 ───────────────────────────────────────────────────────────
def parse_upload(body, ctype):
    m = re.search(r'boundary=([^;]+)', ctype or '')
    if not m: return None, None, {}
    b = ('--' + m.group(1).strip('"')).encode(); fields = {}; fn = data = None
    for part in body.split(b)[1:]:
        if part.strip() in (b'', b'--'): continue
        head, _, dat = part.partition(b'\r\n\r\n'); dat = dat[:-2] if dat.endswith(b'\r\n') else dat
        f = re.search(rb'filename="([^"]*)"', head); n = re.search(rb'name="([^"]+)"', head)
        if f and fn is None: fn, data = f.group(1).decode('utf-8', 'replace'), dat
        elif n: fields[n.group(1).decode()] = dat.decode('utf-8', 'replace')
    return fn, data, fields


def serve(port, db_path=DB, quiet=True):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            if not quiet: super().log_message(*a)
        def _send(self, code, body, ctype='text/html; charset=utf-8', headers=()):
            data = body if isinstance(body, bytes) else body.encode(); self.send_response(code); self.send_header('Content-Type', ctype); self.send_header('Content-Length', str(len(data)))
            for k, v in headers: self.send_header(k, v)
            self.end_headers(); self.wfile.write(data)
        def _redirect(self, to, headers=()):
            self.send_response(302); self.send_header('Location', to)
            for k, v in headers: self.send_header(k, v)
            self.end_headers()
        def _cookie(self):
            m = re.search(r'(?:^|;\s*)sid=([0-9a-f]+)', self.headers.get('Cookie') or ''); return m.group(1) if m else None
        def _deny(self, user, admin=False):
            if not user: self._redirect('/login?next=' + urllib.parse.quote(self.path)); return True
            if admin and user['role'] != 'admin': self._send(403, layout('권한 없음', '<h1>403 · 관리자 전용</h1>', user)); return True
            return False
        def _own_conv(self, con, user, cid):
            c = con.execute('SELECT * FROM convs WHERE id=?', (cid,)).fetchone()
            if not c: self._send(404, layout('없음', '<h1>404</h1>', user)); return None
            if c['user_id'] != user['uid']: self._send(403, layout('권한 없음', '<h1>403 · 본인 대화만 볼 수 있습니다</h1>', user)); return None
            return c

        def do_GET(self):
            con = open_db(db_path)
            try:
                u = urllib.parse.urlparse(self.path); qs = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}; path = u.path.rstrip('/') or '/'
                user = load_session(con, self._cookie())
                if path == '/login': return self._send(200, layout('로그인', LOGIN_HTML.format(err='', next=esc(qs.get('next', '/chat')))))
                if self._deny(user): return
                if path in ('/', '/chat'): return self._send(200, page_chat(con, user, None, qs.get('msg')))
                m = re.match(r'^/chat/(\d+)$', path)
                if m:
                    c = self._own_conv(con, user, int(m.group(1)))
                    return self._send(200, page_chat(con, user, c['id'], qs.get('msg'))) if c else None
                if path == '/ask':                                            # JSON API (rag_audit 진단기 호환)
                    r = answer(con, user, qs.get('q', ''), qs.get('mode', 'extract')); r.pop('top', None)
                    return self._send(200, json.dumps(r, ensure_ascii=False), 'application/json; charset=utf-8')
                if path == '/admin':
                    if self._deny(user, admin=True): return
                    return self._send(200, page_admin(con, user, qs.get('msg')))
                return self._send(404, layout('없음', '<h1>404</h1>', user))
            finally: con.close()

        def do_POST(self):
            con = open_db(db_path)
            try:
                path = urllib.parse.urlparse(self.path).path.rstrip('/')
                n = int(self.headers.get('Content-Length') or 0); raw = self.rfile.read(n); ctype = self.headers.get('Content-Type') or ''
                if ctype.startswith('multipart/'): fn, data, form = parse_upload(raw, ctype)
                else: fn = data = None; form = {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode('utf-8', 'replace'), keep_blank_values=True).items()}
                user = load_session(con, self._cookie())
                if path == '/login':
                    u = check_login(con, form.get('username', ''), form.get('password', ''))
                    if not u: return self._send(200, layout('로그인', LOGIN_HTML.format(err='<div class="flash bad">아이디 또는 비밀번호가 올바르지 않습니다.</div>', next='/chat')))
                    nxt = form.get('next') or '/chat'
                    return self._redirect(nxt if nxt.startswith('/') else '/chat', [('Set-Cookie', f'sid={new_session(con, u["id"])}; HttpOnly; SameSite=Lax; Path=/')])
                if self._deny(user): return
                if not hmac.compare_digest(form.get('_csrf', ''), user['csrf']): return self._send(403, layout('거부', '<h1>403 · CSRF 토큰 불일치</h1>', user))
                if path == '/logout':
                    con.execute('DELETE FROM sessions WHERE token=?', (self._cookie(),)); con.commit(); return self._redirect('/login', [('Set-Cookie', 'sid=; Max-Age=0; Path=/')])
                if path == '/chat/new':
                    cur = con.execute('INSERT INTO convs(user_id, title, created) VALUES(?,?,?)', (user['uid'], f'대화 {now()[5:16]}', now())); con.commit()
                    return self._redirect(f'/chat/{cur.lastrowid}')
                m = re.match(r'^/chat/(\d+)/send$', path)
                if m:
                    c = self._own_conv(con, user, int(m.group(1)))
                    if not c: return
                    q = (form.get('q') or '').strip(); mode = form.get('mode', 'extract')
                    if not q: return self._redirect(f'/chat/{c["id"]}?msg=' + urllib.parse.quote('!질문이 비었습니다'))
                    if mode == 'llm' and setting(con, 'llm_enabled') != '1': mode = 'extract'
                    r = answer(con, user, q, mode)
                    con.execute('INSERT INTO msgs(conv_id, role, content, source, mode, ts) VALUES(?,?,?,?,?,?)', (c['id'], 'user', q, None, mode, now()))
                    con.execute('INSERT INTO msgs(conv_id, role, content, source, mode, ts) VALUES(?,?,?,?,?,?)', (c['id'], 'assistant', r['answer'], r.get('source') or None, mode + ('·캐시' if r.get('cached') else ''), now()))
                    if len(con.execute('SELECT 1 FROM msgs WHERE conv_id=?', (c['id'],)).fetchall()) <= 2: con.execute('UPDATE convs SET title=? WHERE id=?', (q[:24], c['id']))
                    con.commit(); return self._redirect(f'/chat/{c["id"]}')
                if self._deny(user, admin=True): return
                if path == '/admin/upload':
                    if not fn or not fn.lower().endswith(('.md', '.txt')): return self._redirect('/admin?msg=' + urllib.parse.quote('!.md/.txt 파일만'))
                    n = register_doc(con, fn, data.decode('utf-8', 'replace'), user['username']); return self._redirect('/admin?msg=' + urllib.parse.quote(f'업로드 완료 · 청크 {n}개 · 인덱스 재구축'))
                if path == '/admin/settings':
                    try:
                        vals = {'daily_calls': str(int(form['daily_calls'])), 'monthly_usd_cap': str(float(form['monthly_usd_cap'])), 'refuse_th': str(float(form.get('refuse_th', setting(con, 'refuse_th')))), 'llm_enabled': '1' if form.get('llm_enabled') == '1' else '0', 'system_prompt': form.get('system_prompt', '')[:2000]}
                    except (KeyError, ValueError): return self._redirect('/admin?msg=' + urllib.parse.quote('!설정값 형식 오류'))
                    for k, v in vals.items(): con.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (k, v))
                    con.commit(); return self._redirect('/admin?msg=' + urllib.parse.quote('설정 저장'))
                if path == '/admin/eval':
                    try: ev = json.loads(data.decode('utf-8', 'replace'))
                    except Exception: return self._redirect('/admin?msg=' + urllib.parse.quote('!평가셋 JSON 형식 오류'))
                    res = run_eval(get_index(), ev, int(setting(con, 'k')), float(setting(con, 'refuse_th')))
                    open(os.path.join(HERE, 'eval_report.md'), 'w', encoding='utf-8').write(f"# 평가 리포트 ({now()})\n- hit@3 {res['hit3']}/{res['n']} · 커버리지 {res['coverage']} · 범위밖 거절 {res['oos_refused']}/{res['oos_n']}\n" + ''.join(f'- {q} · hit {h} · cov {c} · {"답변" if f else "거절"}\n' for q, h, c, f in res['detail']))
                    return self._send(200, page_admin(con, user, '평가 완료 (eval_report.md 저장)', res))
                return self._send(404, layout('없음', '<h1>404</h1>', user))
            finally: con.close()

    srv = ThreadingHTTPServer(('127.0.0.1', port), H); srv.daemon_threads = True; return srv


# ── 데모 문서·평가셋 ────────────────────────────────────────────────
DEMO_DOCS = {
 '재택근무규정.md': '''# 재택근무 규정

재택근무는 주 2회까지 신청할 수 있으며, 전날 17시까지 팀장 승인을 받아야 합니다.

재택근무 중 근무시간은 09시부터 18시까지이며, 점심시간은 12시부터 13시까지입니다.

재택근무 장비는 회사 노트북만 사용하며 개인 PC 사용은 금지됩니다. VPN 접속은 필수입니다.

재택근무 일에 야근이 필요한 경우 사전에 팀장 승인을 받으면 야근 수당이 지급됩니다.

긴급 회의가 소집되면 30분 이내 화상회의 접속이 가능해야 합니다.''',
 '경비지출규정.md': '''# 경비 지출 규정

식대는 1인당 1만 원까지 지원하며 영수증을 제출해야 합니다. 주류는 지원하지 않습니다.

교통비는 대중교통 실비를 지원하고, 택시는 22시 이후 귀가 시에만 인정됩니다.

출장비는 국내 1박 기준 숙박 8만 원, 일비 3만 원을 지급합니다.

경비 청구는 지출일로부터 30일 이내에 시스템에 등록해야 하며, 기한 경과 시 지급되지 않습니다.

법인카드는 팀장 이상만 발급되며, 개인 용도 사용 시 회수됩니다.''',
 '휴가규정.md': '''# 휴가 규정

연차는 입사 첫해 11일, 이후 매년 15일이 부여되며 최대 25일까지 늘어납니다.

연차 신청은 3일 전까지 시스템에 등록하며, 반차는 오전·오후 단위로 사용할 수 있습니다.

경조사 휴가는 본인 결혼 5일, 배우자 출산 10일, 직계가족 사망 5일입니다.

병가는 연 10일까지 유급이며 3일 이상 연속 시 진단서를 제출합니다.

미사용 연차는 다음 해로 이월되지 않으며 연차수당으로 정산됩니다.''',
}
DEMO_EVAL = {'eval': [
    {'q': '재택근무 주 몇 회까지 가능한가요', 'doc': '재택근무규정', 'keywords': ['주 2회']},
    {'q': '식대 지원 한도가 얼마인가요', 'doc': '경비지출규정', 'keywords': ['1만 원']},
    {'q': '택시비는 언제 인정되나요', 'doc': '경비지출규정', 'keywords': ['22시']},
    {'q': '연차는 며칠 전까지 신청해야 하나요', 'doc': '휴가규정', 'keywords': ['3일']},
    {'q': '배우자 출산 휴가는 며칠인가요', 'doc': '휴가규정', 'keywords': ['10일']},
], 'oos': ['오늘 점심 메뉴 추천해줘', '비트코인 시세 알려줘', '파이썬 정렬 함수 예제']}
# 홀드아웃(캘리브레이션에 쓰지 않은 질문) — ③ 검증용
HELDOUT_POS = [('재택근무 장비는 어떤 걸 써야 하나요', '노트북'), ('경비 청구 기한이 어떻게 되나요', '30일'), ('병가는 며칠까지 유급인가요', '10일'), ('출장 일비는 얼마인가요', '3만 원'), ('긴급 회의는 몇 분 안에 접속해야 하나요', '30분')]
HELDOUT_OOS = ['오늘 서울 날씨 어때', '삼성전자 주식 사도 될까', '이 문장을 영어로 번역해줘']


class Client:
    def __init__(self, base):
        self.base = base; self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    def get(self, path):
        try: r = self.op.open(self.base + path, timeout=15); return r.status, r.geturl(), r.read().decode('utf-8', 'replace')
        except urllib.error.HTTPError as e: return e.code, e.geturl(), e.read().decode('utf-8', 'replace')
    def post(self, path, data=None, raw=None, ctype=None):
        req = urllib.request.Request(self.base + path, data=raw if raw is not None else urllib.parse.urlencode(data or {}).encode(), headers={'Content-Type': ctype or 'application/x-www-form-urlencoded'})
        try: r = self.op.open(req, timeout=15); return r.status, r.geturl(), r.read().decode('utf-8', 'replace')
        except urllib.error.HTTPError as e: return e.code, e.geturl(), e.read().decode('utf-8', 'replace')
    def login(self, u, p): return self.post('/login', {'username': u, 'password': p, 'next': '/chat'})
    def csrf(self):
        m = re.search(r'name=_csrf value="([0-9a-f]+)"', self.get('/chat')[2]); return m.group(1) if m else ''
    def ask(self, q, mode='extract'): return json.loads(self.get(f'/ask?q={urllib.parse.quote(q)}&mode={mode}')[2])


def multipart(fields, filename, data):
    b = 'XB' + secrets.token_hex(6); out = b''
    for k, v in fields.items(): out += f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    out += f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode() + data + f'\r\n--{b}--\r\n'.encode()
    return out, f'multipart/form-data; boundary={b}'


def main_demo():
    import shutil
    for f in (DB, DB + '-wal', DB + '-shm'):
        if os.path.exists(f): os.remove(f)
    shutil.rmtree(DOCS, ignore_errors=True); os.makedirs(DOCS)
    BACKEND['obj'] = DemoLLM(); BACKEND['cache'] = ai.Cache(os.path.join(HERE, '_demo_cache.json'))
    if os.path.exists(BACKEND['cache'].path): os.remove(BACKEND['cache'].path); BACKEND['cache'] = ai.Cache(BACKEND['cache'].path)
    con = open_db(DB)
    for u, p, r in (('admin', 'admin1234', 'admin'), ('user1', 'user1234', 'user'), ('user2', 'user2234', 'user')): add_user(con, u, p, r)
    con.execute("UPDATE settings SET value='1' WHERE key='llm_enabled'"); con.execute("UPDATE settings SET value='3' WHERE key='daily_calls'"); con.commit()
    get_index(rebuild=True)
    port = 8813; srv = serve(port, DB); threading.Thread(target=srv.serve_forever, daemon=True).start(); base = f'http://127.0.0.1:{port}'
    R = []; cnt = lambda t: con.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
    anon, admin, u1, u2 = Client(base), Client(base), Client(base), Client(base)
    # ① 인증·권한
    _, url, _ = anon.get('/chat'); admin.login('admin', 'admin1234'); u1.login('user1', 'user1234'); u2.login('user2', 'user2234')
    code_adm, _, _ = u1.get('/admin'); c1 = u1.csrf()
    body, ctype = multipart({'_csrf': c1}, 'x.md', '# 침입 문서\n\n내용'.encode()); code_up, _, _ = u1.post('/admin/upload', raw=body, ctype=ctype)
    R.append(('① 인증·권한: 미로그인 → 로그인 이동 · user의 관리자 페이지 403 · user의 문서 업로드 403·문서 0', '/login' in url and code_adm == 403 and code_up == 403 and cnt('docs') == 0))
    # ② 업로드 → 인덱스
    ca = admin.csrf(); expect_chunks = 0
    for name, text in DEMO_DOCS.items():
        body, ctype = multipart({'_csrf': ca}, name, text.encode()); admin.post('/admin/upload', raw=body, ctype=ctype); expect_chunks += len(chunk_text(name[:-3], text))
    idx = get_index(); ev = run_eval(idx, DEMO_EVAL, 3, float(setting(con, 'refuse_th')))
    R.append(('② 업로드 3건 → 인덱스 재구축: 청크 수 = 독립 계산 · 정답셋 hit@3 5/5', cnt('docs') == 3 and len(idx.chunks) == expect_chunks and ev['hit3'] == 5))
    # ②-b 캘리브레이션: 평가셋의 범위밖 최고점·정상 최저점 사이로 임계 제안 → 관리자 설정 경로로 적용
    cal_ok = ev['suggested_th'] is not None and ev['oos_max'] < ev['suggested_th'] < ev['pos_min']
    admin.post('/admin/settings', {'_csrf': ca, 'daily_calls': '3', 'monthly_usd_cap': '5.0', 'refuse_th': str(ev['suggested_th'] or REFUSE_TH), 'llm_enabled': '1', 'system_prompt': DEFAULTS['system_prompt']})
    applied = abs(float(setting(con, 'refuse_th')) - (ev['suggested_th'] or REFUSE_TH)) < 1e-9
    R.append((f'②-b 거절 임계 캘리브레이션: 범위밖 최고 {ev["oos_max"]} < 제안 {ev["suggested_th"]} < 정상 최저 {ev["pos_min"]} · 관리자 설정 경로로 적용', cal_ok and applied))
    # ③ 정직 거절 + 출처 — 홀드아웃(캘리브레이션 미사용 질문)
    oos = [u1.ask(q) for q in HELDOUT_OOS]; pos = [u1.ask(q) for q, _ in HELDOUT_POS]
    recall = sum(1 for r, (_, kw) in zip(pos, HELDOUT_POS) if r['found'] and r['source'] and kw in r['answer'])
    R.append((f'③ 정직 거절(홀드아웃): 범위밖 3문 전부 거절 = 오답 0 · 정상 5문 중 {recall}/5 정답 키워드+출처 (기준 ≥4, 의역은 거절 우선)', all(not r['found'] for r in oos) and recall >= 4))
    # ④ ★비용 게이트 (일일 상한 3)
    be = BACKEND['obj']; c_before = be.calls
    qs = ['재택근무 근무시간은', '출장비 숙박은 얼마', '병가는 며칠까지 유급', '법인카드는 누가 발급']
    rs = [u1.ask(q, 'llm') for q in qs]
    fourth_refused = 'refused' in rs[3] and be.calls - c_before == 3
    rc = u1.ask(qs[0], 'llm'); cache_hit = rc.get('cached') is True and be.calls - c_before == 3
    con.execute("UPDATE settings SET value='0.0000001' WHERE key='monthly_usd_cap'"); con.commit()
    rm = u2.ask('연차는 몇 일 부여되나', 'llm'); month_block = 'refused' in rm and '월 요금' in rm['refused'] and be.calls - c_before == 3
    con.execute("UPDATE settings SET value='5.0' WHERE key='monthly_usd_cap'"); con.commit()
    R.append(('④ ★비용 게이트: 일일 상한 3 → 4번째 거절·백엔드 호출 3 · 같은 질문 재요청 = 캐시(호출 +0) · 월 상한 초과 → 첫 호출부터 거절', all(r['found'] for r in rs[:3]) and fourth_refused and cache_hit and month_block))
    # ⑤ 사용량 회계 항등
    tot = con.execute("SELECT SUM(called), SUM(cached), SUM(CASE WHEN refused IS NOT NULL THEN 1 ELSE 0 END), COUNT(*) FROM usage").fetchone()
    _, _, ha = admin.get('/admin'); import re as _re
    shown_called = sum(int(x) for x in _re.findall(r'<tr><td>(?:admin|user1|user2)</td><td class=num>\d+</td><td class=num>(\d+)</td>', ha))
    R.append(('⑤ 사용량 회계 항등: Σ호출 = 백엔드 카운터 · 관리자 화면 사용자별 호출 합 = 테이블 합 · 거절 건도 0원으로 기록', tot[0] == be.calls - c_before and shown_called == tot[0] and tot[2] == 2))
    # ⑥ 대화 격리
    u1.post('/chat/new', {'_csrf': c1}); cid = con.execute("SELECT id FROM convs WHERE user_id=(SELECT id FROM users WHERE username='user1') ORDER BY id DESC LIMIT 1").fetchone()[0]
    for q in ['식대 한도', '택시비 인정 시간', '연차 신청 기한']: u1.post(f'/chat/{cid}/send', {'_csrf': c1, 'q': q, 'mode': 'extract'})
    c2 = u2.csrf(); code_peek, _, _ = u2.get(f'/chat/{cid}'); code_send, _, _ = u2.post(f'/chat/{cid}/send', {'_csrf': c2, 'q': '침입', 'mode': 'extract'})
    ms = [r['content'] for r in con.execute('SELECT content FROM msgs WHERE conv_id=? AND role="user" ORDER BY id', (cid,))]
    R.append(('⑥ 대화 격리: 타인 대화 열람 403 · 타인 대화에 전송 403 · 본인 메시지 3건 순서 보존', code_peek == 403 and code_send == 403 and ms == ['식대 한도', '택시비 인정 시간', '연차 신청 기한']))
    # ⑦ 평가 리포트
    body, ctype = multipart({'_csrf': ca}, 'eval.json', json.dumps(DEMO_EVAL, ensure_ascii=False).encode()); code_ev, _, he = admin.post('/admin/eval', raw=body, ctype=ctype)
    R.append(('⑦ 평가 리포트: 관리자 업로드 실행 → hit@3·범위밖 거절 수치 = 독립 재계산 일치 · eval_report.md 생성',
              code_ev == 200 and f'hit@3 {ev["hit3"]}/{ev["n"]}' in he and f'범위밖 거절 {ev["oos_refused"]}/{ev["oos_n"]}' in he and os.path.exists(os.path.join(HERE, 'eval_report.md'))))
    # ⑧ 재현성·무부작용
    a1 = u1.ask('식대 지원 한도가 얼마인가요'); a2 = u1.ask('식대 지원 한도가 얼마인가요'); u0 = cnt('usage'); h0 = hashlib.sha256(open(os.path.join(DOCS, '휴가규정.md'), 'rb').read()).hexdigest()
    for _ in range(10): u1.get('/chat'); admin.get('/admin'); u2.get(f'/chat')
    R.append(('⑧ 재현성·읽기 무부작용: 같은 질문 = 같은 답·출처 · 페이지 조회 30회 후 사용량·문서 불변', a1 == a2 and cnt('usage') == u0 and hashlib.sha256(open(os.path.join(DOCS, '휴가규정.md'), 'rb').read()).hexdigest() == h0))
    srv.shutdown()
    L = [f'# LLM 서비스 골격 검증 리포트 ({dt.datetime.now():%Y-%m-%d %H:%M})',
         '- 데모 = 로컬 서버 실구동(127.0.0.1:8813) · 문서 3건 업로드 · 사용자 3(관리자 1) · 생성형 백엔드 = 모의(호출 카운트·회계 검증용, 실서비스는 API 키 주입)',
         '', '| 검증 | 결과 |', '|---|---|'] + [f'| {k} | {"PASS" if v else "★FAIL"} |' for k, v in R] + [
         '', f'## 평가 실물 (데모 문서 3건 · 평가셋 5문 + 범위밖 3문 · 홀드아웃 5+3문)', f'- hit@3 {ev["hit3"]}/{ev["n"]} · 키워드 커버리지 {ev["coverage"]} · 범위밖 거절 {ev["oos_refused"]}/{ev["oos_n"]} · 캘리브레이션 임계 {REFUSE_TH} → {ev["suggested_th"]} (범위밖 최고 {ev["oos_max"]} / 정상 최저 {ev["pos_min"]})',
         '', '## 실물', '- 실행: python llm_app.py --serve → http://127.0.0.1:8813 (admin/admin1234 · user1/user1234)', '- 산출: llm_app.db · docs/ · eval_report.md',
         '', '- ※ 상한·모델·거절 임계·시스템 프롬프트 = 설정값. 데모 생성형 = 모의 백엔드(호출·회계·게이트 검증), 실서비스 = core/ai 4중 게이트 + ANTHROPIC_API_KEY + 관리자 승인.']
    open(os.path.join(HERE, 'llm_app_verify.md'), 'w', encoding='utf-8').write('\n'.join(L)); print('\n'.join(L)); con.close()
    return all(v for _, v in R)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    if '--serve' in sys.argv:
        port = int(sys.argv[sys.argv.index('--serve') + 1]) if len(sys.argv) > sys.argv.index('--serve') + 1 else 8813
        if not os.path.exists(DB):
            con = open_db(DB)
            for u, p, r in (('admin', 'admin1234', 'admin'), ('user1', 'user1234', 'user'), ('user2', 'user2234', 'user')): add_user(con, u, p, r)
            os.makedirs(DOCS, exist_ok=True)
            for n, t in DEMO_DOCS.items(): register_doc(con, n, t, 'demo')
            con.close(); print('데모 데이터 생성')
        BACKEND['cache'] = ai.Cache(os.path.join(HERE, '_cache.json')); get_index(rebuild=True)
        print(f'문서 AI: http://127.0.0.1:{port} (Ctrl+C 종료)'); serve(port, DB, quiet=False).serve_forever()
    else:
        sys.exit(0 if main_demo() else 1)
