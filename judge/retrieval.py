"""Retrieval-only scoring of delivered evidence, with explicit named list policies.

The reviewed reference keys ship with each task row as retrieval_key.
No ticket prose, model answer, raw provider body or fetch URL argument is evidence.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import dataclass
from functools import cache
from itertools import chain
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

from .checks import VERSION, Op, _shown_pattern, path_of

if TYPE_CHECKING:
    from harness.run import CodingTask

POLICY_VERSIONS = {"L0": "L0-v2.5.4", "L1": "L1-v3.5.4"}
SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes() + Path(__file__).with_name('checks.py').read_bytes()).hexdigest()


def _key_sha256(key: dict) -> str:
    return hashlib.sha256(json.dumps(key, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def retrieval_identity(*, policy: str, key: dict) -> dict[str, str]:
    """Identify the loaded scorer and reviewed key used for grading."""
    if policy not in POLICY_VERSIONS:
        raise ValueError('retrieval policy must be explicitly L0 or L1')
    return {'policy': policy, 'judge_version': POLICY_VERSIONS[policy], 'source_sha256': SOURCE_SHA256, 'key_sha256': _key_sha256(key)}


NOUN = r'(?:OData(?:\s+API)?\s+)?(?:entity(?:\s+sets?)?|entities|function\s+imports?|collection|API)'
OLD_NOUN = r'(?:function[^\S\n]+imports?|entity[^\S\n]+sets?|entity|entities|collection|API)'
MARKUP = r'[ \t`\'"“”:]'
METHOD = r'(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)'
NEGATIVE = re.compile(r'\b(?:table of contents|contents|navigation|menu)\b', re.I)
LABEL = re.compile(r'\b(?:API\s+operations?|(?:OData(?:\s+v\d+)?(?:\s+API)?\s+)?entities|entity\s+sets?|function\s+imports?|web\s*services?)\b', re.I)
COLUMN = re.compile(r'^(?:OData\s+)?(?:API\s+)?(?:Entity|Entities|Entity\s+Sets?|Function\s+Imports?)(?:\s*[-–—]\s*Description\s+and\s+ID)?$', re.I)
ADDR = re.compile(r"(?:https?://|\{\{?[\w.-]*\}?\}|\B)/[\w{}.:%-]+/[\w{}.:%/()'-]+")
BEFORE = re.compile(
    r'((?<![\w-])(?i:GET|POST|PUT|PATCH|DELETE)|(?<=[a-z])(?:GET|POST|PUT|PATCH|DELETE))(?![\w-])'
    r'(?:[ \t`*"\'\[\]|:>(,]*|[ \t]*(?:\\\s*)?(?:-[A-Za-z]\s+(?:"[^"\n]*"|\'[^\'\n]*\'|\S+)\s*(?:\\\s*)?)*)'
    r'(?:\n[ \t`*>|]*){0,2}'
    r'[ \t`*"\'\[|(]*(?:https?://[^\s/]*|\{\{?[\w.-]*\}?\}|[^\s/]*)?/?[^\s]*$'
)
CURL = re.compile(r'curl\b[^\n]*(?:\\\s*\n[^\n]*){0,6}$', re.I)
AFTER = re.compile(
    r'^[^\s]*?["\'`]?\s*(?:\\\s*)?(?:[:|,{]\s*)*(?:\n\s*)?(?:\{\s*)?(?:--request\s+|-X\s*|(?i:the method is)\s+|(?i:method):\s*)?'
    r'(?:["\']((?i:GET|POST|PUT|PATCH|DELETE))["\']|(GET|POST|PUT|PATCH|DELETE)(?![\w-]))'
)
WS = r'web ?services?|webservices?|servizi web|servi[cç]os web|servicios? web|\bservices?[ \t]+web\b|\bsoap\b|\bwsdl\b|\boperations?\b|request element|response element'
NONOP = r'\btasks?\b|business process(?:es)?|\breports?\b|\breporting\b|\bdomains?\b|\beib\b|\brelated actions?\b|\bdashboard\b'
MARK = re.compile(rf'(?P<ws>{WS})|(?P<no>{NONOP})', re.I)
DESCRIBES = r'indicates|contains|stores|holds|represents|describes|defines|specifies|records|captures|is used|lets|allows|enables|provides|returns|lists|manages|maintains|tracks'


# Documented routes whose same path has another documented method. Without a method word,
# such an address must be clearly for its own action.
SIBLING_METHODS = {
    'get /sn_devops/devops/orchestration/stepmapping': ('post',),
}
ACTIONS = {'get': r'get|gets|retrieves?|returns?|lists?|reads?|query|queries|fetch(?:es)?|views?|search(?:es)?|look[ \t]+up',
           'delete': r'deletes?|removes?',
           'post': r'creates?|adds?|submits?|starts?|launch(?:es)?|triggers?|sends?|posts?|runs?|executes?|imports?|uploads?|refresh(?:es)?|initiates?|approves?|declines?|recalculates?|synchroni[sz]es?',
           'put': r'updates?|replaces?|modif(?:y|ies)|edits?|patch(?:es)?|sets?|changes?|amends?'}
ACTIONS['patch'] = ACTIONS['put']


@dataclass(frozen=True)
class EvidenceHit:
    url: str
    title: str
    text: str
    marker: str
    source: str


@dataclass(frozen=True)
class Answer:
    op: Op
    name: str
    tail: tuple[str, ...]
    pattern: re.Pattern[str]
    words: tuple[str, ...]
    method_shown: bool
    required_query: tuple[tuple[str, str], ...]


def _get(value: Any, key: str, default: Any = None) -> Any:
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def delivered_hits(search_hops: list[Any], fetch_hops: list[Any]) -> list[EvidenceHit]:
    """Read retained search fields and successful fetch titles/full content."""
    hits = []
    for i, hop in enumerate(search_hops, 1):
        for j, hit in enumerate(_get(hop, 'hits', []) or [], 1):
            hits.append(EvidenceHit(str(_get(hit, 'url', '') or ''), str(_get(hit, 'title', '') or ''), str(_get(hit, 'snippet') or _get(hit, 'text') or _get(hit, 'content') or ''), f'{i}.{j}', 'search'))
    for i, hop in enumerate(fetch_hops, 1):
        if _get(hop, 'error'):
            continue
        body = _get(hop, 'content')
        if body is None:
            if _get(hop, 'preview'):
                raise ValueError('full delivered fetch content is missing; preview is not regrading evidence')
            continue
        title = str(_get(hop, 'title', '') or '')
        if str(body).strip() or title.strip():
            hits.append(EvidenceHit(str(_get(hop, 'url', '') or ''), title, str(body), f'fetch.{i}', 'fetch'))
    return hits


def _odata(op: Op) -> bool:
    return any(re.fullmatch(r'odata(?:v?\d+)?', part) for part in op.path)


def _request_pattern(op: Op, tail: tuple[str, ...], navigation: tuple[str, ...] = ()) -> re.Pattern[str]:
    original = _shown_pattern(op, tail)
    if op.soap:
        return original
    end = r'(?!/?[\w{:(])'
    if not original.pattern.endswith(end):
        raise ValueError('operation-pattern contract changed')
    body = original.pattern[:-len(end)]
    body = body.replace(r'(?<![A-Za-z0-9_])', r'(?<![A-Za-z0-9_-])', 1)
    boundaries = [r'(?!/?[\w{:(-])', r'(?=:(?:\s|$))']
    if navigation:
        boundaries.append(r'(?=/(?:' + '|'.join(re.escape(word) for word in navigation) + r')(?![\w]))')
    return re.compile(body + '(?:' + '|'.join(boundaries) + ')', original.flags)


def _reference(key: dict) -> tuple[dict, tuple[Answer, ...]]:
    answers = []
    for n, data in enumerate(key['answers']):
        raw = data['op']; op = Op(raw['method'], tuple(raw['path']), raw.get('soap'))
        tail = tuple(data['tail']); words = tuple(data['with'])
        pattern = _request_pattern(op, tail, words if _odata(op) else ())
        if data['prefix']:
            pattern = re.compile(r'(?<![\w])' + re.escape('/'.join(tail)) + r'(?![\w])', re.I)
        query = tuple((k, str(v).lower() if isinstance(v, bool) else str(v)) for k, v in data.get('required_query', {}).items())
        accepted = key['accepted'][n]
        name = accepted['soap'].removesuffix('_Request') if op.soap else urlsplit(accepted['address']).path.rstrip('/').rsplit('/', 1)[-1].split('(', 1)[0]
        answers.append(Answer(op, name, tail, pattern, words, data['method_shown'], query))
    return key, tuple(answers)


def _split_description(text: str, title: str) -> str:
    heading = re.fullmatch(r'\s*(Create|Delete)\s+(.+?)\s*', title, re.I)
    if not heading:
        return text
    description = re.escape(heading[1] + 's') + r'\s+' + re.escape(heading[2]) + r'\b'
    return re.sub(r'(https?://[^\s<>`"]+?)(' + description + r')', r'\1 \2', text, flags=re.I)


def _documentation_hosts(key: dict) -> set[str]:
    return {urlsplit(url).netloc.lower().removeprefix('www.') for url in key['docs'] + [key['source_url']] if url}


def _split(url: str):
    try:
        return urlsplit(url)
    except ValueError:
        return None


def _documentation_link(url: str, hosts: set[str]) -> bool:
    parsed = _split(url)
    return parsed is not None and parsed.netloc.lower().removeprefix('www.') in hosts and not re.search(r'/(?:api/|odata(?:v?\d+)?/)', parsed.path, re.I)


@cache
def _wrapped_name(name: str) -> re.Pattern[str]:
    pieces = [re.escape(name[:i]) + r'[ \t]*\r?\n[ \t]*' + re.escape(name[i:]) for i in range(1, len(name))]
    return re.compile(r'(?<!\w)(?:' + '|'.join(pieces) + r')(?!\w)')


def _decode(text: str) -> str:
    text = html.unescape(text) if '&' in text else text
    return re.sub(r'\\([{}])', r'\1', re.sub('[\u00ad\u200b\u200c\u200d\u2060\ufeff]', '', text))


def _normalize_text(answer: Answer, text: str, hosts: set[str]) -> tuple[str, list[dict]]:
    changes = []
    decoded = _decode(text)
    if decoded != text:
        changes.append({'kind': 'character encoding', 'raw_sha256': hashlib.sha256(text.encode()).hexdigest()})
        text = decoded
    def link(match):
        if not _documentation_link(match[2], hosts):
            return match[0]
        changes.append({'kind': 'documentation link label', 'raw': match[0], 'normalized': match[1]})
        return match[1]
    text = re.sub(r'\[([^\]\n]+)\]\((https?://[^\s)]+)\)', link, text)
    if answer.op.soap:
        return text, changes
    def address(match):
        normalized = re.sub(r'/(?:[ \t]*\r?\n[ \t]*|[ \t]+)', '/', match[0])
        changes.append({'kind': 'address slash wrap', 'raw': match[0], 'normalized': normalized})
        return normalized
    # PDF line wrapping leaves a slash followed by a line break or by spaces. A space continuation is joined only
    # when the next token is itself a path piece (has '/', '_' or a template brace), never a following prose word.
    wrap = r'(?:[ \t]*\r?\n|[ \t])'
    segment = r'(?:[^\s/<>`"]|/(?!' + wrap + r'))*'
    text = re.sub(r'(?:https?://|/)' + segment + r'(?:/(?:[ \t]*\r?\n[ \t]*(?=[A-Za-z_{])|[ \t]+(?=[A-Za-z_{][^\s/]*[/_{}]))' + segment + r')+', address, text)
    # Search highlights put spaces on both sides of a slash ("POST / api /v2/ documentation / search").
    # Only a run made of slash-separated path pieces is joined, and only when some slash has a space before it,
    # which a PDF wrap or a prose sentence after an address ("/oneTimePaymentPlan/ retrieves") never has.
    def spaced(match):
        normalized = re.sub(r'[ \t]*/[ \t]*', '/', match[0])
        if not re.search(r'[ \t]/', match[0]) or normalized.count('/') < 3 or len(re.findall(r'(?:^|/)api/', normalized)) > 1:
            return match[0]
        changes.append({'kind': 'address highlight spacing', 'raw': match[0], 'normalized': normalized})
        return normalized
    piece = r'[^\s/<>`"|,;]+'
    text = re.sub(r'(?<!\S)(?:(?:https?://[^\s/]+)?/[ \t]*|(?=[^\s/]+/))' + piece + r'(?:[ \t]*/[ \t]*' + piece + r')+/?', spaced, text)
    if len(answer.tail) == 1 and len(answer.name) > 1:
        for match in reversed(list(_wrapped_name(answer.name).finditer(text))):
            normalized = text[:match.start()] + answer.name + text[match.end():]
            start = normalized.rfind('\n', 0, match.start()) + 1
            context = normalized[start:]
            named = answer.pattern.match(context, match.start()-start)
            if named is None:
                continue
            end = context.find('\n', named.end())
            if end != -1 and not re.match(r'\r?\n[ \t]*(?:This|The)[ \t]+' + NOUN + r'\b', context[end:], re.I):
                context = context[:end]
            if _entity_relation(context, named):
                changes.append({'kind': 'identified name wrap', 'raw': match[0], 'normalized': answer.name})
                text = normalized
    return text, changes


def _composed_requests(text: str, title: str):
    headings = []
    namespace = None
    base = None
    owners = set()
    base_depth = 0
    for line in text.splitlines():
        heading = re.match(r'^(#{1,6})[ \t]+(.+?)\s*$', line)
        operation = re.fullmatch(r'(?:#{1,6}[ \t]+)?(.+?)[ \t]+-[ \t]+' + METHOD + r'[ \t]+(/[^\s`<>"]+)[ \t]*', line)
        if operation and base and operation[1] in owners:
            request = operation[2] + ' ' + base[1].rstrip('/') + '/' + operation[3].lstrip('/')
            yield 'snippet', request, [{'kind': 'explicit base and operation', 'raw': [base[0], line], 'owner': operation[1], 'namespace': namespace, 'normalized': request}]
        if heading:
            depth = len(heading[1])
            if base and depth <= base_depth and not operation:
                base = None
            headings = [(level, name) for level, name in headings if level < depth]
            headings.append((depth, heading[2]))
        declared = re.fullmatch(r'[ \t]*(?:[-*][ \t]+)?Namespace:[ \t]*([\w.-]+)[ \t]*', line, re.I)
        if declared:
            namespace = declared[1]
        declared = re.fullmatch(r'[ \t]*(?:[-*][ \t]+)?Base[ \t]+URI:[ \t]*([^\s`<>"]+)[ \t]*', line, re.I)
        if declared:
            base = declared
            owners = {name for _, name in headings}
            base_depth = headings[0][0] if headings else 0
            parsed = _split(base[1])
            if namespace and parsed is not None and namespace in parsed.path.split('/'):
                owners.add(title.strip())


def _entity_case(address: str, answer: Answer, text: str, match: re.Match[str], navigation: bool) -> bool:
    """An OData entity followed by a navigation step must be written in the entity's own letter case."""
    if not _odata(answer.op):
        return True
    if navigation and re.match(r'/(?:' + '|'.join(re.escape(word) for word in answer.words) + r')(?!\w)', text[match.end():], re.I):
        entity = answer.name
    else:
        parts = [part.split('(', 1)[0] for part in urlsplit(address).path.split('/') if part]
        lowered = [part.lower() for part in parts]
        if len(answer.tail) < 2 or answer.tail[0] not in lowered:
            return True
        at = lowered.index(answer.tail[0])
        if at + 1 >= len(parts) or not parts[at][:1].isupper() or not parts[at + 1][:1].islower():
            return True
        entity = parts[at]
    shown = [part.split('(', 1)[0] for part in match.group().split('/')]
    return all(part == entity for part in shown if part.lower() == entity.lower())


