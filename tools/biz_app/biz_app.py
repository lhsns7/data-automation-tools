#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""biz_app.py — 내부 업무 웹앱 골격: 로그인·권한·CRUD·검색·엑셀 가져오기/내보내기·감사 로그·동시 편집 보호 (2026-09-07)

"엑셀로 굴리던 업무를 웹 시스템으로" 유형(어드민·CRM·백오피스)의 뼈대. 표준 라이브러리 + SQLite, 외부 의존 0.
엔티티는 SCHEMA(설정) 한 곳에 선언 → 목록·검색·필터·페이징·등록·수정·삭제·엑셀 IO·감사 로그가 자동으로 붙는다.

본체 = 내부 시스템에서 사고가 나는 자리 4곳을 구조로 막는 것:
  ★권한: 역할(viewer/staff/admin)별 허용 동작 — URL을 직접 쳐도 403, 저장 0
  ★CSRF: 세션 토큰 없는 POST 거부
  ★동시 편집: 레코드 version으로 낙관적 잠금 — 나중에 저장하는 쪽이 조용히 덮어쓰지 못하고 409 충돌
  ★감사 로그: 누가·언제·무엇을·전→후 — 생성/수정/삭제/가져오기 전수 기록
비밀번호 = PBKDF2-HMAC-SHA256(20만 회, 솔트) — 평문 저장 0.

검증(main_demo, 로컬 서버 실구동):
  ①인증(미로그인 리다이렉트·오답 거부·평문 0) ②권한 우회 차단(viewer 생성·staff 삭제 → 403·저장 0, admin 삭제 성공)
  ③CSRF(토큰 없음/불일치 → 403·저장 0) ④★동시 편집(두 세션 같은 레코드 → 두 번째 409, 첫 값 보존)
  ⑤엑셀 왕복(내보내기=DB 전수, 가져오기 정상 5 저장·불량 3 격리 사유) ⑥감사 로그 전수(동작 수=로그 수, 전→후 정확)
  ⑦검색·필터·페이징(정답 카운트 대조) ⑧읽기 무부작용·재현성
