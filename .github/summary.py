#!/usr/bin/env python3
"""Render veripp's JSON report as a GitHub job summary.

The Actions log is a wall of text nobody reads unless something breaks. The
summary is the first thing on the run page, so it should answer "what
happened" without opening anything -- and, when veripp found something, say
plainly what is and is not being claimed.

Best effort by contract: the caller discards our exit status, because a
formatting problem here must never turn a real verification result into a
failed step.
"""

from __future__ import annotations

import json
import os
import sys

VERDICT = {
    "verified": ("✅", "Verified", "within the stated bounds and assumptions"),
    "counterexample": ("❌", "Counterexample", "an input reaches the fault"),
    "usage-error": ("⚠️", "Usage error", "veripp was invoked incorrectly"),
    "inconclusive": ("⏱️", "Inconclusive", "a bound, a timeout, or a vacuous proof — **not** a pass"),
}


def main() -> int:
    status = os.environ.get("VERIPP_STATUS", "")
    outcome = {"0": "verified", "1": "counterexample", "2": "usage-error"}.get(
        status, "inconclusive"
    )
    icon, title, gloss = VERDICT[outcome]

    out = [f"## {icon} veripp: {title}", "", gloss, ""]

    try:
        with open(os.environ.get("VERIPP_REPORT", "")) as handle:
            report = json.load(handle)
    except Exception:
        report = None

    # Valid JSON that is not an object -- a bare list or string -- would sail
    # past the load and then fail on the first .get(). The verdict above is
    # already useful on its own, so stop here rather than risk producing
    # nothing at all.
    if not isinstance(report, dict):
        print("\n".join(out))
        return 0

    if "candidates" in report:  # a scan
        # Two shapes reach here. A single-file scan reports lists of function
        # names (not_harnessable as a dict keyed by reason); a directory scan
        # has already aggregated them into counts. Counting a list and reading
        # an int are both correct answers to "how many" -- treating an int as
        # uncountable silently reported zero for every proof in a tree scan.
        def size(key: str) -> int:
            value = report.get(key)
            if isinstance(value, bool) or value is None:
                return 0
            if isinstance(value, int):
                return value
            if isinstance(value, (list, dict)):
                return len(value)
            return 0

        where = report.get("root") or report.get("source") or "file"
        scope = f" across {report['files']} files" if report.get("files") else ""
        out += [
            f"**{where}**{scope} — "
            f"{report.get('candidates', '?')} functions",
            "",
            "| Outcome | Count |",
            "|---|---:|",
            f"| ✅ Proved | {size('proved')} |",
            f"| ❌ Counterexamples | {size('counterexamples')} |",
            f"| ⏱️ Inconclusive | {size('inconclusive')} |",
            f"| 🔧 Harness artifacts | {size('artifacts')} |",
            f"| — Not harnessable | {size('not_harnessable')} |",
            "",
        ]
        found = report.get("counterexamples") or []
        if found:
            out += ["<details><summary>Counterexamples — each needs triage</summary>", ""]
            for item in found[:20]:
                if isinstance(item, dict):
                    name = item.get("function", "?")
                    where_file = item.get("file")
                    why = item.get("reason") or item.get("property") or ""
                    label = f"`{where_file}` → `{name}`" if where_file else f"`{name}`"
                else:
                    label, why = f"`{item}`", ""
                out += [f"- {label}" + (f" — {why}" if why else "")]
            if len(found) > 20:
                out += [f"- …and {len(found) - 20} more"]
            out += ["", "</details>", ""]
    else:  # a single verify
        if report.get("function"):
            out += [f"**Function:** `{report['function']}`", ""]
        violated = report.get("violated_property")
        if isinstance(violated, dict):
            where = violated.get("loc") or {}
            location = ":".join(
                str(where[k]) for k in ("file", "line", "column") if where.get(k)
            )
            out += [f"**Violated property:** {violated.get('description', 'see log')}", ""]
            if location:
                out += [f"`{location}`" + (f" in `{where['function']}`" if where.get("function") else ""), ""]
            if violated.get("expression"):
                out += ["```", str(violated["expression"]), "```", ""]
            if violated.get("cwes"):
                out += [f"**CWE:** {', '.join(violated['cwes'])}", ""]
        elif violated:
            out += [f"**Violated property:** {violated}", ""]
        if report.get("vacuous"):
            out += ["> **Vacuous.** The assumptions made the call unreachable, so", 
                    "> every property held trivially. This is not a proof.", ""]
        if report.get("bounded") and outcome == "verified":
            out += ["> This is a **bounded** proof: it holds for executions within",
                    "> the unwind bound, not for all executions.", ""]
        for label, key in (("Assumptions", "assumptions"),
                           ("Stubbed calls", "stubbed_calls")):
            items = report.get(key) or []
            if items:
                out += [f"**{label}:**", ""]
                out += [f"- {item}" for item in items[:10]]
                if len(items) > 10:
                    out += [f"- …and {len(items) - 10} more"]
                out += [""]

    if report.get("unsound_probes"):
        out += ["> ⚠️ **The checker itself failed a soundness probe.** Results",
                "> covering that pattern are not trustworthy.", ""]

    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