def _request_identity(answer: Answer, text: str, match: re.Match[str], hosts: set[str], roots: set[str]) -> bool:
    for address in re.finditer(r'https?://[^\s`"]+|(?<!\w)#[^\s`"<>]+', text):
        if address.start() <= match.start() < address.end():
            if address[0].startswith('#') or _documentation_link(address[0], hosts):
                return False
    if len(answer.tail) == 1 and _odata(answer.op):
        address = re.search(r'([^\s"<>`]+/)$', text[:match.start()])
        if address:
            prefix = path_of(address[1])
            expected = tuple(part for part in answer.op.path[:-1] if not VERSION.fullmatch(part))
            actual = tuple(part for part in prefix if not VERSION.fullmatch(part))
            if actual[-len(expected):] != expected:
                if re.search(r'\([^()\n/]*\)/', address[1]) or any(part in roots for part in actual):
                    return False
    return True




def _tags(name: str, url: str, title: str, field: str, text: str, match: re.Match[str] | None) -> set[str]:
    if match is None:
        return set()
    n = re.escape(name); tags = set()
    if match.group().split('(')[0] == name:
        tags.add('case_exact')
    slug = url.split('#')[0].split('?')[0].rstrip('/').rsplit('/', 1)[-1]
    if re.sub(r'\.(html?|htm)$', '', slug).lower() == name.lower():
        tags.add('own_url')
    if re.fullmatch(rf'(?:[\d.]+\s+)?{n}(?:\s*[|–—-].*)?', title.strip()):
        tags.add('own_title')
    if field != 'snippet':
        return tags
    pre = text[max(0, match.start()-80):match.start()]; post = text[match.end():match.end()+200]
    if re.search(r'(?:\[Link\]|\bURI)[^\S\n]*$', pre) or re.match(r"\((?:\d+L?|'[^']*'|\w+=)|\s?\?\s?\$?\w+=", text[match.start()+len(name):match.start()+len(name)+20]):
        tags.add('addr_token')
    if re.search(rf'\b{OLD_NOUN}[^\S\n]+\Z', pre, re.I) or re.match(rf'[^\S\n"”]*\b{OLD_NOUN}\b', post, re.I) or re.match(r'[^\S\n]*(?:This|The)[^\S\n]+(?:entity|enitity|object|function import)\b', post):
        tags.add('api_noun')
    identified_after = re.match(rf'[\u200b\xa0 \t]*(?:\[[^\]\n]{{0,20}}\][^\S\n]*)?(?:\n[\s\u200b]*)?(?:[^.\n]{{0,40}}?\b)?(?:[Tt]his|The)[^\S\n]+(?:{n}[^\S\n]+)?(?:entity|enitity|object|function import)\b', post)
    if identified_after or re.search(r'\b(?:[Tt]his|a new|[Tt]he)[^\S\n]+(?:entity|object|function import)\b[^.\n]*\.\s*$', pre):
        tags.add('this_entity')
    # A list separator before the name does not make it a list entry when the very next words identify it as
    # the entity being described; another list item in between does.
    described = re.match(r'[^\S\n]*(?:[^.\n·•,;|]{0,40}?\b)?(?:[Tt]his|The)[^\S\n]+(?:entity|enitity|object|function import)\b', post)
    # The page describes the item itself: its name opens a line or sentence and is the subject of a describing
    # verb ("PensionEmployeeContributionDetail indicates whether ..."). A name in the middle of a sentence is a passing mention.
    if re.search(r'(?:^|\n|[.!?:][^\S\n]+)[^\S\n]*$', pre) and re.match(r'[^\S\n]+(?:' + DESCRIBES + r')\b', post):
        tags.add('this_entity')
        described = True
    if re.search(r'["“]\s*$', pre) and re.match(r'\s*["”]\s*[:,}]', post):
        tags.add('json')
    sep = r'(?:[^\S\n]*(?:[.,;|·•]|\.\.+|\s\.)[^\S\n]*|[^\S\n]*\n[^\S\n]*|[^\S\n]{2,})'; ident = r'[A-Za-z][A-Za-z0-9_]{3,}'
    if not described and ((re.search(rf'{ident}{sep}$', pre) and re.match(rf'{sep}{ident}', post)) or re.match(r'[^\S\n]*[,·•][^\S\n]*[A-Za-z]\w{3,}', post) or re.search(r'[A-Za-z]\w{3,}[^\S\n]*[,·•][^\S\n]*$', pre)):
        tags.add('list')
    return tags


