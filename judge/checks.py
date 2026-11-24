"""The question the agent is asked, the recorder that reads what a fix sends, and the deterministic answer grader."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path


DRIVER = r'''
import importlib.util, inspect, json, os, re, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

SEEN = []

class Stand(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _any(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8", "replace") if n else ""
        u = urlsplit(self.path)
        body_keys, soap = [], None
        if raw.lstrip().startswith("<"):
            m = re.search(r"<(?:[\w-]+:)?Body\b[^>]*>\s*<(?:[\w-]+:)?([A-Za-z]\w*)", raw)
            soap = m.group(1) if m else None
        elif raw:
            try:
                data = json.loads(raw)
                body_keys = sorted(data) if isinstance(data, dict) else (sorted(data[0]) if isinstance(data, list) and data and isinstance(data[0], dict) else [])
            except ValueError:
                body_keys = sorted(parse_qs(raw))
        SEEN.append({"method": self.command.lower(), "path": u.path, "query": {k: v[0] for k, v in parse_qs(u.query).items()},
                     "body_keys": body_keys, "soap": soap})
        xml = raw.lstrip().startswith("<")
        out = (b"<soapenv:Envelope xmlns:soapenv='http://schemas.xmlsoap.org/soap/envelope/'><soapenv:Body/></soapenv:Envelope>" if xml else b"{}")
        self.send_response(200)
        self.send_header("Content-Type", "text/xml" if xml else "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)
    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _any

srv = ThreadingHTTPServer(("127.0.0.1", 0), Stand)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_address[1]}"
src = open(sys.argv[1]).read()
# Point base URLs at the stand-in; leave every other setting at the fix's own default. A setting the fix reads with
# no default gets a test value (name_hard), as the test harness would have to supply one.
for name, default in re.findall(r"""(?:environ\.get|getenv)\(\s*["']([A-Za-z0-9_]+)["']\s*(?:,\s*(["'][^"']*["']|[^)]+))?\)""", src) + \
        [(n, "") for n in re.findall(r"""environ\[\s*["']([A-Za-z0-9_]+)["']\s*\]""", src)]:
    default = default.strip().strip("'\"")
    if default.startswith(("http://", "https://")) or (not default and re.search(r"BASE|URL|HOST|ENDPOINT", name, re.I)):
        os.environ[name] = base
    elif not default and name not in os.environ:
        os.environ[name] = "sk-judge" if re.search(r"TOKEN|KEY|SECRET|PASSWORD", name, re.I) else f"{name.lower()}_hard"
# Every request, to any host and over http or https, goes to the stand-in: httpx's transports are wrapped to rewrite
# the address to the stand-in's, keeping path and query, so a hard-coded vendor URL is recorded too.
import httpx
for p in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy"):
    os.environ.pop(p, None)
_host, _port = srv.server_address

def _to_stand(request):
    request.url = request.url.copy_with(scheme="http", host=_host, port=_port)
    request.headers["host"] = f"{_host}:{_port}"
    return request

_sync, _async = httpx.HTTPTransport.handle_request, httpx.AsyncHTTPTransport.handle_async_request
httpx.HTTPTransport.handle_request = lambda self, request: _sync(self, _to_stand(request))
async def _ah(self, request):
    return await _async(self, _to_stand(request))
httpx.AsyncHTTPTransport.handle_async_request = _ah

def value(name, ann):
    ann = ann if isinstance(ann, str) else getattr(ann, "__name__", str(ann))
    low = (name + " " + ann).lower()
    if "client" in low or "session" in low:
        import httpx
        return httpx.Client()
    if "int" in ann.lower() or name.lower() in ("limit", "page_size", "pagesize", "size", "top", "count", "page"):
        return 25
    if "bool" in ann.lower():
        return True
    if "dict" in ann.lower() or "mapping" in ann.lower():
        return {}
    if "list" in ann.lower() or "sequence" in ann.lower():
        return []
    return f"{name}_hard"  # reads as a test value, so the grader treats it as filling an ID slot

