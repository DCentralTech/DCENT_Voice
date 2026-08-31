#!/usr/bin/env python3
# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Evaluate local writing styles and cleanup levels. Never mixed into ASR WER."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dcent_voice.attach.registry import write_text_atomic  # noqa: E402
from dcent_voice.dictation import compose_dictation  # noqa: E402
from dcent_voice.dictation.style import apply_style  # noqa: E402


def _evaluate_item(item: dict) -> tuple[bool, str, list[str]]:
    raw = item["input"]
    kind = (item.get("kind") or "style").strip().lower()
    reasons: list[str] = []
    if kind == "cleanup":
        level = item.get("cleanup_level") or "medium"
        got = compose_dictation(raw, cleanup_level=level)
    elif kind == "compose":
        style = item.get("style") or "plain"
        from dcent_voice.config import SnippetEntry

        snippets = tuple(
            SnippetEntry(
                spoken=str(row.get("spoken") or ""),
                expansion=str(row.get("expansion") or ""),
            )
            for row in (item.get("snippets") or [])
        )
        got = compose_dictation(raw, style=style, snippets=snippets)
    else:
        style = item.get("style") or "plain"
        got = apply_style(raw, style)
    ok = True
    if "exact" in item and got != item["exact"]:
        ok = False
        reasons.append(f"exact mismatch: {got!r}")
    for needle in item.get("must_contain") or []:
        if needle not in got:
            ok = False
            reasons.append(f"missing {needle!r}")
    for needle in item.get("must_not_contain") or []:
        if needle in got:
            ok = False
            reasons.append(f"unexpected {needle!r}")
    differ = item.get("must_differ_from_levels") or {}
    if differ:
        for other_level, other_exact in differ.items():
            other = compose_dictation(raw, cleanup_level=other_level)
            if other == got:
                ok = False
                reasons.append(f"same as cleanup_level={other_level!r}")
            if other_exact is not None and other != other_exact:
                ok = False
                reasons.append(f"reference cleanup_level={other_level!r} mismatch: {other!r}")
    return ok, got, reasons


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--corpus",
        type=Path,
        default=ROOT / "eval" / "writing_notes.json",
    )
    parser.add_argument("--output-json", type=Path, default=None)
    args = parser.parse_args(argv)
    corpus = json.loads(args.corpus.read_text(encoding="utf-8"))
    items = corpus.get("items") or []
    results = []
    failed = 0
    for item in items:
        ok, got, reasons = _evaluate_item(item)
        results.append(
            {
                "id": item.get("id"),
                "ok": ok,
                "kind": item.get("kind") or "style",
                "cleanup_level": item.get("cleanup_level"),
                "style": item.get("style"),
                "output": got,
                "reasons": reasons,
            }
        )
        if not ok:
            failed += 1
            detail = " | ".join(reasons)
            print(f"FAIL {item.get('id')}: {detail}")
        else:
            print(f"PASS {item.get('id')}")
    summary = {
        "schema": "dcent-writing-eval-result-v1",
        "scope": "offline_text_writing_no_asr_no_microphone_no_injection",
        "corpus": str(args.corpus),
        "passed": len(results) - failed,
        "failed": failed,
        "total": len(results),
        "results": results,
    }
    if args.output_json:
        write_text_atomic(
            args.output_json,
            json.dumps(summary, indent=2) + "\n",
            require_private=False,
        )
    print(f"writing_eval passed={summary['passed']} failed={summary['failed']}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