def _entity_relation(text: str, m: re.Match[str]) -> bool:
    pre = text[max(0, m.start()-300):m.start()]; post = text[m.end():m.end()+160]
    if re.search(r'\b' + NOUN + r'(?:[ \t]+(?:from|in))?' + MARKUP + r'*\Z', pre, re.I):
        return True
    lead = r'[ \t`\'"“”]*(?:[·:][ \t]*)?(?:\r?\n[ \t]*)?'
    if re.match(lead + r'(?:(?:is|as)[ \t]+)?(?:(?:a|the)[ \t]+)?(?:(?:parent|source|target|MDF)[ \t]+)?' + NOUN + r'\b(?![ \t]+(?:Reference|Guide|Dictionary|Content)\b)', post, re.I) or re.match(lead + r'(?:This|The)[ \t]+' + NOUN + r'\b', post, re.I):
        return True
    return bool(re.search(r'\b' + NOUN + r'[ \t]+that\b[^.!?\n]*\bis[ \t`\'"“”]*$', pre, re.I) or re.search(r'\bentities\b[^.!?\n]*,\s*for example[ \t`\'"“”]*$', pre, re.I))


def _list_fragment(line: str) -> bool:
    line = re.sub(r'^\s*[-*•]\s*', '', line).strip().strip('`|,;:·• ')
    if not line or re.fullmatch(r'[-|:\s]+', line) or re.fullmatch(r'[A-Za-z_]\w*(?:\s*[,;|.·•]\s*(?:and\s+)?[A-Za-z_]\w*)*[\s,;|.·•]*', line):
        return True
    words = line.split()
    return bool(words and all(re.fullmatch(r'[A-Za-z_]\w*', w) and ('_' in w or re.search(r'[a-z][A-Z]', w)) for w in words))


