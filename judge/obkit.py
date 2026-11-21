"""obkit entry points: grade one answer, and the LLM verifier's second reading of it."""

from __future__ import annotations

from pathlib import Path

import yaml

from .checks import grade as grade_call

HERE = Path(__file__).resolve().parent
PROMPT = (HERE / "verifier_prompt.txt").read_text()
CONFIG = yaml.safe_load((HERE / "config.yaml").read_text())
VERDICTS = {"PASS": True, "FAIL": False}


def grade(question: dict, answer: str) -> bool:
    return grade_call(question["retrieval_key"]["accepted"], answer).passed


def verify(question: dict, answer: str, passed: bool, *, dry: bool) -> dict:
    """agree, disagree or unsure with the deterministic verdict, and what the call cost. It never changes a score."""
    if dry:
        return {"verdict": "agree", "cost_usd": 0.0}
    from harness.run import load_environment, make_llm_client

    load_environment()
    right = "\n".join(
        f"{r['method']} {r['address']}" + (f" (SOAP {r['soap']})" if r.get("soap") else "")
        for r in question["retrieval_key"]["accepted"]
    )
    reply = make_llm_client(timeout=120, max_retries=1).responses.create(
        model=CONFIG["verifier"]["model"],
        reasoning={"effort": "low"},
        store=False,
        instructions=PROMPT,
        input=f"Task:\n{question['ticket']}\n\nRight answers:\n{right}\n\nAgent's call:\n{answer}\n\n"
        "End with one line: PASS, FAIL or UNSURE.",
    )
    price = CONFIG["verifier"]["usd_per_mtok"]
    usage = reply.usage
    cost = (usage.input_tokens * price["input"] + usage.output_tokens * price["output"]) / 1e6
    word = ((reply.output_text or "").strip().splitlines() or [""])[-1].strip().upper()
    verdict = "unsure" if word not in VERDICTS else "agree" if VERDICTS[word] == passed else "disagree"
    return {"verdict": verdict, "cost_usd": cost}
