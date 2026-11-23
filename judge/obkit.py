"""obkit entry points: grade one answer, and the LLM verifier's second reading of it."""

from __future__ import annotations

import html
import http.client
import json
import re
import threading
import urllib.parse
import urllib.request
from functools import cache
from pathlib import Path

import yaml

from .checks import grade as grade_call

HERE = Path(__file__).resolve().parent
PROMPT = (HERE / "verifier_prompt.txt").read_text()
CONFIG = yaml.safe_load((HERE / "config.yaml").read_text())
VERDICTS = {"PASS": True, "FAIL": False}


def grade(question: dict, answer: str) -> bool:
    return grade_call(question["retrieval_key"]["accepted"], answer)


def _read(reader: dict, user: str) -> tuple[str, float]:
    """One reader's PASS, FAIL or UNSURE through the Bedrock Converse API, and what the call cost."""
    import os

    import requests

    region, token = os.environ.get("AWS_REGION"), os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
    if not region or not token:
        raise RuntimeError("set AWS_REGION and AWS_BEARER_TOKEN_BEDROCK for the verifier")
    response = requests.post(
        f"https://bedrock-runtime.{region}.amazonaws.com/model/{reader['model']}/converse",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={
            "system": [{"text": PROMPT}],
            "messages": [{"role": "user", "content": [{"text": user}]}],
            "inferenceConfig": {"maxTokens": reader["max_tokens"]},
        },
        timeout=120,
    )
    response.raise_for_status()
    body = response.json()
    text = "".join(part.get("text", "") for part in body["output"]["message"]["content"])
    price = reader["usd_per_mtok"]
    cost = (body["usage"]["inputTokens"] * price["input"] + body["usage"]["outputTokens"] * price["output"]) / 1e6
    return ((text.strip().splitlines() or [""])[-1].strip().upper()), cost


def verify(question: dict, answer: str, passed: bool, *, dry: bool) -> dict:
    """agree, disagree or unsure with the deterministic verdict, and what the calls cost. The first reader reads every
    answer; the second reads again only where the first disagrees or is unsure, and its reading stands. A disagree or
    unsure goes to a person, who rules from the vendor documentation. It never changes a score."""
    if dry:
        return {"verdict": "agree", "cost_usd": 0.0}
    from harness.obkit import load_environment

    load_environment()
    right = "\n".join(
        f"{r['method']} {r['address']}" + (f" (SOAP {r['soap']})" if r.get("soap") else "")
        for r in question["retrieval_key"]["accepted"]
    )
    user = (
        f"Task:\n{question['ticket']}\n\nRight answers:\n{right}\n\nAgent's call:\n{answer}\n\n"
        "End with one line: PASS, FAIL or UNSURE."
    )
    cost = 0.0
    for reader in (CONFIG["verifier"]["first"], CONFIG["verifier"]["second"]):
        word, spent = _read(reader, user)
        cost += spent
        verdict = "unsure" if word not in VERDICTS else "agree" if VERDICTS[word] == passed else "disagree"
        if verdict == "agree":
            break
    return {"verdict": verdict, "cost_usd": cost}


SAP_TOPIC = re.compile(r"https://help\.sap\.com/docs/([^/]+)/([^/]+)/([^/?#]+)")
SAP_LOCK = threading.Lock()  # help.sap.com answers parallel requests with 403 Access Denied


def _fetch(url: str, timeout: int = 20) -> str | None:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8", "replace")
    except (OSError, ValueError, http.client.HTTPException):
        return None


@cache
def _page(url: str) -> str | None:
    """A docs page as plain text, or None when it cannot be read without a browser. SAP Help draws its pages with
    scripts, so its pages are read from the page content service the scripts load."""
    url = url.split("#")[0]
    if topic := SAP_TOPIC.match(url):
        product, deliverable, file = topic.groups()
        found = {"product_url": product, "deliverable_url": deliverable, "topic_url": file, "version": "LATEST"}
        with SAP_LOCK:
            meta = _fetch("https://help.sap.com/http.svc/deliverableMetadata?" + urllib.parse.urlencode(found))
        try:
            data = json.loads(meta)["data"]
            ask = {"deliverableInfo": 1, "deliverable_id": data["deliverable"]["id"], "buildNo": data["deliverable"]["buildNo"]}
            with SAP_LOCK:
                content = _fetch("https://help.sap.com/http.svc/pagecontent?" + urllib.parse.urlencode({**ask, "file_path": data["filePath"]}))
            return _text(json.loads(content)["data"]["body"]) or None
        except (TypeError, ValueError, KeyError):
            return None
    text = _fetch(url)
    if text is None:
        return None
    text = _text(text)
    return text if len(text) >= 2000 else None


def _text(page: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", page)).replace("\\", "")


def _shows(right: dict, page: str) -> bool:
    """Whether a docs page writes the accepted request: the SOAP operation; else the last three path segments,
    with any placeholder or version standing for whatever the page puts there and any key in parentheses after a
    segment, as in `User('cgrant')/teamMembersSize`. An OData entity set or function import (`/odata/v2/<Name>`)
    is often shown only by name on its reference page, so the bare name counts when no URI on the page puts it
    under another path (`/odata/v2/restricted/<Name>`)."""
    if right.get("soap"):
        return right["soap"] in page
    path = re.sub(r"\([^)]*\)", "", right["address"].split("?")[0]).strip("/").split("/")
    if len(path) == 3 and path[0] == "odata" and re.fullmatch(r"v\d+", path[1]):
        name = re.escape(path[2])
        if re.search(rf"odata/v\d+/\s*{name}\b", page, re.IGNORECASE):
            return True
        return not re.search(rf"/\s*{name}\b", page) and bool(re.search(rf"\b{name}\b", page))
    parts = [r"[^/\s]*" if p.startswith("{") or re.fullmatch(r"v\d+(\.\d+)?", p) else re.escape(p) for p in path[-3:]]
    return bool(re.search(r"(?:\([^)]*\))?/\s*".join(parts), page, re.IGNORECASE))


def key_check(question: dict):
    """True when an accepted request appears on its docs page, False when every page was read and none shows one,
    None when a page needs a browser."""
    key = question["retrieval_key"]
    pages = [_page(url) for url in (key.get("docs") or [question["source_url"]])]
    readable = [page for page in pages if page is not None]
    if any(_shows(right, page) for right in key["accepted"] for page in readable):
        return True
    if len(readable) < len(pages):
        return None
    shown = "; ".join(f"{r['method']} {r['address']}" for r in key["accepted"])
    return f"{shown} is not on {', '.join(key.get('docs') or [question['source_url']])}"