def _typed_table(text: str, m: re.Match[str]) -> bool:
    start = text.rfind('\n', 0, m.start()) + 1; end = text.find('\n', m.end()); current = text[start:end if end != -1 else len(text)]; before = text[start:m.start()]
    if re.search(r'\bDimension\s+API\s+Entity\s*[-–—]\s*Description\s+and\s+ID\b.*[-–—]\s*$', before, re.I):
        return True
    if '|' not in current or _metadata_table(text, start):
        return False
    column = before.count('|')
    for line in reversed(text[:start].splitlines()):
        if not line.strip():
            continue
        if '|' not in line:
            return False
        cells = [c.strip().strip('*` ') for c in line.split('|')]
        if column < len(cells) and COLUMN.fullmatch(cells[column]):
            return True
        if not re.fullmatch(r'[\s|:-]+', line) and any(COLUMN.fullmatch(c) for c in cells):
            return False
    return False


def _metadata_table(text: str, start: int) -> bool:
    """An OData $metadata container table (Containers, EntityContainer, Entity Sets) lists entity sets without describing them."""
    for line in reversed(text[:start].splitlines()):
        if line.strip() and '|' not in line:
            return bool(re.match(r'[#*\s]*(?:Containers|EntityContainer|Entity[ \t]*Sets)\b', line, re.I))
    return False


def _prose_identification(text: str, m: re.Match[str]) -> bool:
    """Recognize names grammatically identified as APIs, not incidental mentions."""
    start = max(text.rfind('\n', 0, m.start()), text.rfind('.', 0, m.start())) + 1
    end = text.find('\n', m.end())
    pre = text[start:m.start()]
    post = text[m.end():end if end != -1 else len(text)]
    ident = r'[`"]?[A-Za-z_]\w*[`"]?'
    separator = r'(?:,\s*(?:and\s+)?|\s+and\s+)'
    role = r'(?:APIs?|entities|entity\s+sets?|function\s+imports?|collections?)'
    if re.fullmatch(r'(?:The\s+)?(?:' + ident + separator + r')*', pre.lstrip(), re.I) and re.match(r'(?:' + separator + ident + r')*\s+' + role + r'\b', post, re.I):
        return True
    labels = list(re.finditer(r'\b' + role + r'\b', pre, re.I))
    if not labels:
        return False
    label = labels[-1]
    if re.search(r'\bfollowing\s*$', pre[:label.start()], re.I) and re.fullmatch(r'[:\s]*(?:' + ident + r'\s+)*', pre[label.end():]):
        return True
    return bool(
        re.fullmatch(r'[ :]*(?:' + ident + separator + r')*', pre[label.end():])
        and re.match(r'(?:' + separator + ident + r')*\s+(?:can|are|is|retrieve|return|support)\b', post, re.I)
    )


def _list_heading(text: str, m: re.Match[str], title: str, *, explicit_only: bool = False) -> bool:
    if NEGATIVE.search(title) or re.match(r'\s*(?:\.{2,}|(?:\.\s*){3,})\s*\d+', text[m.end():].split('\n', 1)[0]):
        return False
    if _typed_table(text, m):
        return not explicit_only
    for raw in reversed(text[:m.start()].split('\n')):
        line = raw.strip().strip('#* ')
        if not line:
            continue
        if NEGATIVE.search(line):
            return False
        found = list(LABEL.finditer(line))
        if found:
            label = found[-1]; suffix = line[label.end():]
            # A label counts only when the page itself introduces the list: it ends in a colon, or the
            # items follow it on the same line separated by commas. A bare group heading over bullet items
            # is how documentation sites print their navigation tree, so it does not label a list.
            introduced = re.fullmatch(r'\s*:\s*', suffix) or re.fullmatch(r'\s*:?\s*[A-Za-z_]\w*(?:\s*,\s*(?:and\s+)?[A-Za-z_]\w*)*\s*,?\s*', suffix) and suffix.strip()
            return not explicit_only and bool(introduced or (re.search(r'\blist of\b', line[:label.start()], re.I) and re.search(r'\bare[;:]?\s*$', suffix, re.I)))
        if re.search(r'\bthe following APIs\b', line, re.I):
            return True
        # Flattened navigation loses nesting, and an excerpt may omit the words that mark it as navigation.
        # A plain topic line therefore always ends the group: it cannot lend an earlier group's type.
        if re.fullmatch(r'[A-Za-z]+', line.lstrip('-• ')) and not re.search(r'[a-z][A-Z]', line):
            return False
        if not _list_fragment(line):
            return False
    return False


