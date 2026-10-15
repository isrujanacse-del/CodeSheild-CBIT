"""CBIT Proctored Exam Portal. Run: pip install flask && python app.py
Needs python3, gcc and a JDK (javac/java) on PATH. Linux/macOS (uses `resource`)."""
import os, re, json, sqlite3, subprocess, tempfile, shutil, time, resource, functools
from flask import Flask, request, session, redirect, jsonify, render_template_string, abort
from werkzeug.security import generate_password_hash as gph, check_password_hash as cph

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me-in-production")
DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "exam.db")
ROLL = re.compile(r"^1601\d{8}$")
MAIL = re.compile(r"^[a-z0-9._%+-]+@(cbit\.ac\.in|cbit\.org\.in)$")

def q(sql, a=(), one=False, commit=False):
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    cur = c.execute(sql, a); r = cur.fetchone() if one else cur.fetchall()
    if commit: c.commit(); r = cur.lastrowid
    c.close(); return r

def init():
    c = sqlite3.connect(DB)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, ident TEXT UNIQUE, name TEXT, pw TEXT, role TEXT);
    CREATE TABLE IF NOT EXISTS exams(id INTEGER PRIMARY KEY, title TEXT, minutes INT, max_flags INT);
    CREATE TABLE IF NOT EXISTS questions(id INTEGER PRIMARY KEY, eid INT, title TEXT, body TEXT, marks REAL, mode TEXT);
    CREATE TABLE IF NOT EXISTS tests(id INTEGER PRIMARY KEY, qid INT, inp TEXT, out TEXT, hidden INT);
    CREATE TABLE IF NOT EXISTS attempts(id INTEGER PRIMARY KEY, uid INT, eid INT, start REAL, done INT DEFAULT 0, UNIQUE(uid,eid));
    CREATE TABLE IF NOT EXISTS subs(id INTEGER PRIMARY KEY, uid INT, qid INT, lang TEXT, code TEXT, score REAL, passed INT, total INT, UNIQUE(uid,qid));
    CREATE TABLE IF NOT EXISTS flags(id INTEGER PRIMARY KEY, uid INT, eid INT, kind TEXT, ts TEXT DEFAULT CURRENT_TIMESTAMP);
    """)
    if not c.execute("SELECT 1 FROM users").fetchone():
        c.execute("INSERT INTO users(ident,name,pw,role) VALUES('admin','Administrator',?,'admin')", (gph("admin123"),))
        c.execute("INSERT INTO users(ident,name,pw,role) VALUES('160100000000','Test Student',?,'student')", (gph("test123"),))
        c.execute("INSERT INTO exams(id,title,minutes,max_flags) VALUES(1,'CS301 — CBIT Data Structures & Coding Lab Exam',60,5)")
        c.execute("INSERT INTO questions(id,eid,title,body,marks,mode) VALUES(1,1,'Two-Number Sum & Parity','Read two integers A and B from stdin. Print their sum S followed by a space and EVEN if S is even or ODD if S is odd.',10,'partial')")
        c.executemany("INSERT INTO tests(qid,inp,out,hidden) VALUES(1,?,?,?)", [('14 28\n','42 EVEN',0),('7 12\n','19 ODD',0),('-15 15\n','0 EVEN',1),('100 205\n','305 ODD',1)])
        c.execute("INSERT INTO questions(id,eid,title,body,marks,mode) VALUES(2,1,'Maximum Subarray Sum (Kadane)','Given N followed by N space-separated integers, print the maximum contiguous subarray sum.',15,'all')")
        c.executemany("INSERT INTO tests(qid,inp,out,hidden) VALUES(2,?,?,?)", [('5\n-2 1 -3 4 -1\n','4',0),('6\n4 -1 2 1 -5 4\n','6',0),('4\n-8 -3 -6 -2\n','-2',1),('5\n10 20 -5 15 30\n','70',1)])
    c.commit(); c.close()

def me(): return q("SELECT * FROM users WHERE id=?", (session.get("uid"),), one=True) if "uid" in session else None
def need(role):
    def deco(f):
        @functools.wraps(f)
        def w(*a, **k):
            u = me()
            if not u: return redirect("/login")
            if u["role"] != role: abort(403)
            return f(u, *a, **k)
        return w
    return deco

# ---------- code runner (NOT a hardened sandbox: use Docker/nsjail in production) ----------
def norm(s): return "\n".join(l.rstrip() for l in s.strip().replace("\r", "").split("\n"))
def limits(java):
    def f():
        resource.setrlimit(resource.RLIMIT_CPU, (10, 10)); resource.setrlimit(resource.RLIMIT_FSIZE, (1 << 20, 1 << 20))
        if not java: resource.setrlimit(resource.RLIMIT_AS, (512 << 20, 512 << 20))
    return f

def run_tests(lang, code, tests):
    d = tempfile.mkdtemp(); env = {"PATH": os.environ.get("PATH", "")}
    try:
        if lang == "python": open(d + "/main.py", "w").write(code); cmd = ["python3", "main.py"]
        elif lang == "c":
            open(d + "/main.c", "w").write(code)
            p = subprocess.run(["gcc", "main.c", "-o", "main", "-lm"], cwd=d, capture_output=True, text=True, timeout=20)
            if p.returncode: return [dict(ok=False, got="", err="Compile error:\n" + p.stderr[:800])] * len(tests)
            cmd = ["./main"]
        elif lang == "java":
            open(d + "/Main.java", "w").write(code)
            p = subprocess.run(["javac", "Main.java"], cwd=d, capture_output=True, text=True, timeout=30)
            if p.returncode: return [dict(ok=False, got="", err="Compile error:\n" + p.stderr[:800])] * len(tests)
            cmd = ["java", "-Xmx256m", "Main"]
        else: return [dict(ok=False, got="", err="Unsupported language")] * len(tests)
        out = []
        for t in tests:
            try:
                p = subprocess.run(cmd, cwd=d, input=t["inp"], capture_output=True, text=True, env=env,
                                   timeout=10 if lang == "java" else 5, preexec_fn=limits(lang == "java"))
                out.append(dict(ok=p.returncode == 0 and norm(p.stdout) == norm(t["out"]), got=p.stdout[:500], err=p.stderr[:500]))
            except subprocess.TimeoutExpired: out.append(dict(ok=False, got="", err="Time limit exceeded"))
        return out
    finally: shutil.rmtree(d, True)

def grade(qu, res):
    p, n = sum(r["ok"] for r in res), len(res)
    s = 0 if not n else (qu["marks"] * p / n if qu["mode"] == "partial" else (qu["marks"] if p == n else 0))
    return round(s, 2), p, n

def parse_tests(text):
    """Blocks separated by a line '---'. Each: input, a line '=>', expected output. Start a block with '#hidden' to hide it."""
    res = []
    for b in re.split(r"\n-{3,}\n", "\n" + text.replace("\r", "").strip() + "\n"):
        b = b.strip("\n")
        if "=>" not in b: continue
        hidden = b.startswith("#hidden"); b = b.split("\n", 1)[1] if hidden and "\n" in b else b
        inp, out = re.split(r"\n?=>\n?", b, 1)
        res.append((inp.strip() + "\n", out.strip(), int(hidden)))
    return res

# ---------- pages ----------
BASE = """<!doctype html><html data-theme="dark"><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>{{ t }}</title><style>
:root,html[data-theme="light"]{--bg:#f8fafc;--fg:#0f172a;--card:#ffffff;--bd:#cbd5e1;--ac:#4f46e5;--bad:#be123c;--ok:#047857;--muted:#475569}
html[data-theme="dark"]{--bg:#0b0f19;--fg:#e2e8f0;--card:#111827;--bd:#1f2937;--ac:#6366f1;--bad:#f87171;--ok:#34d399;--muted:#94a3b8}
*{box-sizing:border-box}body{margin:0;font:15px system-ui,-apple-system,sans-serif;background:var(--bg);color:var(--fg);transition:background .2s,color .2s}
nav{display:flex;justify-content:space-between;align-items:center;padding:12px 20px;background:var(--card);border-bottom:1px solid var(--bd)}
main{max-width:980px;margin:20px auto;padding:0 16px}.card{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:18px;margin-bottom:16px}
input,select,textarea,button{font:inherit;padding:8px 10px;border:1px solid var(--bd);border-radius:8px;background:var(--bg);color:var(--fg);width:100%;margin:4px 0 10px}
button,.btn{background:var(--ac);color:#fff;border:0;cursor:pointer;width:auto;padding:8px 16px;border-radius:8px;text-decoration:none;display:inline-block;font-weight:600}
.theme-btn{background:transparent;color:var(--fg);border:1px solid var(--bd);padding:6px 12px;margin:0 8px 0 0;font-size:13px}
a{color:var(--ac)}table{width:100%;border-collapse:collapse}td,th{padding:8px;border-bottom:1px solid var(--bd);text-align:left}
.err{color:var(--bad)}.ok{color:var(--ok)}.muted{color:var(--muted);font-size:13px}textarea{font-family:ui-monospace,Consolas,monospace}
</style></head><body><nav><div><b>CodeShield CBIT</b> <span class=muted>· Lock the Tab. Prove the Logic.</span></div>
<div style="display:flex;align-items:center;gap:8px"><button type=button class=theme-btn id=tbtn onclick="toggleTheme()">☀️ Bright Theme</button>
<span>{% if u %}{{ u.name }} · <a href=/logout>Logout</a>{% endif %}</span></div></nav><main>@@BODY@@</main>
<script>
function applyTheme(t){document.documentElement.setAttribute('data-theme',t);var b=document.getElementById('tbtn');if(b)b.textContent=t==='dark'?'☀️ Bright Theme':'🌙 Dark Theme';}
function toggleTheme(){var cur=document.documentElement.getAttribute('data-theme')||'dark';var nxt=cur==='dark'?'light':'dark';try{localStorage.setItem('cs_theme',nxt)}catch(e){}applyTheme(nxt);}
try{var sv=localStorage.getItem('cs_theme');if(sv)applyTheme(sv);}catch(e){}
</script></body></html>"""
def page(body, **k): return render_template_string(BASE.replace("@@BODY@@", body), u=me(), t=k.pop("t", "CodeShield CBIT — Proctored Coding Arena"), **k)

@app.route("/")
def index():
    u = me()
    return redirect("/login" if not u else "/admin" if u["role"] == "admin" else "/student")

@app.route("/login", methods=["GET", "POST"])
def login():
    err = ""
    if request.method == "POST":
        u = q("SELECT * FROM users WHERE ident=?", (request.form["ident"].strip().lower(),), one=True)
        if u and cph(u["pw"], request.form["pw"]): session["uid"] = u["id"]; return redirect("/")
        err = "Invalid credentials"
    return page("""<div class=card style="max-width:380px;margin:40px auto"><h2>Login</h2><p class=err>{{err}}</p>
    <form method=post>Roll number / CBIT email<input name=ident required>Password<input name=pw type=password required>
    <button>Login</button> <a href=/register>Register</a></form></div>""", err=err)

@app.route("/register", methods=["GET", "POST"])
def register():
    err = ""
    if request.method == "POST":
        i, n, p = request.form["ident"].strip().lower(), request.form["name"].strip(), request.form["pw"]
        if not (ROLL.match(i) or MAIL.match(i)): err = "Use a roll number like 1601xxxxxxxx or a @cbit.ac.in / @cbit.org.in email."
        elif len(p) < 6 or not n: err = "Name required and password must be at least 6 characters."
        elif q("SELECT 1 FROM users WHERE ident=?", (i,), one=True): err = "Already registered."
        else: q("INSERT INTO users(ident,name,pw,role) VALUES(?,?,?,'student')", (i, n, gph(p)), commit=True); return redirect("/login")
    return page("""<div class=card style="max-width:380px;margin:40px auto"><h2>Student registration</h2><p class=err>{{err}}</p>
    <form method=post>Full name<input name=name required>Roll number or CBIT email<input name=ident required>
    Password<input name=pw type=password required><button>Register</button> <a href=/login>Back</a></form></div>""", err=err)

@app.route("/logout")
def logout(): session.clear(); return redirect("/login")

# ---------- admin ----------
@app.route("/admin", methods=["GET", "POST"])
@need("admin")
def admin(u):
    if request.method == "POST":
        q("INSERT INTO exams(title,minutes,max_flags) VALUES(?,?,?)", (request.form["title"], int(request.form["minutes"]), int(request.form["mf"])), commit=True)
    return page("""<div class=card><h3>Exams</h3><table>{% for e in ex %}<tr><td>{{e.title}}</td><td>{{e.minutes}} min · auto-submit after {{e.max_flags}} flags</td>
    <td><a href=/admin/exam/{{e.id}}>Questions</a> · <a href=/admin/exam/{{e.id}}/results>Results &amp; flags</a></td></tr>{% endfor %}</table></div>
    <div class=card><h3>New exam</h3><form method=post>Title<input name=title required>Duration (minutes)<input name=minutes type=number value=60>
    Max flags before auto-submit<input name=mf type=number value=5><button>Create</button></form></div>""", ex=q("SELECT * FROM exams ORDER BY id DESC"))

@app.route("/admin/exam/<int:eid>", methods=["GET", "POST"])
@need("admin")
def admin_exam(u, eid):
    if request.method == "POST":
        f = request.form
        qid = q("INSERT INTO questions(eid,title,body,marks,mode) VALUES(?,?,?,?,?)", (eid, f["title"], f["body"], float(f["marks"]), f["mode"]), commit=True)
        for i, o, h in parse_tests(f["tests"]): q("INSERT INTO tests(qid,inp,out,hidden) VALUES(?,?,?,?)", (qid, i, o, h), commit=True)
    if request.args.get("del"):
        q("DELETE FROM tests WHERE qid=?", (request.args["del"],), commit=True); q("DELETE FROM questions WHERE id=?", (request.args["del"],), commit=True)
    qs = q("SELECT *,(SELECT COUNT(*) FROM tests WHERE qid=questions.id) nt FROM questions WHERE eid=?", (eid,))
    return page("""<a href=/admin>&larr; Exams</a><div class=card><h3>Questions</h3><table>{% for x in qs %}<tr><td>{{x.title}}</td><td>{{x.marks}} marks · {{x.mode}} · {{x.nt}} tests</td>
    <td><a href="?del={{x.id}}" onclick="return confirm('Delete?')">Delete</a></td></tr>{% endfor %}</table></div>
    <div class=card><h3>Add question</h3><form method=post>Title<input name=title required>Problem statement<textarea name=body rows=5 required></textarea>
    Marks<input name=marks type=number step=any value=10>Grading
    <select name=mode><option value=partial>Partial: marks proportional to test cases passed</option><option value=all>All-or-nothing: full marks only if every test passes</option></select>
    Test cases (blocks separated by a line of <code>---</code>; input, then a line <code>=&gt;</code>, then expected output; first line <code>#hidden</code> hides a test from students)
    <textarea name=tests rows=9 placeholder="2 3&#10;=>&#10;5&#10;---&#10;#hidden&#10;10 20&#10;=>&#10;30"></textarea><button>Add</button></form></div>""", qs=qs)

@app.route("/admin/exam/<int:eid>/results")
@need("admin")
def results(u, eid):
    rows = q("""SELECT u.name,u.ident,a.done,(SELECT COALESCE(SUM(s.score),0) FROM subs s JOIN questions z ON z.id=s.qid WHERE s.uid=u.id AND z.eid=a.eid) score,
      (SELECT COUNT(*) FROM flags f WHERE f.uid=u.id AND f.eid=a.eid) nf FROM attempts a JOIN users u ON u.id=a.uid WHERE a.eid=?""", (eid,))
    fl = q("SELECT f.ts,f.kind,u.name,u.ident FROM flags f JOIN users u ON u.id=f.uid WHERE f.eid=? ORDER BY f.id DESC", (eid,))
    tot = q("SELECT COALESCE(SUM(marks),0) t FROM questions WHERE eid=?", (eid,), one=True)["t"]
    return page("""<a href=/admin>&larr; Exams</a><div class=card><h3>Scores (out of {{tot}})</h3><table><tr><th>Student<th>Score<th>Flags<th>Status</tr>
    {% for r in rows %}<tr><td>{{r.name}} ({{r.ident}})<td>{{r.score}}<td class="{{'err' if r.nf}}">{{r.nf}}<td>{{'Submitted' if r.done else 'In progress'}}</tr>{% endfor %}</table></div>
    <div class=card><h3>Flag log</h3><table><tr><th>Time (UTC)<th>Student<th>Violation</tr>{% for f in fl %}<tr><td>{{f.ts}}<td>{{f.name}} ({{f.ident}})<td class=err>{{f.kind}}</tr>{% endfor %}</table></div>""", rows=rows, fl=fl, tot=tot)

# ---------- student ----------
@app.route("/student")
@need("student")
def student(u):
    return page("""<div class=card><h3>Available exams</h3><table>{% for e in ex %}<tr><td>{{e.title}}</td><td>{{e.minutes}} min</td><td><a class=btn href=/exam/{{e.id}}>Open</a></td></tr>{% endfor %}</table></div>""",
                ex=q("SELECT * FROM exams ORDER BY id DESC"))

EXAM_JS = r"""
const D=JSON.parse(document.getElementById('d').textContent),$=id=>document.getElementById(id);
let Q=[],cur=0,ended=false,started=false,endAt=0,drafts={},last={};
const TPL={python:'# read input with input()\n',c:'#include <stdio.h>\nint main(){\n    return 0;\n}\n',java:'import java.util.*;\npublic class Main {\n    public static void main(String[] a){\n        Scanner s=new Scanner(System.in);\n    }\n}\n'};
const post=(u,b)=>fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}).then(r=>r.json());
const esc=s=>String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
function flag(kind){if(!started||ended)return;const t=Date.now();if(last[kind]&&t-last[kind]<1500)return;last[kind]=t;
 post('/api/flag',{eid:D.eid,kind:kind}).then(r=>{$('fl').textContent=r.n+'/'+r.max;if(r.done)finish(true)});}
function fs(){return document.documentElement.requestFullscreen({navigationUI:'hide'}).catch(()=>{});}
$('go').onclick=async()=>{await fs();const r=await post('/api/start',{eid:D.eid});if(r.error){$('msg').textContent=r.error;return}
 Q=r.questions;endAt=Date.now()+r.remaining*1000;$('fl').textContent=r.n+'/'+r.max;$('intro').style.display='none';$('exam').style.display='block';started=true;show(0);tick();};
function tick(){if(ended)return;const s=Math.max(0,Math.round((endAt-Date.now())/1000));$('tm').textContent=Math.floor(s/60)+':'+String(s%60).padStart(2,'0');if(s<=0)return finish(true);setTimeout(tick,500);}
function show(i){cur=i;const x=Q[i];$('qs').innerHTML=Q.map((y,j)=>'<button style="margin:2px;opacity:'+(j==i?1:.6)+'" onclick="show('+j+')">Q'+(j+1)+(y.score!=null?' ✓':'')+'</button>').join('');
 $('qt').textContent=x.title+' ('+x.marks+' marks)';$('qb').textContent=x.body;
 $('samples').innerHTML=x.samples.map(t=>'<div><b>Sample input</b><pre>'+esc(t.inp)+'</pre><b>Expected output</b><pre>'+esc(t.out)+'</pre></div>').join('');
 $('lang').value=x.lang||'python';load();$('out').innerHTML='';}
function key(){return Q[cur].id+':'+$('lang').value}
function load(){const k=key();$('ed').value=drafts[k]!==undefined?drafts[k]:(Q[cur].code&&Q[cur].lang==$('lang').value?Q[cur].code:TPL[$('lang').value]);}
$('lang').onchange=load;$('ed').oninput=e=>{drafts[key()]=$('ed').value;if(e.inputType&&/paste|drop/i.test(e.inputType)){flag('paste_attempt');load()}};
async function go(path){$('out').innerHTML='Running…';const r=await post(path,{eid:D.eid,qid:Q[cur].id,lang:$('lang').value,code:$('ed').value});
 if(r.error){$('out').textContent=r.error;if(r.done)finish(true);return}
 let h='';if(r.score!=null){h+='<p><b>Score: '+r.score+' / '+Q[cur].marks+'</b> — passed '+r.passed+' of '+r.total+' tests</p>';Q[cur].score=r.score;Q[cur].lang=$('lang').value;Q[cur].code=$('ed').value;show(cur);}
 (r.results||[]).forEach((t,i)=>{h+='<p class='+(t.ok?'ok':'err')+'>Sample '+(i+1)+': '+(t.ok?'PASS':'FAIL')+'</p>'+(t.ok?'':'<pre>'+esc(t.err||('Got:\n'+t.got))+'</pre>')});$('out').innerHTML=h;}
$('run').onclick=()=>go('/api/run');$('sub').onclick=()=>go('/api/submit');
$('fin').onclick=()=>{if(confirm('Finish and submit the exam?'))finish(false)};
function finish(forced){if(ended)return;ended=true;post('/api/finish',{eid:D.eid}).then(()=>{if(document.fullscreenElement)document.exitFullscreen();
 document.body.innerHTML='<main><div class=card><h2>Exam submitted</h2><p>'+(forced?'Your exam was ended automatically (time up or too many violations).':'Thank you.')+'</p><a href=/student>Back</a></div></main>'})}
// ---- proctoring ----
document.addEventListener('visibilitychange',()=>{if(document.hidden)flag('tab_switch_or_minimize')});
window.addEventListener('blur',()=>flag('window_lost_focus'));
document.addEventListener('fullscreenchange',()=>{if(!started||ended)return;if(!document.fullscreenElement){flag('fullscreen_exit');$('ov').style.display='flex'}else $('ov').style.display='none'});
$('back').onclick=fs;
['copy','cut','paste','drop','dragstart','contextmenu'].forEach(ev=>document.addEventListener(ev,e=>{e.preventDefault();flag(ev==='contextmenu'?'right_click':ev+'_attempt')}));
document.addEventListener('keydown',e=>{const k=e.key.toLowerCase(),c=e.ctrlKey||e.metaKey;
 if(e.key==='Tab'&&e.target.id==='ed'){e.preventDefault();const t=e.target,s=t.selectionStart;t.setRangeText('    ',s,t.selectionEnd,'end');t.dispatchEvent(new Event('input'));return}
 if(c&&['c','v','x','a','p','s','u','f','g','n','t','w','r'].includes(k)&&!(k==='a'&&false)){e.preventDefault();flag('blocked_shortcut_ctrl_'+k)}
 else if(c&&e.shiftKey&&['i','j','c','k'].includes(k)){e.preventDefault();flag('devtools_shortcut')}
 else if(e.key==='F12'||e.key==='PrintScreen'||e.key==='Meta'||e.key==='Escape'||e.key==='Insert'&&e.shiftKey||(e.altKey&&e.key==='Tab')){e.preventDefault();flag('blocked_key_'+e.key)}});
window.addEventListener('beforeunload',e=>{if(started&&!ended){e.preventDefault();e.returnValue=''}});
window.addEventListener('resize',()=>{if(started&&!ended&&!document.fullscreenElement)flag('window_resized')});
if(navigator.keyboard&&navigator.keyboard.lock)document.addEventListener('fullscreenchange',()=>{if(document.fullscreenElement)navigator.keyboard.lock().catch(()=>{})});
"""

@app.route("/exam/<int:eid>")
@need("student")
def exam(u, eid):
    e = q("SELECT * FROM exams WHERE id=?", (eid,), one=True) or abort(404)
    return page("""<div id=intro class=card><h2>{{e.title}}</h2><p>Duration: {{e.minutes}} minutes. Languages: Python, C, Java.</p><ul>
    <li>The exam opens in fullscreen. Leaving fullscreen, switching tabs/windows, copy/paste, right-click, shortcuts and developer tools are <b>blocked and flagged</b> to the admin.</li>
    <li>After {{e.max_flags}} flags your exam is submitted automatically.</li><li>Java: the class must be named <code>Main</code>. Read input from stdin, write to stdout.</li></ul>
    <button id=go>I agree — start exam</button> <span id=msg class=err></span></div>
    <div id=exam style="display:none"><div class=card style="display:flex;justify-content:space-between"><b>Time left: <span id=tm></span></b><span>Flags: <b id=fl class=err></b></span><button id=fin>Finish exam</button></div>
    <div class=card><div id=qs></div><h3 id=qt></h3><pre id=qb style="white-space:pre-wrap;font-family:inherit"></pre><div id=samples></div></div>
    <div class=card><select id=lang style="width:auto"><option value=python>Python</option><option value=c>C</option><option value=java>Java</option></select>
    <textarea id=ed rows=16 spellcheck=false autocomplete=off autocapitalize=off></textarea><button id=run>Run sample tests</button> <button id=sub>Submit this question</button><div id=out></div></div></div>
    <div id=ov style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.92);color:#fff;z-index:99;align-items:center;justify-content:center;flex-direction:column">
    <h2>You left fullscreen — this has been flagged.</h2><button id=back>Return to fullscreen</button></div>
    <script id=d type=application/json>{{ {'eid': e.id}|tojson }}</script><script>{{ js|safe }}</script>""", e=e, js=EXAM_JS, t=e["title"])

def active(u, eid):
    a = q("SELECT a.*,e.minutes,e.max_flags FROM attempts a JOIN exams e ON e.id=a.eid WHERE a.uid=? AND a.eid=?", (u["id"], eid), one=True)
    if not a or a["done"] or time.time() > a["start"] + a["minutes"] * 60 + 10: return None
    return a

def nflags(uid, eid): return q("SELECT COUNT(*) c FROM flags WHERE uid=? AND eid=?", (uid, eid), one=True)["c"]

@app.route("/api/start", methods=["POST"])
@need("student")
def api_start(u):
    eid = request.json["eid"]; e = q("SELECT * FROM exams WHERE id=?", (eid,), one=True) or abort(404)
    if not q("SELECT 1 FROM attempts WHERE uid=? AND eid=?", (u["id"], eid), one=True):
        q("INSERT INTO attempts(uid,eid,start) VALUES(?,?,?)", (u["id"], eid, time.time()), commit=True)
    a = active(u, eid)
    if not a: return jsonify(error="This exam has already ended for you.")
    qs = []
    for x in q("SELECT * FROM questions WHERE eid=?", (eid,)):
        s = q("SELECT * FROM subs WHERE uid=? AND qid=?", (u["id"], x["id"]), one=True)
        qs.append(dict(id=x["id"], title=x["title"], body=x["body"], marks=x["marks"], score=s and s["score"], lang=s and s["lang"], code=s and s["code"],
                       samples=[dict(inp=t["inp"], out=t["out"]) for t in q("SELECT * FROM tests WHERE qid=? AND hidden=0", (x["id"],))]))
    return jsonify(questions=qs, remaining=int(a["start"] + e["minutes"] * 60 - time.time()), n=nflags(u["id"], eid), max=e["max_flags"])

def _run(u, hidden_too):
    j = request.json; a = active(u, j["eid"])
    if not a: return None, None, jsonify(error="Exam has ended.", done=True)
    qu = q("SELECT * FROM questions WHERE id=? AND eid=?", (j["qid"], j["eid"]), one=True) or abort(404)
    tests = q("SELECT * FROM tests WHERE qid=?" + ("" if hidden_too else " AND hidden=0"), (qu["id"],))
    return j, qu, run_tests(j["lang"], j["code"][:20000], tests)

@app.route("/api/run", methods=["POST"])
@need("student")
def api_run(u):
    j, qu, r = _run(u, False)
    return r if j is None else jsonify(results=r)

@app.route("/api/submit", methods=["POST"])
@need("student")
def api_submit(u):
    j, qu, r = _run(u, True)
    if j is None: return r
    s, p, n = grade(qu, r)
    q("INSERT OR REPLACE INTO subs(uid,qid,lang,code,score,passed,total) VALUES(?,?,?,?,?,?,?)", (u["id"], qu["id"], j["lang"], j["code"], s, p, n), commit=True)
    return jsonify(score=s, passed=p, total=n, results=[x for x, t in zip(r, q("SELECT hidden FROM tests WHERE qid=?", (qu["id"],))) if not t["hidden"]])

@app.route("/api/flag", methods=["POST"])
@need("student")
def api_flag(u):
    j = request.json; a = active(u, j["eid"])
    if not a: return jsonify(n=nflags(u["id"], j["eid"]), max=0, done=True)
    q("INSERT INTO flags(uid,eid,kind) VALUES(?,?,?)", (u["id"], j["eid"], str(j["kind"])[:60]), commit=True)
    n = nflags(u["id"], j["eid"]); return jsonify(n=n, max=a["max_flags"], done=n > a["max_flags"])

@app.route("/api/finish", methods=["POST"])
@need("student")
def api_finish(u):
    q("UPDATE attempts SET done=1 WHERE uid=? AND eid=?", (u["id"], request.json["eid"]), commit=True); return jsonify(ok=True)

init()
if __name__ == "__main__": app.run(host="0.0.0.0", port=5000)
