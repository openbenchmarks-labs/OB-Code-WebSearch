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
import xml.etree.ElementTree as ET
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

SEEN = []


def json_body(data, at=""):
    """Every field name in a JSON body, nested ones as dotted paths (items.quantity), and each scalar as path=value."""
    if isinstance(data, list):
        parts = [json_body(item, at) for item in data]
        return [n for p in parts for n in p[0]], [v for p in parts for v in p[1]]
    if not isinstance(data, dict):
        return [], ([f"{at[:-1]}={data}"] if at else [])
    names, values = [f"{at}{k}" for k in data], []
    for k, v in data.items():
        n, vals = json_body(v, f"{at}{k}.")
        names, values = names + n, values + vals
    return names, values


def xml_body(el, at=""):
    """The same for the elements under a SOAP request element, by local name."""
    names, values = [], []
    for child in el:
        path = at + child.tag.rsplit("}", 1)[-1]
        names.append(path)
        if len(child):
            n, v = xml_body(child, path + ".")
            names, values = names + n, values + v
        elif (child.text or "").strip():
            values.append(f"{path}={child.text.strip()}")
    return names, values


class Stand(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _any(self):
        n = int(self.headers.get("Content-Length") or 0)
        data = self.rfile.read(n) if n else b""
        raw = data.decode("utf-8", "replace")
        u = urlsplit(self.path)
        body_keys, body_values, soap = [], [], None
        ctype = self.headers.get("Content-Type") or ""
        if raw.lstrip().startswith("<"):
            m = re.search(r"<(?:[\w-]+:)?Body\b[^>]*>\s*<(?:[\w-]+:)?([A-Za-z]\w*)", raw)
            soap = m.group(1) if m else None
            try:
                body = next(e for e in ET.fromstring(raw).iter() if e.tag.rsplit("}", 1)[-1] == "Body")
                body_keys, body_values = xml_body(body[0])
            except (ET.ParseError, StopIteration, IndexError):
                pass
        elif ctype.startswith("multipart/form-data"):
            form = BytesParser(policy=policy.default).parsebytes(f"Content-Type: {ctype}\r\n\r\n".encode() + data)
            for part in form.iter_parts():
                name = part.get_param("name", header="content-disposition")
                body_keys.append(name)
                if not part.get_filename():
                    body_values.append(f"{name}={part.get_content().strip()}")
        elif raw:
            try:
                body_keys, body_values = json_body(json.loads(raw))
            except ValueError:
                body_keys = sorted(parse_qs(raw))
                body_values = [f"{k}={v[0]}" for k, v in parse_qs(raw).items()]
        SEEN.append({"method": self.command.lower(), "path": u.path, "query": {k: v[0] for k, v in parse_qs(u.query).items()},
                     "body_keys": sorted(set(body_keys)), "body_values": body_values, "soap": soap})
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
        env = {k: v for k, v in os.environ.items() if k.upper() in ("PATH", "HOME", "LANG", "VIRTUAL_ENV", "PYTHONPATH", "TMPDIR", "SYSTEMROOT")}
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
           "ukg": "UKG Pro Workforce Management", "wd": "Workday", "wds": "Workday", "vv": "Veeva Vault"}
# UKG publishes two products under one prefix; a question whose docs are UKG Pro HCM's names that product instead.
UKG_HCM_DOCS = "developer.ukg.com/hcm/"

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
                       "description": "Query parameters or body fields (SOAP: request elements) the ticket needs sent, as name, or name=value when the ticket or the docs fix the value (for example $expand=someNavigation); empty if none."},
        },
        "required": ["method", "path", "operation", "fields"],
        "additionalProperties": False,
    },
}

_RULES = """Answer with the request only: method, path, SOAP operation if the API is SOAP, and the query parameters or body fields (for SOAP, the request elements) the ticket needs. Do not write code. Write any value the caller supplies as {name}. When the ticket or the docs fix a value (a name, a date, a status, a navigation to expand, an action name), give it as name=value."""
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
    if vendor == VENDORS["ukg"] and UKG_HCM_DOCS in row.get("source_url", ""):
        vendor = "UKG Pro HCM"
    return (f"Vendor: {vendor}\n" if vendor else "") + f"The program sends this request today: {now}\n" + \
        f"Ticket: {ticket_text(row)}\n\nWhich request does the ticket need?"


# SAP prints some form-section keys both as sectIndex and sectionIndex.
LABEL_ALIAS = {'sectindex': 'sectionindex'}


def _answer_keys(inside: str) -> str:
    """The keys in parentheses as label=value, sorted by label; a key written by position has an empty label. A value
    is a placeholder ({}) when it is one, or empty; otherwise it is kept, so a key that fixes a value (('ACTIVE')) needs it."""
    out = []
    for part in inside.split(','):
        label, eq, value = part.partition('=')
        if not eq:
            label, value = '', part
        label = label.strip().lower()
        out.append(f'{LABEL_ALIAS.get(label, label)}={_key_value(value)}')
    return '(' + ','.join(sorted(out)) + ')'


def _key_value(value: str) -> str:
    value = re.sub(r"^(datetime|guid)(?=')", '', value.strip(), flags=re.I).strip('\'"')
    value = re.sub(r'^(\d+)[lL]$', r'\1', value)
    return '{}' if not value or '{' in value else value.lower()


VERSION = '{version}'