def _bare_allowed(tags: set[str], text: str, m: re.Match[str], name: str, policy: str, title: str) -> bool:
    if m.group().split('(', 1)[0] != name:
        return False
    tags.add('case_exact'); direct = _entity_relation(text, m)
    if direct:
        if re.search(r'\b' + NOUN + r'[ \t]+["“]$', text[max(0, m.start()-160):m.start()], re.I):
            tags.discard('json')
        if re.match(r'[ \t]*[·:][ \t]*(?:This|The)[ \t]+' + NOUN + r'\b', text[m.end():], re.I):
            tags.discard('list')
        tags.add('api_noun')
    if 'json' not in tags and (_prose_identification(text, m) or _typed_table(text, m) or _list_heading(text, m, title, explicit_only=True)):
        return True
    if tags & {'addr_token', 'api_noun', 'this_entity', 'own_url', 'own_title'} and not tags & {'json', 'list'}:
        return True
    return 'json' not in tags and policy == 'L1' and (_typed_table(text, m) or _list_heading(text, m, title))


def _legacy_method(text: str, start: int, end: int) -> str | None:
    pre = text[max(0, start-260):start]; last = None
    for m in ADDR.finditer(pre):
        if m.end() < len(pre)-1:
            last = m
    if last:
        pre = pre[last.end():]
    if pre.count('\n') > 3:
        pre = pre[[i for i, c in enumerate(pre) if c == '\n'][-4]+1:]
    m = BEFORE.search(pre)
    if m:
        return m[1].lower()
    command = CURL.search(text[max(0, start-400):start])
    if command:
        cmd = command.group() + text[end:end+200].split('\n\n')[0]; method = re.search(r'(?:-X|--request)\s*[\'"]?(GET|POST|PUT|PATCH|DELETE)', cmd, re.I)
        if method:
            return method[1].lower()
        if re.search(r'\s(?:-d|--data[\w-]*|-F|--form)\s', cmd):
            return 'post'
    post = text[end:end+160]; next_address = ADDR.search(post, 1)
    if next_address:
        post = post[:next_address.start()]
    method = AFTER.search(post)
    return (method[1] or method[2]).lower() if method else None


def _method(text: str, m: re.Match[str], title: str) -> str | None:
    before = text[max(0, m.start()-800):m.start()]; after = text[m.end():m.end()+240]
    yaml = re.match(r'\s*:\s*' + METHOD + r'\s*:', after, re.I)
    if yaml:
        return yaml[1].lower()
    prose = re.match(r'[^\s`]*[`\'" ]*\s+(?:in|using|with)\s+(?:(?:a|the)\s+)?' + METHOD + r'\s+request\b', after, re.I)
    if prose:
        return prose[1].lower()
    bracketed = re.match(r'(?:[^\s()]|\([^\s()]*\))*[ \t]*\([ \t]*' + METHOD + r'[ \t]*\)', after)
    if bracketed:
        return bracketed[1].lower()
    original = _legacy_method(text, m.start(), m.end())
    if original:
        return original
    methods = list(re.finditer(r'(?m)^\s*' + METHOD + r'\s*$', before))
    if methods:
        last = methods[-1]; between = before[last.end():]
        if title.strip() and title.strip().lower() in between.lower() and re.search(r'\b(?:Environment|Save|Share|Send)\b', between) and not ADDR.search(between):
            return last[1].lower()
    core = title.split('|', 1)[0].strip()
    if core and core in before and all(re.search(r'\b' + word + r'\b', text) for word in ('Save', 'Share', 'Send')):
        labels = []
        for label in re.finditer(r'(?m)^\s*' + METHOD + r'\s*$', text):
            if label.start() >= m.end() and re.match(r'\s*' + re.escape(core) + r'(?:\s|$)', text[label.end():]) and not re.search(r'https?://|/(?:api|personnel)/|\{\{[^}]+\}\}/', text[m.end():label.start()]):
                labels.append(label[1].lower())
        if labels and len(set(labels)) == 1:
            return labels[0]
    return None


def _restated_method(answer: Answer, text: str, m: re.Match[str], title: str) -> str | None:
    """The method shown at the nearest earlier showing of the same address, with no other address in between."""
    earlier = [o for o in answer.pattern.finditer(text) if o.end() <= m.start() and path_of(o.group()) == path_of(m.group())]
    if not earlier or any(a.end() < m.start() for a in ADDR.finditer(text, earlier[-1].end(), m.start())):
        return None
    return _method(text, earlier[-1], title)


def _url_pattern(answer: Answer) -> re.Pattern[str]:
    """A documentation slug joins path pieces with hyphens, so in a URL a hyphen beside the tail is a separator."""
    pattern = answer.pattern.pattern.replace(r'(?<![A-Za-z0-9_-])', r'(?<![A-Za-z0-9_])', 1).replace(r'(?!/?[\w{:(-])', r'(?!/?[\w{:(])', 1)
    return re.compile(pattern, answer.pattern.flags)


def _url_ok(key: dict, answer: Answer, url: str) -> bool:
    parsed = _split(url)
    if parsed is None or parsed.netloc.lower().removeprefix('www.') not in _documentation_hosts(key):
        return False
    path = re.sub(r'\.(html?|htm)$', '', parsed.path.rstrip('/'), flags=re.I)
    match = re.search(_url_pattern(answer).pattern + '$', path, re.I)
    if not match:
        return False
    if match.start() == 0 or path[match.start()-1] == '/':
        return True
    if any(path.lower() == re.sub(r'\.(html?|htm)$', '', urlsplit(reference).path.rstrip('/'), flags=re.I).lower() for reference in [key['source_url'], *key['docs']]):
        return True
    slug = '-'.join(part for part in path.rsplit('/', 1)[-1].lower().split('-') if not VERSION.fullmatch(part))
    expected = answer.op.method + '_' + '-'.join(part for part in answer.op.path if not VERSION.fullmatch(part))
    return slug == expected