error = None
try:
    spec = importlib.util.spec_from_file_location("fix", sys.argv[1])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fn = getattr(mod, "run")
    kwargs = {p.name: value(p.name, p.annotation) for p in inspect.signature(fn).parameters.values()
              if p.default is inspect.Parameter.empty and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)}
    fn(**kwargs)
except BaseException as exc:
    error = f"{type(exc).__name__}: {str(exc)[:200]}"
print("@@RESULT@@" + json.dumps({"requests": SEEN, "error": error}))
sys.stdout.flush()
os._exit(0)
'''


def record(source: str, timeout: float = 15.0) -> dict:
    """Run `source` once against the stand-in; return the requests it sent and the error it raised, if any."""
    with tempfile.TemporaryDirectory() as tmp:
        fix, driver = Path(tmp) / "fix.py", Path(tmp) / "driver.py"
        fix.write_text(source or "")
        driver.write_text(DRIVER)
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "VIRTUAL_ENV", "PYTHONPATH", "TMPDIR")}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        try:
            out = subprocess.run([sys.executable, "-B", str(driver), str(fix)], cwd=tmp, env=env, capture_output=True,
                                 text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            text = (exc.stdout or b"").decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            return {"requests": [], "error": "Timeout"} if "@@RESULT@@" not in text else json.loads(text.split("@@RESULT@@")[-1])
        line = next((l for l in out.stdout.splitlines() if l.startswith("@@RESULT@@")), None)
        if line is None:
            return {"requests": [], "error": (out.stderr.strip().splitlines() or ["no result"])[-1][:200]}
        return json.loads(line.removeprefix("@@RESULT@@"))


_RECORDED: dict[tuple[str, str], dict] = {}


def recorded(row: dict, which: str) -> dict:
    """The requests a question's reference fix (`gold`) or starter sends to the stand-in. A recording that sent a
    request is kept for the process; one that sent none (a timeout under load, a crash) is tried again next time."""
    key = (row["task_id"], which)
    if key in _RECORDED:
        return _RECORDED[key]
    src = row["gold_code"] if which == "gold" else row["starter_code"]
    result = record(src) if src else {"requests": [], "error": "no file"}
    if result["requests"]:
        _RECORDED[key] = result
    return result


VENDORS = {"gen": "Genesys Cloud", "sf": "SAP SuccessFactors", "sap": "SAP S/4HANA Cloud", "sn": "ServiceNow",
           "ukg": "UKG Pro Workforce Management", "wd": "Workday", "vv": "Veeva Vault"}

ANSWER_SCHEMA = {
    "type": "json_schema",
    "name": "request",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "method": {"type": "string", "description": "HTTP method: GET, POST, PUT, PATCH or DELETE."},
            "path": {"type": "string", "description": "Address path after the host, as the docs write it; a value the caller supplies as {name}."},
            "operation": {"type": "string", "description": "For SOAP, the request element (for example Get_Workers_Request); empty otherwise."},
            "fields": {"type": "array", "items": {"type": "string"},
                       "description": "Query parameters or body fields the ticket needs sent, as name, or name=value when the docs fix the value (for example $expand=someNavigation); empty if none."},
        },
        "required": ["method", "path", "operation", "fields"],
        "additionalProperties": False,
    },
}

_RULES = """Answer with the request only: method, path, SOAP operation if the API is SOAP, and the query parameters or body fields the ticket needs. Do not write code. Write any value the caller supplies as {name}. When the docs fix a value the ticket needs (a navigation to expand, an action name), give it as name=value."""
SYSTEM = f"""You find the exact API request a ticket needs in the vendor's current public documentation.
You have web_search and web_fetch. You must call web_search before you answer. Do not rely on memory for paths, operation names or field names: use a name only if you saw it in a result. Fetch a specific URL when snippets are not enough. If you are unsure, search again; using more of the search budget is better than a wrong answer.
{_RULES}"""
SYSTEM_SEARCH_ONLY = f"""You find the exact API request a ticket needs in the vendor's current public documentation.
You have web_search. You do not have web_fetch. You must call web_search before you answer. Do not rely on memory for paths, operation names or field names: use a name only if you saw it in a result. If snippets are not enough, search again with a more specific query. If you are unsure, search again; using more of the search budget is better than a wrong answer.
{_RULES}"""
SYSTEM_NO_SEARCH = f"""You state the exact API request a ticket needs, from your own knowledge of the vendor's current public documentation. You cannot look anything up.
{_RULES}"""


def ticket_text(row: dict) -> str:
    """The ticket sentence(s) from the locked prompt, without the code-editing instructions."""
    text = row["prompt"].split("Desired state (product ticket):", 1)[-1].strip().split("\n\n")[0]
    return re.split(r"\s*Cite the https URL", text)[0].strip()


def question(row: dict) -> str:
    """The question text. Every question's starter sends a request, so a recording that sent none is our failure and
    raises, rather than asking a question without its "sends this request today" line."""
    starter = recorded(row, "starter")
    if not starter["requests"]:
        raise RuntimeError(f"{row['task_id']}: the starter sent no request to the stand-in ({starter.get('error')})")
    r = starter["requests"][0]
    now = f"{r['method'].upper()} {r['path']}" + (f" (SOAP operation {r['soap']})" if r.get("soap") else "")
    qid = row["task_id"]
    vendor = VENDORS.get(qid.split("_")[1], "") if qid.count("_") >= 2 else ""
    return (f"Vendor: {vendor}\n" if vendor else "") + f"The program sends this request today: {now}\n" + \
        f"Ticket: {ticket_text(row)}\n\nWhich request does the ticket need?"


SLOT = "{}"
_HOLE = "\x00"  # stands for a value the source computes at run time
# Test values the ticket writer put where the docs have a placeholder: object_record_hard, object_hard, abc1a2b3c4d5e6.
FIXTURE = re.compile(r":?[A-Za-z0-9_.-]+_hard\b|[a-z]{3}1a2b3c4d5e6")
VERSION = re.compile(r"^v\d+(?:\.\d+)*$")
PAGING = {"$format", "$top", "$skip", "$count", "$inlinecount", "$skiptoken", "format", "limit", "offset", "cursor", "page", "pagesize", "page_size"}
# A placeholder in any style: {id}, {{id}}, <id>, URL-encoded %7Bid%7D.
PLACEHOLDER = re.compile(r"\{\{[^{}/\s]*\}\}|\{[^{}/\s]*\}|%7B[^/\s]*?%7D|<[\w.-]+>", re.I)


def _segment(raw: str) -> str:
    seg = FIXTURE.sub(_HOLE, PLACEHOLDER.sub(_HOLE, raw)).lower()
    if not seg.replace(_HOLE, "").strip(":"):
        return SLOT
    # An OData key reads the same entity set: BenefitInsurancePlan('abc') and BenefitInsurancePlan are one resource.
    seg = re.sub(r"\(.*\)$", "", seg)
    return seg.replace(_HOLE, SLOT)


def _path_of(url: str) -> tuple[str, ...]:
    url = re.sub(r"^[a-z][a-z0-9+.-]*://[^/]*", "", url.strip(), flags=re.I)
    url = re.split(r"[?#]", url, maxsplit=1)[0]
    return tuple(_segment(s) for s in url.split("/") if s)


def _core(path: tuple[str, ...]) -> tuple[str, ...]:
    p = path[1:] if path[:1] == ("api",) else path
    return tuple(s for s in p if not VERSION.match(s))


def _segments_match(agent: tuple[str, ...], ref: tuple[str, ...]) -> bool:
    # A placeholder where the reference names its version segment is the same address.
    return len(agent) == len(ref) and all(a == r or (a == SLOT and VERSION.match(r)) for a, r in zip(agent, ref))


def _in_address(needle: str, request: dict) -> bool:
    """Whether an answer string names part of a recorded request's address (or its SOAP element) rather than a
    parameter."""
    soap = (request.get("soap") or "").lower()
    if soap and needle.strip().lower() == soap:
        return True
    segs, core = _core(_path_of("/" + needle.strip().strip("/"))), _core(_path_of(request["path"]))
    return bool(segs) and any(_segments_match(core[i:i + len(segs)], segs) for i in range(len(core) - len(segs) + 1))


def _words(name: str) -> list[str]:
    return [w.lower() for w in re.split(r"[_\W]+|(?<=[a-z])(?=[A-Z])", name) if len(w) >= 3]


def asked_fields(row: dict) -> list[str]:
    """Parameters and body fields the reference adds over the starter that the ticket asks for."""
    gold, starter = recorded(row, "gold")["requests"], recorded(row, "starter")["requests"]
    if not gold:
        return []
    keys = lambda r: list(r.get("query", {})) + list(r.get("body_keys", []))
    had = {k.lower() for k in (keys(starter[0]) if starter else [])}
    ticket = ticket_text(row).lower()
    # An answer string that is part of the address (an endpoint name such as recordLearningEvents) is not a field ask.
    needles = " ".join(n for n in row["required"] + row["query_forbidden"] if not _in_address(n, gold[0])).lower()
    stem = lambda w: w[:-1] if w.endswith("s") else w
    ticket_words = {stem(w) for w in re.findall(r"[a-z]+", ticket)}
    out = []
    for k in keys(gold[0]):
        if k.lower() in had or k.lower() in PAGING:
            continue
        words = _words(k)
        asked = k.lower() in needles or (words and all(stem(w) in ticket_words for w in words))
        if asked and not any(f"optional {w}" in ticket for w in words):
            out.append(k)
    return out


# SAP prints some form-section keys both as sectIndex and sectionIndex.
LABEL_ALIAS = {'sectindex': 'sectionindex'}


def _answer_keys(inside: str) -> str:
    labels = (x.split('=')[0].strip().lower() for x in inside.split(',') if '=' in x)
    return '(' + ','.join(sorted(LABEL_ALIAS.get(l, l) for l in labels)) + ')'


def _segments(path: str) -> list[str]:
    path = re.sub(r'^https?://[^/]+', '', (path or '').split('?')[0]).rstrip('/')
    out = []
    for s in path.split('/'):
        s = re.sub(r'\(([^)]*)\)', lambda m: _answer_keys(m.group(1)), s)
        free = re.fullmatch(r'\{[^}]*\}|v\d+(\.\d+)?', s)
        if s:
            out.append('{}' if free else s.lower())
    return out[1:] if out[:1] == ['api'] else out


def _service(address: str) -> str:
    m = re.search(r'/ccx/service/[^/]+/([^/?]+)', address)
    assert m, f'a SOAP right answer without a Workday service: {address}'
    return m.group(1).lower()


def _expands(address: str) -> list[str]:
    """The top-level names a right answer expands ($expand=a,b/c,d(...)): the bare call returns only links to them."""
    m = re.search(r'[?&]\$expand=([^&]*)', address)
    if not m:
        return []
    names, depth, cur = [], 0, ''
    for ch in m.group(1) + ',':
        depth += (ch == '(') - (ch == ')')
        if ch == ',' and depth == 0:
            names.append(re.split(r'[(/]', cur)[0].strip())
            cur = ''
        else:
            cur += ch
    return [n for n in names if n]


def _op(s: str | None) -> str:
    return re.sub(r'_Request$', '', (s or '').strip())


def _same(got: list[str], want: list[str]) -> bool:
    # a placeholder in the right answer matches anything; the agent's own placeholder matches only a placeholder
    return len(got) == len(want) and all(a == b or b == '{}' for a, b in zip(got, want))


def _rest_route(call: dict, right: dict) -> bool:
    return ((call.get('method') or '').upper() == right['method'].upper()
            and _same(_segments(call.get('path') or ''), _segments(right['address'])))


def _rest_matches(call: dict, right: dict) -> bool:
    if not _rest_route(call, right):
        return False
    query = str(call.get('path')) + '&' + '&'.join(map(str, call.get('fields') or []))
    need = ([right['require_query']] if right.get('require_query') else []) + _expands(right['address'])
    return all(re.search(r'(?:^|[?&\s])\$?expand=[^&\s]*\b' + re.escape(n) + r'\b', query, re.I) for n in need)


def _norm(name: str) -> str:
    return re.sub(r'[^a-z0-9]', '', name.lower())


def _named(call: dict) -> set[str]:
    """Every name the call gives, without case or punctuation: each field and query parameter, and each dotted,
    indexed, spaced or quoted part of it (values too, since some agents write a whole body as one field), so
    "device token", "deviceToken" and "root.device_token" all name deviceToken."""
    path = str(call.get('path') or '')
    items = [str(f) for f in call.get('fields') or []] + (path.split('?', 1)[1].split('&') if '?' in path else [])
    out = set()
    for item in items:
        name = item.split('=', 1)[0]
        out.add(_norm(name))
        out.update(_norm(part) for part in re.split(r'[\s.,:;=\[\]{}()"\'/]+', item) if part)
    return out


def _unversioned(path: str) -> str:
    return re.sub(r'/v\d+(\.\d+)*(?=/|$|\?)', '', path or '', flags=re.I)


def asked_by_answer(row: dict, accepted: list[dict]) -> dict[int, list[str]]:
    """The fields the ticket asks for (`asked_fields`), owed by each right answer that is the reference fix's own
    request, with or without its version segment; another right answer may take other fields, so it owes none."""
    gold = recorded(row, 'gold')
    if not gold['requests']:
        raise RuntimeError(f"{row['task_id']}: the reference fix sent no request ({gold.get('error')}), so its asked fields are unknown")
    fields = asked_fields(row)
    if not fields:
        return {}
    sent = {'method': gold['requests'][0]['method'], 'path': _unversioned(gold['requests'][0]['path']), 'fields': []}
    return {i: fields for i, right in enumerate(accepted)
            if not right.get('soap') and _rest_route(sent, {**right, 'address': _unversioned(right['address'])})}


def grade_task(row: dict, answer: str | dict | None) -> bool:
    """`grade` with the ticket's asked fields; the reference fix is recorded only for an answer that is already a
    right call, so a wrong call costs no recording."""
    accepted = row['retrieval_key']['accepted']
    return grade(accepted, answer) and grade(accepted, answer, asked_by_answer(row, accepted))


def grade(accepted: list[dict], answer: str | dict | None, asked: dict[int, list[str]] | None = None) -> bool:
    """Whether the answer's call is one of the right answers. `asked` maps a right answer's position to the fields
    its call must name (`asked_by_answer`)."""
    try:
        call = json.loads(answer) if isinstance(answer, str) else (answer or {})
    except ValueError:
        return False
    if not isinstance(call, dict) or not (call.get('path') or call.get('operation')):
        return False
    if not accepted:
        raise ValueError('a question with no right answers')
    for i, right in enumerate(accepted):
        if right.get('soap'):
            # Workday owns each operation in one service; the call must be sent to that service
            sent_to = [x.lower() for x in re.split(r'[/?]', call.get('path') or '')]
            if call.get('operation') and _op(call['operation']) == _op(right['soap']) and _service(right['address']) in sent_to:
                return True
        elif _rest_matches(call, right) and all(_norm(f) in _named(call) for f in (asked or {}).get(i, [])):
            return True
    return False