실행: python biz_app.py --serve [포트]  (데모 계정 admin/admin1234 · staff/staff1234 · viewer/viewer1234)
※ 스키마·역할·페이지 크기는 설정값. 실서비스 = HTTPS 리버스 프록시(nginx 등) 뒤에 두고 계정 초기 비밀번호 교체.
"""
import os, sys, re, io, csv, json, html, hmac, hashlib, secrets, sqlite3, threading, datetime as dt
import urllib.parse, urllib.request, urllib.error, http.cookiejar

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', '..', 'core'))
from xlsx import write_workbook

DB = os.path.join(HERE, 'biz_app.db')
PAGE = 20
SESSION_HOURS = 8

# ── 설정: 엔티티 스키마 (name, label, type, required, extra) — type: str|text|int|money|date|enum|ref ──
SCHEMA = {
    'customers': dict(label='고객', fields=[
        ('name', '이름', 'str', True, None),
        ('phone', '연락처', 'str', True, None),
        ('grade', '등급', 'enum', True, ['일반', 'VIP', '휴면']),
        ('memo', '메모', 'text', False, None)]),
    'orders': dict(label='주문', fields=[
        ('customer_id', '고객', 'ref', True, 'customers'),
        ('product', '상품', 'str', True, None),
        ('qty', '수량', 'int', True, None),
        ('amount', '금액', 'money', True, None),
        ('status', '상태', 'enum', True, ['접수', '배송중', '완료', '취소']),
        ('ordered', '주문일', 'date', True, None)]),
}
ROLE_RANK = {'viewer': 1, 'staff': 2, 'admin': 3}
PERM = {'list': 'viewer', 'export': 'viewer', 'create': 'staff', 'update': 'staff', 'import': 'staff',
        'delete': 'admin', 'audit': 'admin', 'users': 'admin'}


# ── DB ──────────────────────────────────────────────────────────────
def open_db(path=DB):
    con = sqlite3.connect(path, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL,
                   pw_hash TEXT NOT NULL, salt TEXT NOT NULL, role TEXT NOT NULL, created TEXT NOT NULL)''')
    con.execute('''CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, user_id INT NOT NULL, csrf TEXT NOT NULL, expires TEXT NOT NULL)''')
    con.execute('''CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, user TEXT NOT NULL,
                   entity TEXT NOT NULL, rec_id INT, action TEXT NOT NULL, before TEXT, after TEXT)''')
    for ent, meta in SCHEMA.items():
        cols = ', '.join(f'{f[0]} {"INTEGER" if f[2] in ("int", "money", "ref") else "TEXT"}' for f in meta['fields'])
        con.execute(f'''CREATE TABLE IF NOT EXISTS {ent}(id INTEGER PRIMARY KEY AUTOINCREMENT, {cols},
                        version INTEGER NOT NULL DEFAULT 1, created TEXT NOT NULL, updated TEXT NOT NULL)''')
    con.commit()
    return con


def now():
    return dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def pw_hash(password, salt):
    return hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 200_000).hex()


def add_user(con, username, password, role):
    salt = secrets.token_hex(16)
    con.execute('INSERT INTO users(username, pw_hash, salt, role, created) VALUES(?,?,?,?,?)',
                (username, pw_hash(password, salt), salt, role, now()))
    con.commit()


def check_login(con, username, password):
    u = con.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
    if not u or not hmac.compare_digest(u['pw_hash'], pw_hash(password, u['salt'])):
        return None
    return u


def new_session(con, user_id):
    token, csrf = secrets.token_hex(32), secrets.token_hex(16)
    exp = (dt.datetime.now() + dt.timedelta(hours=SESSION_HOURS)).strftime('%Y-%m-%d %H:%M:%S')
    con.execute('INSERT INTO sessions VALUES(?,?,?,?)', (token, user_id, csrf, exp)); con.commit()
    return token


def load_session(con, token):
    if not token: return None
    s = con.execute('SELECT s.csrf, s.expires, u.username, u.role, u.id AS uid FROM sessions s JOIN users u ON u.id=s.user_id WHERE token=?',
                    (token,)).fetchone()
    if not s or s['expires'] < now(): return None
    return dict(csrf=s['csrf'], username=s['username'], role=s['role'], uid=s['uid'])


def allowed(role, action):
    return ROLE_RANK.get(role, 0) >= ROLE_RANK[PERM[action]]


def audit(con, user, entity, rec_id, action, before=None, after=None):
    con.execute('INSERT INTO audit(ts, user, entity, rec_id, action, before, after) VALUES(?,?,?,?,?,?,?)',
                (now(), user, entity, rec_id, action,
                 json.dumps(before, ensure_ascii=False) if before is not None else None,
                 json.dumps(after, ensure_ascii=False) if after is not None else None))


# ── 검증(스키마) ───────────────────────────────────────────────────
def validate(con, entity, form):
    """폼/가져오기 행 → (값 dict, 오류 문자열). 오류면 저장하지 않는다."""
    vals, errs = {}, []
    for name, label, typ, req, extra in SCHEMA[entity]['fields']:
        raw = (form.get(name) or '').strip() if not isinstance(form.get(name), (int, float)) else str(form.get(name))
        if raw == '' or raw is None:
            if req: errs.append(f'{label}: 필수')
            vals[name] = None; continue
        try:
            if typ == 'int':
                vals[name] = int(raw.replace(',', ''))
            elif typ == 'money':
                v = int(float(raw.replace(',', '').replace('원', '')))
                if v < 0: raise ValueError
                vals[name] = v
            elif typ == 'date':
                vals[name] = dt.datetime.strptime(raw[:10], '%Y-%m-%d').strftime('%Y-%m-%d')
            elif typ == 'enum':
                if raw not in extra: raise KeyError(f'허용값 {"/".join(extra)} 밖')
                vals[name] = raw
            elif typ == 'ref':
                try: rid = int(raw)
                except ValueError: raise KeyError('존재하지 않는 참조')
                if not con.execute(f'SELECT 1 FROM {extra} WHERE id=?', (rid,)).fetchone(): raise KeyError('존재하지 않는 참조')
                vals[name] = rid
            else:
                vals[name] = raw
        except KeyError as e:
            errs.append(f'{label}: {e.args[0]} ("{raw}")')
        except (ValueError, TypeError):
            errs.append(f'{label}: 형식 오류 ("{raw}")')
    return vals, ' · '.join(errs)


def rec_dict(row, entity):
    return {f[0]: row[f[0]] for f in SCHEMA[entity]['fields']}


# ── CRUD ───────────────────────────────────────────────────────────
def create(con, entity, vals, user):
    names = [f[0] for f in SCHEMA[entity]['fields']]
    cur = con.execute(f'INSERT INTO {entity}({",".join(names)}, created, updated) VALUES({",".join("?" * len(names))},?,?)',
                      [vals[n] for n in names] + [now(), now()])
    audit(con, user, entity, cur.lastrowid, 'create', None, vals); con.commit()
    return cur.lastrowid


def update(con, entity, rec_id, vals, version, user):
    """낙관적 잠금: version 일치할 때만 저장. 반환 True/False(충돌)"""
    before = con.execute(f'SELECT * FROM {entity} WHERE id=?', (rec_id,)).fetchone()
    if not before: return False
    names = [f[0] for f in SCHEMA[entity]['fields']]
    cur = con.execute(f'UPDATE {entity} SET {",".join(n + "=?" for n in names)}, version=version+1, updated=? WHERE id=? AND version=?',
                      [vals[n] for n in names] + [now(), rec_id, version])
    if cur.rowcount == 0:
        con.rollback(); return False
    audit(con, user, entity, rec_id, 'update', rec_dict(before, entity), vals); con.commit()
    return True


def referenced_by(con, entity, rec_id):
    """이 레코드를 ref로 가리키는 다른 엔티티 건수 [(라벨, n)] — 참조 중이면 삭제 거부(고아 레코드 방지)"""
    out = []
    for ent, meta in SCHEMA.items():
        for name, label, typ, req, extra in meta['fields']:
            if typ == 'ref' and extra == entity:
                n = con.execute(f'SELECT COUNT(*) FROM {ent} WHERE {name}=?', (rec_id,)).fetchone()[0]
                if n: out.append((meta['label'], n))
    return out


def delete(con, entity, rec_id, user):
    """반환 (성공, 사유). 참조 중인 레코드는 지우지 않는다."""
    before = con.execute(f'SELECT * FROM {entity} WHERE id=?', (rec_id,)).fetchone()
    if not before: return False, '없는 레코드'
    refs = referenced_by(con, entity, rec_id)
    if refs: return False, '삭제 불가: ' + ', '.join(f'{l} {n}건' for l, n in refs) + '이(가) 참조 중'
    con.execute(f'DELETE FROM {entity} WHERE id=?', (rec_id,))
    audit(con, user, entity, rec_id, 'delete', rec_dict(before, entity), None); con.commit()
    return True, ''


def query(con, entity, q='', flt='', page=1):
    fields = SCHEMA[entity]['fields']
    where, args = [], []
    txt = [f[0] for f in fields if f[2] in ('str', 'text')]
    if q and txt:
        where.append('(' + ' OR '.join(f'{n} LIKE ?' for n in txt) + ')'); args += [f'%{q}%'] * len(txt)
    enum = next((f for f in fields if f[2] == 'enum'), None)
    if flt and enum:
        where.append(f'{enum[0]}=?'); args.append(flt)
    w = (' WHERE ' + ' AND '.join(where)) if where else ''
    total = con.execute(f'SELECT COUNT(*) FROM {entity}{w}', args).fetchone()[0]
    rows = con.execute(f'SELECT * FROM {entity}{w} ORDER BY id DESC LIMIT ? OFFSET ?', args + [PAGE, (page - 1) * PAGE]).fetchall()
    return rows, total, enum


def ref_names(con, entity):
    """ref 필드 표시용 {필드: {id: 표시명}}"""
    out = {}
    for name, label, typ, req, extra in SCHEMA[entity]['fields']:
        if typ == 'ref':
            first = SCHEMA[extra]['fields'][0][0]
            out[name] = {r['id']: r[first] for r in con.execute(f'SELECT id, {first} FROM {extra}')}
    return out


# ── 엑셀 IO ────────────────────────────────────────────────────────
def export_xlsx(con, entity, path):
    fields = SCHEMA[entity]['fields']; refs = ref_names(con, entity)
    rows = []
    for r in con.execute(f'SELECT * FROM {entity} ORDER BY id'):
        row = [r['id']]
        for name, label, typ, req, extra in fields:
            v = r[name]
            row.append(refs[name].get(v, v) if typ == 'ref' else v)
        rows.append(row + [r['version'], r['updated']])
    header = ['ID'] + [f[1] for f in fields] + ['버전', '수정 시각']
    write_workbook(path, {SCHEMA[entity]['label']: (header, rows)},
                   summary={'생성': now(), '건수': f'{len(rows)}건', '엔티티': entity})
    return rows


def parse_upload(body, ctype):
    """multipart/form-data에서 첫 파일 part → (filename, bytes). cgi 모듈 없이(3.13 제거) 직접 파싱."""
    m = re.search(r'boundary=([^;]+)', ctype or '')
    if not m: return None, None
    b = ('--' + m.group(1).strip('"')).encode()
    for part in body.split(b)[1:]:
        if part.strip() in (b'', b'--'): continue
        head, _, data = part.partition(b'\r\n\r\n')
        fn = re.search(rb'filename="([^"]*)"', head)
        if fn:
            return fn.group(1).decode('utf-8', 'replace'), data.rstrip(b'\r\n--').rstrip(b'\r\n')
    return None, None


def read_rows(filename, data):
    """xlsx/csv → [dict(라벨→값)] (첫 행 = 헤더)"""
    if filename.lower().endswith('.xlsx'):
        import openpyxl
        ws = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
        ws = ws[ws.sheetnames[-1]] if '요약' in ws.sheetnames[0] else ws[ws.sheetnames[0]]
        it = ws.iter_rows(values_only=True)
        header = [str(h).strip() if h is not None else '' for h in next(it)]
        rows = [dict(zip(header, ['' if v is None else (v.strftime('%Y-%m-%d') if hasattr(v, 'strftime') else str(v)) for v in r])) for r in it if any(v not in (None, '') for v in r)]
    else:
        txt = data.decode('utf-8-sig', 'replace')
        rows = list(csv.DictReader(io.StringIO(txt)))
    return rows


def import_rows(con, entity, rows, user):
    """라벨 헤더 행 → 스키마 검증 → 정상만 저장, 불량은 (행번호, 사유) 격리. 반환 (저장수, [(행, 사유)])"""
    fields = SCHEMA[entity]['fields']; label2name = {f[1]: f[0] for f in fields}
    refs = {f[0]: {v: k for k, v in ref_names(con, entity)[f[0]].items()} for f in fields if f[2] == 'ref'}
    saved, bad = 0, []
    for i, r in enumerate(rows, 2):
        form = {}
        for lab, val in r.items():
            n = label2name.get((lab or '').strip())
            if not n: continue
            if n in refs and val and not str(val).strip().isdigit():
                val = refs[n].get(str(val).strip(), val)                       # 표시명 → id
            form[n] = '' if val is None else str(val)
        vals, err = validate(con, entity, form)
        if err: bad.append((i, err)); continue
        create(con, entity, vals, user); saved += 1
    if saved: audit(con, user, entity, None, 'import', None, {'saved': saved, 'rejected': len(bad)}); con.commit()
    return saved, bad


# ── HTML ───────────────────────────────────────────────────────────
CSS = """body{font-family:'Malgun Gothic',system-ui,sans-serif;margin:0;background:#f3f5f9;color:#1c2028}
nav{background:#1f2937;color:#fff;padding:10px 22px;display:flex;gap:18px;align-items:center}nav a{color:#e5e7eb;text-decoration:none;font-weight:700}
nav .u{margin-left:auto;font-size:13px;color:#cbd5e1}main{max-width:1100px;margin:22px auto;padding:0 16px}
h1{font-size:20px;margin:0 0 12px}.bar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:12px}
input,select,textarea{padding:8px 10px;border:1px solid #cfd6de;border-radius:8px;font-size:14px;font-family:inherit}
.btn{padding:8px 14px;border:0;border-radius:8px;background:#2563eb;color:#fff;font-weight:700;cursor:pointer;text-decoration:none;font-size:14px;display:inline-block}
.btn.gray{background:#64748b}.btn.red{background:#b91c1c}.btn.sm{padding:5px 10px;font-size:13px}
table{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e2e6ec;border-radius:10px;overflow:hidden}
th{background:#eef2f7;text-align:left;font-size:13px;padding:9px 10px;border-bottom:1px solid #e2e6ec}td{padding:8px 10px;border-bottom:1px solid #eef0f4;font-size:14px}
td.num{text-align:right;font-variant-numeric:tabular-nums}.tag{padding:2px 8px;border-radius:999px;font-size:12px;background:#e0e7ff;color:#3730a3;font-weight:700}
.msg{padding:10px 14px;border-radius:8px;margin-bottom:12px;font-weight:700}.ok{background:#dcfce7;color:#166534}.no{background:#fee2e2;color:#991b1b}
.card{background:#fff;border:1px solid #e2e6ec;border-radius:12px;padding:20px;max-width:560px}label{display:block;font-size:13px;font-weight:700;margin:12px 0 5px}
.card input,.card select,.card textarea{width:100%;box-sizing:border-box}.pg a{margin-right:8px}.muted{color:#6b7280;font-size:13px}"""


def esc(v):
    return html.escape('' if v is None else str(v))


def layout(title, body, user=None, msg=None):
    nav = ''.join(f'<a href="/{e}">{esc(m["label"])}</a>' for e, m in SCHEMA.items())
    if user and allowed(user['role'], 'audit'): nav += '<a href="/audit">감사 로그</a><a href="/users">사용자</a>'
    who = (f'<span class=u>{esc(user["username"])} · {esc(user["role"])} '
           f'<form method=post action=/logout style="display:inline"><input type=hidden name=_csrf value="{user["csrf"]}">'
           f'<button class="btn gray sm">로그아웃</button></form></span>') if user else ''
    m = ''
    if msg:
        kind = 'no' if msg.startswith('!') else 'ok'
        m = f'<div class="msg {kind}">{esc(msg.lstrip("!"))}</div>'
    return (f'<!doctype html><html lang=ko><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">'
            f'<title>{esc(title)} · 업무 시스템</title><style>{CSS}</style></head><body>'
            f'<nav><a href="/" style="font-size:16px">업무 시스템</a>{nav}{who}</nav><main>{m}{body}</main></body></html>')


def fmt(v, typ, refs=None, name=None):
    if v is None: return ''
    if typ == 'money': return f'{int(v):,}'
    if typ == 'ref' and refs: return refs.get(name, {}).get(v, v)
    if typ == 'enum': return f'<span class=tag>{esc(v)}</span>'
    return esc(v)


def page_list(con, entity, user, q, flt, page, msg):
    meta = SCHEMA[entity]; rows, total, enum = query(con, entity, q, flt, page); refs = ref_names(con, entity)
    pages = max(1, (total + PAGE - 1) // PAGE)
    opts = ''.join(f'<option {"selected" if flt == o else ""}>{esc(o)}</option>' for o in (enum[4] if enum else []))
    bar = (f'<form class=bar method=get><input name=q value="{esc(q)}" placeholder="검색">'
           + (f'<select name=f><option value="">전체 {esc(enum[1])}</option>{opts}</select>' if enum else '')
           + f'<button class="btn gray">조회</button><span class=muted>총 {total:,}건 · {page}/{pages}쪽</span></form>')
    acts = f'<a class=btn href="/{entity}/export.xlsx">엑셀 내보내기</a> '
    if allowed(user['role'], 'create'): acts = f'<a class=btn href="/{entity}/new">+ {esc(meta["label"])} 등록</a> ' + acts
    if allowed(user['role'], 'import'): acts += f'<a class="btn gray" href="/{entity}/import">엑셀 가져오기</a>'
    th = ''.join(f'<th>{esc(f[1])}</th>' for f in meta['fields'])
    trs = ''
    for r in rows:
        tds = ''.join(f'<td class="{"num" if f[2] in ("int", "money") else ""}">{fmt(r[f[0]], f[2], refs, f[0])}</td>' for f in meta['fields'])
        a = ''
        if allowed(user['role'], 'update'): a += f'<a class="btn gray sm" href="/{entity}/{r["id"]}/edit">편집</a> '
        if allowed(user['role'], 'delete'):
            a += (f'<form method=post action="/{entity}/{r["id"]}/delete" style="display:inline" onsubmit="return confirm(\'삭제할까요?\')">'
                  f'<input type=hidden name=_csrf value="{user["csrf"]}"><button class="btn red sm">삭제</button></form>')
        trs += f'<tr><td class=num>{r["id"]}</td>{tds}<td>{a}</td></tr>'
    pg = ' '.join(f'<a href="?q={urllib.parse.quote(q)}&f={urllib.parse.quote(flt)}&page={p}">{"[" + str(p) + "]" if p == page else p}</a>'
                  for p in range(1, pages + 1))
    body = (f'<h1>{esc(meta["label"])} 관리</h1>{bar}<div class=bar>{acts}</div>'
            f'<table><tr><th>ID</th>{th}<th></th></tr>{trs or "<tr><td colspan=99 class=muted>데이터 없음</td></tr>"}</table><p class=pg>{pg}</p>')
    return layout(meta['label'], body, user, msg)


def page_form(con, entity, user, rec=None, form=None, err=''):
    meta = SCHEMA[entity]; form = form or (rec_dict(rec, entity) if rec else {})
    inputs = ''
    for name, label, typ, req, extra in meta['fields']:
        v = form.get(name); v = '' if v is None else v
        if typ == 'enum':
            o = ''.join(f'<option {"selected" if str(v) == x else ""}>{esc(x)}</option>' for x in extra)
            inp = f'<select name={name}>{o}</select>'
        elif typ == 'ref':
            first = SCHEMA[extra]['fields'][0][0]
            o = ''.join(f'<option value={r["id"]} {"selected" if str(v) == str(r["id"]) else ""}>{esc(r[first])}</option>'
                        for r in con.execute(f'SELECT id, {first} FROM {extra} ORDER BY {first}'))
            inp = f'<select name={name}><option value="">선택</option>{o}</select>'
        elif typ == 'text':
            inp = f'<textarea name={name} rows=3>{esc(v)}</textarea>'
        elif typ == 'date':
            inp = f'<input type=date name={name} value="{esc(v)}">'
        else:
            inp = f'<input name={name} value="{esc(v)}">'
        inputs += f'<label>{esc(label)}{" *" if req else ""}</label>{inp}'
    ver = f'<input type=hidden name=version value="{rec["version"]}">' if rec else ''
    body = (f'<h1>{esc(meta["label"])} {"수정" if rec else "등록"}</h1><div class=card><form method=post>'
            f'<input type=hidden name=_csrf value="{user["csrf"]}">{ver}{inputs}'
            f'<div class=bar style="margin-top:16px"><button class=btn>저장</button><a class="btn gray" href="/{entity}">취소</a></div></form></div>')
    return layout(meta['label'], body, user, ('!' + err) if err else None)


# ── 서버 ───────────────────────────────────────────────────────────
def serve(port, db_path=DB, quiet=True):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            if not quiet: super().log_message(*a)

        # helpers
        def _send(self, code, body, ctype='text/html; charset=utf-8', headers=()):
            data = body if isinstance(body, bytes) else body.encode()
            self.send_response(code); self.send_header('Content-Type', ctype); self.send_header('Content-Length', str(len(data)))
            for k, v in headers: self.send_header(k, v)
            self.end_headers(); self.wfile.write(data)

        def _redirect(self, to, headers=()):
            self.send_response(302); self.send_header('Location', to)
            for k, v in headers: self.send_header(k, v)
            self.end_headers()

        def _cookie(self):
            c = self.headers.get('Cookie') or ''
            m = re.search(r'(?:^|;\s*)sid=([0-9a-f]+)', c)
            return m.group(1) if m else None

        def _form(self):
            n = int(self.headers.get('Content-Length') or 0); raw = self.rfile.read(n)
            ctype = self.headers.get('Content-Type') or ''
            if ctype.startswith('multipart/'):
                return {}, raw, ctype
            return {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode('utf-8', 'replace'), keep_blank_values=True).items()}, raw, ctype

        def _user(self, con):
            return load_session(con, self._cookie())

        def _deny(self, user, action, con):
            if not user:
                self._redirect('/login?next=' + urllib.parse.quote(self.path)); return True
            if not allowed(user['role'], action):
                self._send(403, layout('권한 없음', f'<h1>403 · 권한 없음</h1><p>이 동작({esc(action)})은 {esc(PERM[action])} 이상만 가능합니다.</p>', user)); return True
            return False

        def do_GET(self):
            con = open_db(db_path)
            try:
                u = urllib.parse.urlparse(self.path); qs = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
                path = u.path.rstrip('/') or '/'
                user = self._user(con)
                if path == '/login':
                    return self._send(200, layout('로그인', LOGIN_HTML.format(err='', next=esc(qs.get('next', '/')))))
                if path == '/':
                    if not user: return self._redirect('/login')
                    return self._redirect('/' + next(iter(SCHEMA)))
                if path == '/audit':
                    if self._deny(user, 'audit', con): return
                    trs = ''.join(f'<tr><td>{r["ts"]}</td><td>{esc(r["user"])}</td><td>{esc(r["entity"])}</td><td class=num>{r["rec_id"] or ""}</td>'
                                  f'<td><span class=tag>{r["action"]}</span></td><td class=muted>{esc(r["before"] or "")}</td><td class=muted>{esc(r["after"] or "")}</td></tr>'
                                  for r in con.execute('SELECT * FROM audit ORDER BY id DESC LIMIT 200'))
                    return self._send(200, layout('감사 로그', f'<h1>감사 로그 (최근 200)</h1><table><tr><th>시각</th><th>사용자</th><th>대상</th><th>ID</th><th>동작</th><th>전</th><th>후</th></tr>{trs}</table>', user))
                if path == '/users':
                    if self._deny(user, 'users', con): return
                    trs = ''.join(f'<tr><td>{esc(r["username"])}</td><td><span class=tag>{esc(r["role"])}</span></td><td>{r["created"]}</td></tr>'
                                  for r in con.execute('SELECT * FROM users ORDER BY id'))
                    form = (f'<div class=card><form method=post action=/users><input type=hidden name=_csrf value="{user["csrf"]}">'
                            f'<label>아이디</label><input name=username><label>비밀번호</label><input type=password name=password>'
                            f'<label>역할</label><select name=role><option>viewer</option><option>staff</option><option>admin</option></select>'
                            f'<div class=bar style="margin-top:14px"><button class=btn>사용자 추가</button></div></form></div>')
                    return self._send(200, layout('사용자', f'<h1>사용자</h1><table><tr><th>아이디</th><th>역할</th><th>생성</th></tr>{trs}</table><br>{form}', user))
                m = re.match(r'^/(\w+)(?:/(new|import|export\.xlsx|(\d+)/edit))?$', path)
                if not m or m.group(1) not in SCHEMA:
                    return self._send(404, layout('없음', '<h1>404</h1>', user))
                ent, sub, rid = m.group(1), m.group(2), m.group(3)
                if sub is None:
                    if self._deny(user, 'list', con): return
                    return self._send(200, page_list(con, ent, user, qs.get('q', ''), qs.get('f', ''), max(1, int(qs.get('page', 1) or 1)), qs.get('msg')))
                if sub == 'new':
                    if self._deny(user, 'create', con): return
                    return self._send(200, page_form(con, ent, user))
                if sub == 'import':
                    if self._deny(user, 'import', con): return
                    body = (f'<h1>{esc(SCHEMA[ent]["label"])} 엑셀 가져오기</h1><div class=card><form method=post enctype=multipart/form-data>'
                            f'<input type=hidden name=_csrf value="{user["csrf"]}"><label>파일(.xlsx/.csv, 첫 행 = 열 이름)</label><input type=file name=file>'
                            f'<p class=muted>열 이름은 내보내기 파일과 동일하게: {", ".join(esc(f[1]) for f in SCHEMA[ent]["fields"])}. 불량 행은 저장하지 않고 사유를 보여줍니다.</p>'
                            f'<div class=bar><button class=btn>가져오기</button></div></form></div>')
                    return self._send(200, layout('가져오기', body, user))
                if sub == 'export.xlsx':
                    if self._deny(user, 'export', con): return
                    tmp = os.path.join(HERE, f'_export_{ent}_{secrets.token_hex(4)}.xlsx')
                    export_xlsx(con, ent, tmp); data = open(tmp, 'rb').read(); os.remove(tmp)
                    return self._send(200, data, 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                                      [('Content-Disposition', f'attachment; filename="{ent}.xlsx"')])
                if rid:
                    if self._deny(user, 'update', con): return
                    rec = con.execute(f'SELECT * FROM {ent} WHERE id=?', (int(rid),)).fetchone()
                    if not rec: return self._send(404, layout('없음', '<h1>404</h1>', user))
                    return self._send(200, page_form(con, ent, user, rec))
            finally:
                con.close()

        def do_POST(self):
            con = open_db(db_path)
            try:
                path = urllib.parse.urlparse(self.path).path.rstrip('/')
                form, raw, ctype = self._form()
                user = self._user(con)
                if path == '/login':
                    u = check_login(con, form.get('username', ''), form.get('password', ''))
                    if not u:
                        return self._send(200, layout('로그인', LOGIN_HTML.format(err='<div class="msg no">아이디 또는 비밀번호가 올바르지 않습니다.</div>', next='/')))
                    tok = new_session(con, u['id'])
                    nxt = form.get('next') or '/'
                    return self._redirect(nxt if nxt.startswith('/') else '/', [('Set-Cookie', f'sid={tok}; HttpOnly; SameSite=Lax; Path=/')])
                if not user:
                    return self._redirect('/login')
                # CSRF: multipart는 본문에서 토큰 추출
                if ctype.startswith('multipart/'):
                    mc = re.search(rb'name="_csrf"\r\n\r\n([0-9a-f]+)', raw); tok = mc.group(1).decode() if mc else ''
                else:
                    tok = form.get('_csrf', '')
                if not hmac.compare_digest(tok, user['csrf']):
                    return self._send(403, layout('거부', '<h1>403 · CSRF 토큰 불일치</h1><p>폼을 다시 열어 제출해 주세요.</p>', user))
                if path == '/logout':
                    con.execute('DELETE FROM sessions WHERE token=?', (self._cookie(),)); con.commit()
                    return self._redirect('/login', [('Set-Cookie', 'sid=; Max-Age=0; Path=/')])
                if path == '/users':
                    if self._deny(user, 'users', con): return
                    try:
                        if form.get('role') not in ROLE_RANK or not form.get('username') or len(form.get('password', '')) < 8: raise ValueError
                        add_user(con, form['username'], form['password'], form['role'])
                        return self._redirect('/users')
                    except (ValueError, sqlite3.IntegrityError):
                        return self._send(400, layout('오류', '<h1>사용자 추가 실패</h1><p>아이디 중복·역할·비밀번호(8자 이상)를 확인하세요.</p>', user))
                m = re.match(r'^/(\w+)/(new|import|(\d+)/(edit|delete))$', path)
                if not m or m.group(1) not in SCHEMA:
                    return self._send(404, layout('없음', '<h1>404</h1>', user))
                ent, sub, rid, act = m.group(1), m.group(2), m.group(3), m.group(4)
                if sub == 'new':
                    if self._deny(user, 'create', con): return
                    vals, err = validate(con, ent, form)
                    if err: return self._send(400, page_form(con, ent, user, None, form, err))
                    create(con, ent, vals, user['username'])
                    return self._redirect(f'/{ent}?msg=' + urllib.parse.quote('등록했습니다'))
                if sub == 'import':
                    if self._deny(user, 'import', con): return
                    fn, data = parse_upload(raw, ctype)
                    if not fn: return self._send(400, layout('오류', '<h1>파일이 없습니다</h1>', user))
                    saved, bad = import_rows(con, ent, read_rows(fn, data), user['username'])
                    trs = ''.join(f'<tr><td class=num>{r}</td><td>{esc(why)}</td></tr>' for r, why in bad)
                    body = (f'<h1>가져오기 결과</h1><div class="msg {"ok" if not bad else "no"}">저장 {saved}건 · 격리(미저장) {len(bad)}건</div>'
                            + (f'<table><tr><th>행</th><th>사유</th></tr>{trs}</table>' if bad else '') + f'<p><a class=btn href="/{ent}">목록으로</a></p>')
                    return self._send(200, layout('가져오기 결과', body, user))
                rid = int(rid)
                if act == 'delete':
                    if self._deny(user, 'delete', con): return
                    ok, why = delete(con, ent, rid, user['username'])
                    return self._redirect(f'/{ent}?msg=' + urllib.parse.quote('삭제했습니다' if ok else '!' + why))
                if act == 'edit':
                    if self._deny(user, 'update', con): return
                    vals, err = validate(con, ent, form)
                    rec = con.execute(f'SELECT * FROM {ent} WHERE id=?', (rid,)).fetchone()
                    if err: return self._send(400, page_form(con, ent, user, rec, form, err))
                    try: ver = int(form.get('version', 0))
                    except ValueError: ver = 0
                    if not update(con, ent, rid, vals, ver, user['username']):
                        cur = con.execute(f'SELECT * FROM {ent} WHERE id=?', (rid,)).fetchone()
                        body = (f'<h1>409 · 동시 편집 충돌</h1><div class="msg no">다른 사용자가 먼저 저장했습니다(현재 버전 {cur["version"] if cur else "-"}). '
                                f'아래 최신 값을 확인하고 다시 편집하세요.</div><p>{esc(json.dumps(rec_dict(cur, ent), ensure_ascii=False) if cur else "삭제됨")}</p>'
                                f'<p><a class=btn href="/{ent}/{rid}/edit">최신 값으로 다시 편집</a></p>')
                        return self._send(409, layout('충돌', body, user))
                    return self._redirect(f'/{ent}?msg=' + urllib.parse.quote('저장했습니다'))
            finally:
                con.close()

    srv = ThreadingHTTPServer(('127.0.0.1', port), H)
    srv.daemon_threads = True
    return srv


LOGIN_HTML = ('<div class=card style="margin:60px auto"><h1>로그인</h1>{err}<form method=post action=/login>'
              '<input type=hidden name=next value="{next}"><label>아이디</label><input name=username autofocus>'
              '<label>비밀번호</label><input type=password name=password><div class=bar style="margin-top:16px"><button class=btn>로그인</button></div></form>'
              '<p class=muted>데모 계정: admin / staff / viewer (비밀번호 = 아이디+1234)</p></div>')


# ── 데모 데이터 ────────────────────────────────────────────────────
def seed(con):
    for u, r in (('admin', 'admin'), ('staff', 'staff'), ('viewer', 'viewer')):
        add_user(con, u, u + '1234', r)
    import random
    rnd = random.Random(7)
    last = ['김', '이', '박', '최', '정', '강', '조', '윤', '장', '임']; first = ['민준', '서연', '지우', '하은', '도윤', '수아', '예준', '지민', '시우', '하린']
    for i in range(30):
        vals = dict(name=last[i % 10] + first[(i * 3) % 10], phone=f'010-{1000 + i * 37:04d}-{2000 + i * 53:04d}',
                    grade=['일반', 'VIP', '휴면'][i % 3], memo='' if i % 4 else '단골')
        create(con, 'customers', vals, 'seed')
    prods = ['비타민', '유산균', '텀블러', '마스크', '커피']
    for i in range(45):
        vals = dict(customer_id=1 + (i * 7) % 30, product=prods[i % 5], qty=1 + i % 4, amount=(1 + i % 4) * [12000, 24000, 15000, 9000, 18000][i % 5],
                    status=['접수', '배송중', '완료', '취소'][i % 4], ordered=(dt.date(2026, 9, 1) + dt.timedelta(days=i % 7)).isoformat())
        create(con, 'orders', vals, 'seed')
    con.execute("DELETE FROM audit WHERE user='seed'"); con.commit()      # 시드는 감사 대상 아님(검증 카운트 정확성)


# ── 검증 ───────────────────────────────────────────────────────────
class Client:
    """세션(쿠키)별 HTTP 클라이언트. 리다이렉트는 따라가되 최종 URL·코드 기록."""
    def __init__(self, base):
        self.base = base; self.cj = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cj))

    def get(self, path):
        try:
            r = self.op.open(self.base + path, timeout=10); return r.status, r.geturl(), r.read().decode('utf-8', 'replace')
        except urllib.error.HTTPError as e:
            return e.code, e.geturl(), e.read().decode('utf-8', 'replace')

    def post(self, path, data=None, raw=None, ctype=None):
        body = raw if raw is not None else urllib.parse.urlencode(data or {}).encode()
        req = urllib.request.Request(self.base + path, data=body, headers={'Content-Type': ctype or 'application/x-www-form-urlencoded'})
        try:
            r = self.op.open(req, timeout=10); return r.status, r.geturl(), r.read().decode('utf-8', 'replace')
        except urllib.error.HTTPError as e:
            return e.code, e.geturl(), e.read().decode('utf-8', 'replace')

    def login(self, u, p):
        return self.post('/login', {'username': u, 'password': p, 'next': '/'})

    def csrf(self, path='/customers/new'):
        m = re.search(r'name=_csrf value="([0-9a-f]+)"', self.get(path)[2]); return m.group(1) if m else ''


def multipart(fields, filename, data):
    b = 'XBOUNDARY' + secrets.token_hex(6); out = b''
    for k, v in fields.items():
        out += f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    out += f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode() + data + f'\r\n--{b}--\r\n'.encode()
    return out, f'multipart/form-data; boundary={b}'


def main_demo():
    import openpyxl
    db = os.path.join(HERE, 'biz_app.db')
    for f in (db, db + '-wal', db + '-shm'):
        if os.path.exists(f): os.remove(f)
    con = open_db(db); seed(con); con.close()
    port = 8811
    srv = serve(port, db, quiet=True); threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{port}'
    con = open_db(db)
    R = []
    def cnt(t): return con.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]

    # ① 인증
    anon = Client(base)
    code, url, _ = anon.get('/customers')
    wrong = Client(base); _, _, h = wrong.login('staff', 'nope')
    plain_leak = any(pw in (r['pw_hash'] + r['salt']) for r in con.execute('SELECT pw_hash, salt FROM users') for pw in ('admin1234', 'staff1234', 'viewer1234'))
    ok1 = ('/login' in url) and ('올바르지 않습니다' in h) and not plain_leak and cnt('sessions') == 0
    R.append(('① 인증: 미로그인 → 로그인 이동 · 오답 거부(세션 0) · 비밀번호 평문 저장 0', ok1))

    admin, staff, viewer = Client(base), Client(base), Client(base)
    admin.login('admin', 'admin1234'); staff.login('staff', 'staff1234'); viewer.login('viewer', 'viewer1234')
    c0 = cnt('customers')
    # ② 권한 우회
    vc = viewer.csrf('/customers')                      # viewer는 목록 페이지에서 토큰을 볼 수 없음(폼 없음) → 세션 csrf를 DB에서 직접 꺼내 '토큰은 맞지만 권한이 없는' 케이스로 검증
    vc = con.execute('SELECT csrf FROM sessions s JOIN users u ON u.id=s.user_id WHERE u.username=?', ('viewer',)).fetchone()[0]
    code_v, _, _ = viewer.post('/customers/new', {'_csrf': vc, 'name': '침입자', 'phone': '010-0000-0000', 'grade': '일반'})
    c_after_v = cnt('customers')
    sc = staff.csrf()
    code_s, _, _ = staff.post('/customers/1/delete', {'_csrf': sc})
    still = con.execute('SELECT 1 FROM customers WHERE id=1').fetchone() is not None
    ac = admin.csrf()
    code_r, _, hr = admin.post('/customers/30/delete', {'_csrf': ac})           # 주문이 참조 중 → 거부
    ref_kept = con.execute('SELECT 1 FROM customers WHERE id=30').fetchone() is not None and '참조 중' in hr
    staff.post('/customers/new', {'_csrf': sc, 'name': '임시고객', 'phone': '010-9999-0000', 'grade': '일반', 'memo': ''})
    tmp_id = con.execute("SELECT id FROM customers WHERE name='임시고객'").fetchone()[0]
    code_a, _, _ = admin.post(f'/customers/{tmp_id}/delete', {'_csrf': ac})
    gone = con.execute('SELECT 1 FROM customers WHERE id=?', (tmp_id,)).fetchone() is None
    ok2 = code_v == 403 and c_after_v == c0 and code_s == 403 and still and ref_kept and code_a in (200, 302) and gone
    R.append(('② 권한 우회 차단: viewer 생성 → 403·저장 0 · staff 삭제 → 403·잔존 · ★참조 중 고객 삭제 → 거부 · admin 참조 없는 삭제 → 성공', ok2))
    c1 = cnt('customers')
    # ③ CSRF
    code_n, _, _ = staff.post('/customers/new', {'name': 'CSRF없음', 'phone': '010-1111-2222', 'grade': '일반'})
    code_w, _, _ = staff.post('/customers/new', {'_csrf': 'deadbeef', 'name': 'CSRF틀림', 'phone': '010-1111-2222', 'grade': '일반'})
    ok3 = code_n == 403 and code_w == 403 and cnt('customers') == c1
    R.append(('③ CSRF: 토큰 없음/불일치 POST → 403 · 저장 0', ok3))
    # ④ 동시 편집 (낙관적 잠금)
    rec = con.execute('SELECT * FROM customers WHERE id=2').fetchone(); v0 = rec['version']
    fs = dict(_csrf=sc, name='스태프수정', phone=rec['phone'], grade=rec['grade'], memo='', version=v0)
    fa = dict(_csrf=ac, name='관리자수정', phone=rec['phone'], grade=rec['grade'], memo='', version=v0)
    code_e1, _, _ = staff.post('/customers/2/edit', fs)
    code_e2, _, h2 = admin.post('/customers/2/edit', fa)
    after = con.execute('SELECT name, version FROM customers WHERE id=2').fetchone()
    ok4 = code_e1 in (200, 302) and code_e2 == 409 and after['name'] == '스태프수정' and after['version'] == v0 + 1 and '충돌' in h2
    R.append(('④ ★동시 편집: 두 세션 같은 레코드 → 첫 저장 성공(version+1) · 둘째 409 충돌 · 첫 값 보존', ok4))
    # ⑤ 엑셀 왕복
    code_x, _, _ = admin.get('/orders/export.xlsx')
    r = admin.op.open(base + '/orders/export.xlsx', timeout=10); data = r.read()
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True); ws = wb['주문']
    xrows = [row for row in ws.iter_rows(min_row=2, values_only=True) if row[0] is not None]
    db_orders = con.execute('SELECT o.id, c.name, o.product, o.qty, o.amount, o.status, o.ordered FROM orders o JOIN customers c ON c.id=o.customer_id ORDER BY o.id').fetchall()
    export_ok = len(xrows) == len(db_orders) and all(
        (x[0], x[1], x[2], int(x[3]), int(x[4]), x[5], str(x[6])[:10]) == (d[0], d[1], d[2], d[3], d[4], d[5], d[6]) for x, d in zip(xrows, db_orders))
    # 가져오기: 정상 5 + 불량 3(필수 누락·타입·허용값 밖)
    wb2 = openpyxl.Workbook(); w2 = wb2.active
    w2.append(['고객', '상품', '수량', '금액', '상태', '주문일'])
    names5 = [r[0] for r in con.execute('SELECT DISTINCT name FROM customers ORDER BY id LIMIT 5')]
    good = [[names5[0], '비타민', 2, 24000, '접수', '2026-09-10'], [names5[1], '커피', 1, 18000, '완료', '2026-09-10'],
            [names5[2], '텀블러', 3, 45000, '배송중', '2026-09-11'], [names5[3], '마스크', 5, 45000, '접수', '2026-09-11'], [names5[4], '유산균', 1, 24000, '완료', '2026-09-12']]
    bad = [['', '비타민', 1, 12000, '접수', '2026-09-10'], [names5[0], '비타민', '두개', 12000, '접수', '2026-09-10'], [names5[0], '비타민', 1, 12000, '반품', '2026-09-10']]
    for row in good + bad: w2.append(row)
    buf = io.BytesIO(); wb2.save(buf)
    o0 = cnt('orders')
    body, ctype = multipart({'_csrf': sc}, 'import.xlsx', buf.getvalue())
    code_i, _, hi = staff.post('/orders/import', raw=body, ctype=ctype)
    import_ok = code_i == 200 and '저장 5건' in hi and '격리(미저장) 3건' in hi and cnt('orders') == o0 + 5 and '필수' in hi and '형식 오류' in hi and '허용값' in hi
    ok5 = export_ok and import_ok
    R.append(('⑤ 엑셀 왕복: 내보내기 = DB 전수 일치 · 가져오기 정상 5 저장 · 불량 3 격리(필수·타입·허용값 사유)', ok5))
    # ⑥ 감사 로그 전수
    acts = {r['action']: r['n'] for r in con.execute('SELECT action, COUNT(*) n FROM audit GROUP BY action')}
    upd = con.execute("SELECT before, after FROM audit WHERE action='update' ORDER BY id DESC LIMIT 1").fetchone()
    ok6 = (acts.get('create') == 6 and acts.get('update') == 1 and acts.get('delete') == 1 and acts.get('import') == 1
           and json.loads(upd['before'])['name'] != '스태프수정' and json.loads(upd['after'])['name'] == '스태프수정')
    R.append(('⑥ 감사 로그 전수: 생성 6·수정 1·삭제 1·가져오기 1 = 동작 수 · 수정 전→후 값 정확', ok6))
    # ⑦ 검색·필터·페이징
    _, _, hq = viewer.get('/customers?q=' + urllib.parse.quote('김'))
    nq = con.execute("SELECT COUNT(*) FROM customers WHERE name LIKE '%김%' OR phone LIKE '%김%' OR memo LIKE '%김%'").fetchone()[0]
    _, _, hf = viewer.get('/customers?f=VIP'); nf = con.execute("SELECT COUNT(*) FROM customers WHERE grade='VIP'").fetchone()[0]
    _, _, hp = viewer.get('/orders?page=3'); no = cnt('orders'); pages = (no + PAGE - 1) // PAGE
    ok7 = (f'총 {nq:,}건' in hq and f'총 {nf:,}건' in hf and f'3/{pages}쪽' in hp and hp.count('<tr>') - 1 == no - PAGE * 2)
    R.append(('⑦ 검색·필터·페이징: 검색 카운트 = SQL · 등급 필터 = SQL · 3쪽 행수 = 잔여 정확', ok7))
    # ⑧ 읽기 무부작용·재현성
    h_before = hashlib.sha256(b''.join(str(tuple(r)).encode() for r in con.execute('SELECT * FROM customers ORDER BY id'))).hexdigest()
    a0 = cnt('audit')
    for _ in range(10): viewer.get('/customers'); viewer.get('/orders'); admin.get('/audit')
    h_after = hashlib.sha256(b''.join(str(tuple(r)).encode() for r in con.execute('SELECT * FROM customers ORDER BY id'))).hexdigest()
    ok8 = h_before == h_after and cnt('audit') == a0
    R.append(('⑧ 읽기 무부작용: 조회 30회 후 데이터 해시·감사 로그 불변', ok8))

    srv.shutdown()
    export_xlsx(con, 'customers', os.path.join(HERE, '고객_데모.xlsx')); export_xlsx(con, 'orders', os.path.join(HERE, '주문_데모.xlsx'))
    L = [f'# 내부 업무 웹앱 골격 검증 리포트 ({dt.datetime.now():%Y-%m-%d %H:%M})',
         '- 데모 = 로컬 서버 실구동(127.0.0.1:8811) · 계정 3역할 · 고객 30·주문 45 시드 · 세 세션(admin/staff/viewer)이 실제 HTTP로 공격·편집·가져오기',
         '', '| 검증 | 결과 |', '|---|---|'] + [f'| {k} | {"PASS" if v else "★FAIL"} |' for k, v in R] + [
         '', '## 실물', f'- 실행: python biz_app.py --serve → http://127.0.0.1:8811 (admin/admin1234 · staff/staff1234 · viewer/viewer1234)',
         '- 산출: 고객_데모.xlsx · 주문_데모.xlsx (내보내기 실물) · biz_app.db',
         '', '- ※ 스키마(SCHEMA)에 엔티티를 선언하면 목록·검색·필터·페이징·등록·수정·삭제·엑셀 IO·감사 로그·권한이 자동. 실서비스 = HTTPS 프록시 뒤 + 초기 비밀번호 교체.']
    open(os.path.join(HERE, 'biz_app_verify.md'), 'w', encoding='utf-8').write('\n'.join(L))
    print('\n'.join(L)); con.close()
    return all(v for _, v in R)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    if '--serve' in sys.argv:
        port = int(sys.argv[sys.argv.index('--serve') + 1]) if len(sys.argv) > sys.argv.index('--serve') + 1 else 8811
        if not os.path.exists(DB):
            con = open_db(DB); seed(con); con.close(); print('데모 데이터 생성')
        print(f'업무 시스템: http://127.0.0.1:{port} (Ctrl+C 종료)')
        serve(port, DB, quiet=False).serve_forever()
    else:
        sys.exit(0 if main_demo() else 1)