def _query_ok(answer: Answer, text: str, match: re.Match[str], field: str) -> bool:
    if not answer.required_query:
        return True
    if field == 'url':
        raw = urlsplit(text).query
    else:
        suffix = re.match(r'[^\s<>`"]*', text[match.end():]).group().rstrip("'.,;")
        if '?' not in suffix:
            return False
        raw = suffix.split('?', 1)[1]
    query = parse_qs(raw.replace('&amp;', '&'), keep_blank_values=True)
    return all(key in query and query[key][-1] == value for key, value in answer.required_query)


def _soap_owner(text: str, match: re.Match[str]) -> re.Match[str] | None:
    pattern = re.compile(r'/(?:ccx/service/[^/\s]+|productionapi)/(?P<owner>[^/\s;?<>`"]+)(?=/|[\s;?]|$)', re.I)
    start = 0
    for boundary in re.finditer(r'\n[ \t\r]*\n', text[:match.start()]):
        start = boundary.end()
    owner = None
    for candidate in pattern.finditer(text, start, match.start()):
        owner = candidate
    if owner and owner.start() >= text.rfind('\n', 0, match.start()) + 1:
        return owner
    end = text.find('\n', match.end())
    return pattern.search(text, match.end(), len(text) if end < 0 else end) or owner


def _soap_context(text: str, m: re.Match[str]) -> bool:
    after = text[m.end():m.end()+40]
    if re.match(r'\s*[_ ]?(request|response)\b', after, re.I):
        return True
    following = MARK.search(after)
    if following and following.start() <= 25:
        return bool(following.group('ws'))
    preceding = list(MARK.finditer(text[:m.start()]))
    return bool(preceding[-1].group('ws')) if preceding else bool(following and following.group('ws'))


def _soap_own_title(title: str, name: str) -> re.Match[str] | None:
    return re.match(r'(?:[\d.]+\s+)?(' + re.escape(name).replace('_', '[ _]') + r'(?:_Request)?)(?:\s+[|–—-].*|\s*[|–—].*)?\Z', title.strip(), re.I)


def _soap_named(text: str, m: re.Match[str], title: str, name: str) -> bool:
    return (m.group().lower() == name.lower() + '_request' or _soap_context(text, m) or _soap_owner(text, m) is not None
            or _soap_own_title(title, name) is not None or _soap_described(text, m))


def _soap_described(text: str, m: re.Match[str]) -> bool:
    """The operation's own description: a table row whose next cell describes it, or a sentence it opens."""
    start = text.rfind('\n', 0, m.start()) + 1; end = text.find('\n', m.end())
    before = text[start:m.start()]; after = text[m.end():end if end != -1 else len(text)]
    row = re.search(r'(?:^[ \t]*(?:[*-][ \t]+)?\|?|\|)[ \t*`]*$', before) and re.match(r'[ \t*`]*(?:END OF LIFE[ \t]*)?\|[ \t]*[A-Za-z]+(?:[ \t]+[^\s|]+){2,}', after)
    prose = re.search(r'(?:^|[.!?:][ \t]+)[ \t*`]*$', before) and re.match(r'[ \t*`]+(?:' + DESCRIBES + r')\b', after)
    return bool(row or prose)


def _soap_spaced(text: str, name: str, longer: list[str]):
    text = re.sub(r'(?<!\w)(_{1,2})([A-Za-z][A-Za-z0-9 -]* [A-Za-z0-9 -]+?)\1(?!\w)', lambda m: m[2], text)
    pattern = re.escape(name).replace('_', '[ ]+')
    for m in re.finditer(rf'(?<![\w-]){pattern}(?:[ _]request)?(?![\w-])', text):
        lo = max(0, m.start()-80); nearby = text[lo:m.end()+80]
        if any(any(lo+x.start() <= m.start() and lo+x.end() >= m.end() for x in re.finditer(re.escape(other).replace('_', '[ ]+'), nearby)) for other in longer):
            continue
        if _soap_context(text, m):
            yield text, m


def _route(op: Op) -> str:
    return op.method + ' /' + '/'.join(s for s in op.path if s != 'api' and not VERSION.fullmatch(s))


def _action_classes(phrase: str, *, first_word: bool) -> set[str]:
    words = re.findall(r"[A-Za-z]+(?:[ \t]+up)?", phrase)
    words = words[:1] if first_word else words
    return {method for method, verbs in ACTIONS.items() for w in words if re.fullmatch(verbs, w, re.I)} - {'patch'}


def _clearly_for(method: str, others: tuple[str, ...], text: str, m: re.Match[str] | None, title: str) -> bool:
    """With no method word next to it, an address whose path has another documented method counts only when the page
    says the action plainly for it: its title, its section heading, its own entry, or its description name the accepted
    action (or method) and none of the other documented ones."""
    def shown(phrase: str) -> set[str]:
        return {w.lower() for w in re.findall(r'(?<![\w/])' + METHOD + r'(?![\w/])', phrase)} - {'head', 'options'}
    found = _action_classes(re.sub(r'^[\s\d.#*|]+', '', title), first_word=True)
    if m is not None:
        start = text.rfind('\n', 0, m.start()) + 1; end = text.find('\n', m.end()); end = len(text) if end < 0 else end
        entry = re.split(r'[·|•]', text[start:m.start()])[-1][-80:]
        found |= shown(entry) or _action_classes(re.sub(r'^[\s\d.#*`-]+', '', entry), first_word=True)
        description = text[m.end():end].strip(' \t`*|:-') or next((line for line in text[end:].split('\n') if line.strip()), '')
        found |= _action_classes(re.split(r'(?<=[.!?])\s|[·|•]', description.strip(), maxsplit=1)[0][:200], first_word=False)
        for line in reversed(text[:start].split('\n')[-12:]):
            heading = re.fullmatch(r'[ \t]*(?:#{1,6}[ \t]+|\*\*)([^\n]{3,160}?)(?:\*\*)?[ \t]*', line)
            if heading:
                classes = shown(heading[1]) or _action_classes(re.sub(r'^[\s\d.]+', '', heading[1]), first_word=True)
                if classes:
                    found |= classes
                    break
    mine = 'put' if method == 'patch' else method
    return mine in found and not (found & {'put' if o == 'patch' else o for o in others})