def _segments(path: str) -> list[str]:
    path = re.sub(r'^https?://[^/]+', '', (path or '').split('?')[0]).rstrip('/')
    out = []
    for s in path.split('/'):
        s = re.sub(r'\(([^)]*)\)', lambda m: _answer_keys(m.group(1)), s)
        if s:
            # any API version matches any other, but a version is not a placeholder: it holds a version's place only
            out.append('{}' if re.fullmatch(r'\{[^}]*\}', s) else VERSION if re.fullmatch(r'v\d+(\.\d+)?', s, re.I)
                       else s.lower())
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
    # a placeholder in the right answer matches anything, a whole segment or the part of one it stands for
    # (Entity_{templateId}); the agent's own placeholder matches only a placeholder. A version in the right answer
    # matches a version or the agent's placeholder for one ({api_version}), never another segment.
    return len(got) == len(want) and all(a == b or (b == VERSION and a == '{}') or b == '{}'
                                         or (b != VERSION and '{' in b and re.fullmatch(_part(b), a))
                                         for a, b in zip(got, want))


def _part(segment: str) -> str:
    return '[^/,()]+'.join(re.escape(p) for p in re.split(r'\{[^}]*\}', segment))


def _rest_route(call: dict, right: dict) -> bool:
    """Same method and path; a right answer with `match: prefix` names a service root, and any path under it matches."""
    got, want = _segments(call.get('path') or ''), _segments(right['address'])
    if right.get('match') == 'prefix':
        got = got[:len(want)]
    return (call.get('method') or '').upper() == right['method'].upper() and _same(got, want)


def _rest_matches(call: dict, right: dict) -> bool:
    if not _rest_route(call, right):
        return False
    query = str(call.get('path')) + '&' + '&'.join(map(str, call.get('fields') or []))
    need = ([right['require_query']] if right.get('require_query') else []) + _expands(right['address'])
    if not all(re.search(r'(?:^|[?&\s])\$?expand=[^&\s]*\b' + re.escape(n) + r'\b', query, re.I) for n in need):
        return False
    return _gives_all(call, right)


def _norm(name: str) -> str:
    return re.sub(r'[^a-z0-9]', '', name.lower())


def _entries(call: dict) -> list[str]:
    """The call's fields and the parameters in its path's query string."""
    path = str(call.get('path') or '')
    return [str(f) for f in call.get('fields') or []] + (path.split('?', 1)[1].split('&') if '?' in path else [])


_PAIR = re.compile(r'(["\']?)([\w$@.\[\]/-]+)\1\s*([=:])\s*')
_NEXT = re.compile(r'&|,\s*["\']?[\w$@.\[\]/-]+["\']?\s*[=:]')


def _values(entry: str) -> list[tuple[str, str]]:
    """(name, value) for each `name=value` or `"name": value` in an entry; the name is the last dotted, indexed or
    slashed segment, so `postingStatuses[0].status=FAILED` gives ("status", "FAILED")."""
    out = []
    for m in _PAIR.finditer(entry):
        rest = entry[m.end():]
        if rest and rest[0] in '"\'':
            raw = rest[1:].split(rest[0], 1)[0]
        elif m.group(3) == '=':
            raw = _NEXT.split(rest, 1)[0]
        else:
            raw = re.split(r'[,}\]]', rest, maxsplit=1)[0]
        segments = [s for s in re.split(r'[.\[\]/]', m.group(2)) if s]
        if segments:
            out.append((_norm(segments[-1]), raw.strip().strip('"\'')))
    return out


def _gives(entry: str, field: str) -> bool:
    """Whether one entry names a field the key lists (`name`, or `name=value` when the ticket fixes the value). A
    name compares without case or punctuation against the entry's name and each dotted, indexed, spaced or quoted part
    of it (values too, since some agents write a whole body as one entry), so "device token", "deviceToken" and
    "root.device_token" all name deviceToken. A value must be the one given for that name in the entry, the same
    without case or punctuation (a date may carry a time after it); a placeholder or a list of choices is no value."""
    name, _, value = field.partition('=')
    if not value:
        parts = {_norm(entry.split('=', 1)[0])} | {_norm(p) for p in re.split(r'[\s.,:;=\[\]{}()"\'/]+', entry) if p}
        return _norm(name) in parts
    want = _norm(value)
    date = re.fullmatch(r'\d{4}-\d{2}-\d{2}', value)
    for got_name, got in _values(entry):
        if got_name != _norm(name) or '{' in got or '|' in got:
            continue
        if _norm(got) == want or (date and _norm(got).startswith(want)):
            return True
    return False


def _gives_all(call: dict, right: dict) -> bool:
    entries = _entries(call)
    return all(any(_gives(e, field) for e in entries) for field in right.get('fields') or [])


def grade(accepted: list[dict], answer: str | dict | None) -> bool:
    """Whether the answer's call is one of the right answers, including the fields that right answer lists."""
    try:
        call = json.loads(answer) if isinstance(answer, str) else (answer or {})
    except ValueError:
        return False
    if not isinstance(call, dict) or not (call.get('path') or call.get('operation')):
        return False
    if not accepted:
        raise ValueError('a question with no right answers')
    for right in accepted:
        if right.get('soap'):
            # the call must be sent to a service that publishes the operation; a key lists one right answer per service
            sent_to = [x.lower() for x in re.split(r'[/?]', call.get('path') or '')]
            if (call.get('operation') and _op(call['operation']) == _op(right['soap'])
                    and _service(right['address']) in sent_to and _gives_all(call, right)):
                return True
        elif _rest_matches(call, right):
            return True
    return False