NEGATED_BEFORE = re.compile(r"\b(?:do[ \t]+not|don[’']t|never|must[ \t]+not|should[ \t]+not|cannot|can[’']t)[ \t]+(?:call|use|invoke|send)\b|\bno[ \t]+(?:results?|matches|documents?)[ \t]+(?:were[ \t]+)?(?:found[ \t]+)?(?:for|matching)\b|\bdid[ \t]+not[ \t]+match[ \t]+any\b", re.I)
NEGATED_AFTER = re.compile(r"\b(?:is|are|was|were|has[ \t]+been|have[ \t]+been)[ \t]+(?:not[ \t]+part[ \t]+of|removed|retired|discontinued)\b|\bno[ \t]+longer[ \t]+(?:exists?|available|supported)\b|\bdoes[ \t]+not[ \t]+exist\b", re.I)


def _negated(text: str, m: re.Match[str]) -> bool:
    """The sentence that shows the call says it is not to be called, does not exist, or found no results for it."""
    start = max(text.rfind('\n', 0, m.start()), *(text.rfind(p, 0, m.start()) for p in ('. ', '! ', '? '))) + 1
    ends = [i for i in (text.find(p, m.end()) for p in ('\n', '. ', '! ', '? ')) if i != -1]
    end = min(ends) if ends else len(text)
    return bool(NEGATED_BEFORE.search(text[start:m.start()]) or NEGATED_AFTER.search(text[m.end():end]))


def _witness(answer: int, hit: EvidenceHit, field: str, text: str, match: re.Match[str], normalizations: list[dict] | None = None) -> dict:
    witness = {'answer': answer, 'hit': hit.marker, 'source': hit.source, 'url': hit.url, 'field': field, 'quote': text[max(0, match.start()-90):match.end()+80], 'quote_view': 'normalized program evidence; raw delivered text is retained separately'}
    if normalizations:
        witness['normalizations'] = normalizations
        witness['raw_text_sha256'] = hashlib.sha256(hit.text.encode()).hexdigest()
        witness['raw_title'] = hit.title
    return witness


INDEX_ADDRESS = re.compile(r'(?<![\w/])/[\w{}.:$-]+(?:/[\w{}.:$()\',=-]+)+')
INDEX_SOAP = re.compile(r'\b[A-Z][a-z]+(?:_[A-Z][A-Za-z]+){1,6}\b')
CONNECTOR = re.compile(r'^https?://(?:[^/?#]*\.)?(?:wso2\.com|ballerina\.io|pinkfish\.ai|cdata\.com|pipedream\.com|rapidapi\.com)(?:[:/?#]|$)'
                       r'|^https?://[^/?#]+/(?:[^?#]*/)?connectors?(?:[/?#]|$)', re.I)
CHANGELOG = re.compile(r"\bwhat[’']?s[ \t]+new\b|\brelease[ \t]+notes?\b|(?-i:\bUpdates\b)|\bchange[ \t-]?log\b|\bnew[ \t]+and[ \t]+changed\b|\brevision[ \t]+history\b|\b20\d\d[ \t]*R[12]\b", re.I)


def _listing(hit: EvidenceHit) -> str | None:
    """A page that lists names: a third-party connector catalog, or a route/API index (25+ addresses or operations)."""
    if CONNECTOR.search(hit.url):
        return 'connector'
    whole = hit.url + '\n' + hit.title + '\n' + hit.text
    if len({a.lower() for a in INDEX_ADDRESS.findall(whole)}) >= 25 or len(set(INDEX_SOAP.findall(whole))) >= 25:
        return 'index'
    return None


def _changelog(text: str, m: re.Match[str], title: str) -> bool:
    """The occurrence sits on a change-log or release-notes page, or under such a section heading."""
    if CHANGELOG.search(title):
        return True
    for line in reversed(text[:m.start()].split('\n')):
        heading = re.fullmatch(r'[ \t]*(?:#{1,6}[ \t]+(.+?)|\*\*([^*\n]+)\*\*)[ \t]*', line)
        if heading:
            return bool(CHANGELOG.search(heading[1] or heading[2]))
    return False


def _address_described(text: str, m: re.Match[str]) -> bool:
    """The entry that shows an address also says what the call does: three or more words beside it."""
    start = text.rfind('\n', 0, m.start()) + 1; end = text.find('\n', m.end()); end = len(text) if end < 0 else end
    head = m.start() - len(re.search(r'\S*\Z', text[:m.start()])[0]); tail = m.end() + len(re.match(r'\S*', text[m.end():])[0])
    def words(s):
        return [w for w in re.findall(r'(?<![\w/{.-])[A-Za-z]{2,}(?![\w/}-])(?!\.\w)', s) if w.upper() not in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS')]
    found = words(text[start:head] + ' ' + text[tail:end])
    if len(found) < 3:
        following = next((line for line in text[end:].split('\n') if line.strip()), '')
        if not re.match(r'\s*(?:(?i:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)[ \t]*(?:$|\S*/)|/|https?://)', following):
            found += words(following)
    return len(found) >= 3


def _entry_described(answer: Answer, kind: str, text: str, m: re.Match[str], method: str | None, tags: set[str]) -> bool:
    """On a page that only lists names, an entry counts only when it shows the request and says what it does."""
    if answer.op.soap:
        request = kind != 'connector' or m.group().lower() == answer.name.lower() + '_request' or _soap_owner(text, m) is not None
        described = _soap_described(text, m) or re.match(r'\S*[ \t]*\|[ \t]*[A-Za-z]+(?:[ \t]+[^\s|]+){2,}', text[m.end():])
        return request and bool(described)
    token = re.search(r'\S*\Z', text[:m.start()])[0].lstrip('`*"\'([<')
    if not re.match(r'(?:https?://|\{\{?[\w.-]*\}?\}|/)', token):
        return kind == 'changelog' and 'this_entity' in tags
    return (kind == 'changelog' or method == answer.op.method) and _address_described(text, m)


def _judge(key: dict, answers: tuple[Answer, ...], hits: list[EvidenceHit], policy: str) -> list[dict]:
    hosts = _documentation_hosts(key)
    roots = {answer.tail[0] for answer in answers if len(answer.tail) == 1 and _odata(answer.op)}
    for n, answer in enumerate(answers):
        for hit in hits:
            listing = _listing(hit)
            url = hit.url.replace('\\_', '_') if hit.source == 'search' else ''
            title = _decode(hit.title).replace('\\_', '_')
            text, normalizations = _normalize_text(answer, hit.text, hosts)
            text = _split_description(text.replace('\\_', '_'), title)
            if answer.words and not all(re.search(r'(?<![A-Za-z0-9_])' + re.escape(w) + r'(?![A-Za-z0-9_])', '\n'.join((url, title, text))) for w in answer.words):
                continue
            composed = () if answer.op.soap else _composed_requests(text, title)
            for raw_field, raw_value, raw_changes in chain((('url', url, []), ('title', title, []), ('snippet', text, normalizations)), composed):
                candidates = ((raw_field, raw_value, match, raw_changes) for match in (_url_pattern(answer) if raw_field == 'url' else answer.pattern).finditer(raw_value))
                if answer.op.soap and raw_field != 'url':
                    candidates = chain(candidates, (('spaced SOAP operation', normalized, match, raw_changes + [{'kind': 'spaced SOAP operation', 'raw': raw_value, 'normalized': normalized}]) for normalized, match in _soap_spaced(raw_value, answer.name, key['longer_operations'][answer.name])))
                if answer.op.soap and raw_field == 'title':
                    own = _soap_own_title(raw_value, answer.name)
                    if own and ' ' in own[1] and any(mark.group('ws') for mark in MARK.finditer(title + '\n' + text)):
                        candidates = chain(candidates, (('own SOAP title', raw_value.strip(), own, raw_changes + [{'kind': 'spaced SOAP operation as own page title', 'raw': raw_value}]),))
                for field, value, match, changes in candidates:
                    if answer.op.soap and field in ('title', 'snippet') and not _soap_named(value, match, title, answer.name):
                        continue
                    if answer.op.soap and field in ('title', 'snippet') and re.match(r'\.[A-Za-z][A-Za-z0-9]{0,4}\b', value[match.end():]):
                        continue
                    constraint_match = match
                    if answer.op.soap:
                        owner = _soap_owner(value, match)
                        if owner:
                            service = next(part for part in reversed(answer.op.path) if not VERSION.fullmatch(part))
                            if owner['owner'].lower() != service:
                                continue
                            constraint_match = owner
                    others = () if answer.op.soap else SIBLING_METHODS.get(_route(answer.op), ())
                    if field == 'url':
                        if not _url_ok(key, answer, url) or answer.method_shown:
                            continue
                        if others and not (_odata(answer.op) and len(answer.tail) == 1) and not _clearly_for(answer.op.method, others, '', None, title + ' ' + url.rstrip('/').rsplit('/', 1)[-1].replace('-', ' ').replace('_', ' ')):
                            continue
                    else:
                        if not answer.op.soap:
                            if not _request_identity(answer, value, match, hosts, roots):
                                continue
                            navigation = answer.words and value[match.end():match.end()+1] == '/'
                            if not _entity_case(key['accepted'][n]['address'], answer, value, match, navigation):
                                continue
                            if len(answer.tail) == 1 and value[match.start()-1:match.start()] != '/' and not navigation:
                                tags = _tags(answer.name, url, title, field, value, match)
                                if not _bare_allowed(tags, value, match, answer.name, policy, title):
                                    continue
                        method = None if answer.op.soap and constraint_match is match else _method(value, constraint_match, title)
                        if (method and method != answer.op.method) or (answer.method_shown and method != answer.op.method):
                            continue
                        if method is None and not answer.op.soap:
                            restated = _restated_method(answer, value, match, title)
                            if restated and restated != answer.op.method:
                                continue
                            bare_entity = _odata(answer.op) and len(answer.tail) == 1 and (value[match.start()-1:match.start()] != '/' or answer.name[:1].islower())
                            if others and restated is None and not bare_entity and not _clearly_for(answer.op.method, others, value, match, title):
                                continue
                        if _negated(value, match):
                            continue
                        if field not in ('title', 'own SOAP title'):
                            kind = listing or ('changelog' if _changelog(value, match, title) else None)
                            if kind and not _entry_described(answer, kind, value, match, method, _tags(answer.name, url, title, field, value, match)):
                                continue
                    if _query_ok(answer, value, constraint_match, field):
                        return [_witness(n, hit, field, value, match, changes)]
    return []


def _input(key: dict, answers: tuple[Answer, ...], hits: list[EvidenceHit]) -> str:
    ref = key['reference']; soap = ref['soap']
    data = {'reference_address': ref['address'], 'method': ref['method'], 'SOAP_operation': soap.removesuffix('_Request') if soap else None, 'SOAP_request_element': soap, 'reference_doc_URL': key['source_url'], 'reference_doc_URLs': key['docs']}
    out = [json.dumps(data, ensure_ascii=False), '\nAccepted answers (equal alternatives):']
    for i, raw in enumerate(key['accepted'], 1):
        a = {name: value for name, value in raw.items() if name != 'executable_address'}
        if answers[i-1].required_query:
            a['required_query'] = dict(answers[i-1].required_query)
        if a.get('soap'):
            a['SOAP_operation_stem'] = a['soap'].removesuffix('_Request')
        if a.get('method_shown'):
            a['requirement'] = 'the method word must appear next to this address'
        if a.get('with'):
            a['required_words_in_same_result'] = a['with']
        others = () if answers[i-1].op.soap else SIBLING_METHODS.get(_route(answers[i-1].op), ())
        if others:
            a['other_documented_methods_on_this_path'] = [o.upper() for o in others]
        out.append(str(i) + '. ' + json.dumps(a, ensure_ascii=False))
    out.append('\nSearch results (verbatim):')
    for hit in hits:
        label = 'URL' if hit.source == 'search' else 'Fetch URL (caller argument, provenance only)'
        out.append(f'\n[{hit.marker}]\n{label}: {hit.url}\nTitle: {hit.title}\nText: {hit.text}\n[/{hit.marker}]')
    return '\n'.join(out)


def retrieval_input(task: CodingTask, *, search_hops: list[Any], fetch_hops: list[Any]) -> str:
    """Render the scored alternatives and delivered evidence for an independent audit."""
    key, answers = _reference(task.retrieval_key)
    return _input(key, answers, delivered_hits(search_hops, fetch_hops))


def score_retrieval(task: CodingTask, *, search_hops: list[Any], fetch_hops: list[Any], policy: str) -> dict:
    """Score both named policies; the caller must explicitly choose its primary one."""
    identity = retrieval_identity(policy=policy, key=task.retrieval_key)
    if task.retrieval_key['answers'] is None:
        return {'status': 'not_evaluated', 'passed': None, 'reason': 'answers not in key', **identity}
    key, answers = _reference(task.retrieval_key)
    hits = delivered_hits(search_hops, fetch_hops)
    evidence = {rule: _judge(key, answers, hits, rule) for rule in POLICY_VERSIONS}
    passes = {rule: bool(value) for rule, value in evidence.items()}
    return {'passed': passes[policy], **identity, 'passes': passes, 'evidence': evidence, 'input_sha256': hashlib.sha256(_input(key, answers, hits).encode()).hexdigest()}
