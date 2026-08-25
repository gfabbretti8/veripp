"""SARIF output, so findings land on the pull request rather than in a log.

GitHub code scanning ingests SARIF and renders each result as an annotation on
the diff. That is the difference between a finding somebody has to go looking
for in a job log and one that appears next to the line that causes it.

Two things here are deliberate and worth keeping:

Baselined findings are emitted as *suppressed* results rather than dropped.
Code scanning then shows them as suppressed instead of pretending they do not
exist, which is the honest rendering of "somebody accepted this" and keeps the
count stable when an entry is later removed.

Every result carries the bound it was obtained under. A SARIF consumer strips
away veripp's own reporting, so anything the reader needs in order to judge a
result has to travel inside the message.
"""

from __future__ import annotations

import json
from pathlib import Path

#: The oasis-tcs raw URL that most tools quote is a 404 today; this one
#: resolves, and is what the output was validated against.
SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"
SARIF_VERSION = "2.1.0"
HOMEPAGE = "https://github.com/gfabbretti8/veripp"

#: veripp reports a handful of property classes; each becomes a SARIF rule so
#: code scanning can group, filter and explain them.
RULES = {
    "overflow": (
        "Arithmetic overflow",
        "An arithmetic operation can exceed the range of its type. In C and "
        "C++ signed overflow is undefined behaviour.",
        ["CWE-190", "CWE-191"],
    ),
    "bounds": (
        "Out-of-bounds access",
        "An index can fall outside the object being indexed.",
        ["CWE-125", "CWE-787"],
    ),
    "pointer": (
        "Invalid pointer dereference",
        "A pointer that can be null or otherwise invalid is dereferenced.",
        ["CWE-476"],
    ),
    "division": (
        "Division by zero",
        "A divisor can be zero.",
        ["CWE-369"],
    ),
    "other": (
        "Property violation",
        "A checked property does not hold for some input.",
        [],
    ),
}


def rule_for(description: str) -> str:
    text = (description or "").lower()
    if "overflow" in text:
        return "overflow"
    if "bound" in text or "array" in text or "index" in text:
        return "bounds"
    if "dereference" in text or "null" in text or "pointer" in text:
        return "pointer"
    if "division" in text or "divide" in text:
        return "division"
    return "other"


def _relative(path: str, root: Path) -> str:
    """SARIF URIs must be relative to the checkout, or code scanning cannot
    match a result to a file in the diff."""
    if not path:
        return ""
    candidate = Path(path)
    try:
        return str(candidate.resolve().relative_to(root.resolve()))
    except (ValueError, OSError):
        return candidate.name


def build(findings: list[dict], *, root: Path, version: str,
          bounds: str = "", suppressed: set | None = None) -> dict:
    """A SARIF log for `findings`.

    Each finding is a dict with file, line, column, function, property and
    cwes -- the shape `veripp scan --json` already emits.
    """
    suppressed = suppressed or set()
    used: dict[str, dict] = {}
    results = []

    for finding in findings:
        description = finding.get("property") or "property violation"
        rule_id = rule_for(description)
        title, explanation, cwes = RULES[rule_id]
        used.setdefault(rule_id, {
            "id": rule_id,
            "name": title.replace(" ", ""),
            "shortDescription": {"text": title},
            "fullDescription": {"text": explanation},
            "help": {
                "text": f"{explanation} Reproduce with: veripp verify <file> "
                        "--function <name>",
            },
            "properties": {
                "tags": ["security", "correctness", *cwes],
                "precision": "high",
            },
            "defaultConfiguration": {"level": "error"},
        })

        function = finding.get("function", "")
        message = description
        if function:
            message = f"{description} in `{function}`"
        if bounds:
            # The consumer shows only this text, so the bound has to be in it:
            # a reader must not take a bounded result for a total one.
            message += f" ({bounds})"
        message += (
            ". A counterexample holds in the generated harness; confirm a "
            "caller can reach it."
        )

        uri = _relative(finding.get("file", ""), root)
        result = {
            "ruleId": rule_id,
            "level": "error",
            "message": {"text": message},
            "locations": [{
                "physicalLocation": {
                    "artifactLocation": {"uri": uri or "unknown"},
                    "region": {
                        "startLine": max(1, int(finding.get("line") or 1)),
                        **({"startColumn": int(finding["column"])}
                           if finding.get("column") else {}),
                    },
                }
            }],
            # Keyed the same way the baseline is, so code scanning tracks a
            # finding across commits even when the code moves.
            "partialFingerprints": {
                "veripp/v1": f"{uri}:{function}:{description}",
            },
        }
        if (uri, function, description) in suppressed:
            result["suppressions"] = [{
                "kind": "external",
                "justification": "Accepted in .veripp-baseline",
            }]
        results.append(result)

    return {
        "$schema": SCHEMA,
        "version": SARIF_VERSION,
        "runs": [{
            "tool": {"driver": {
                "name": "veripp",
                "version": version,
                "informationUri": HOMEPAGE,
                "rules": list(used.values()),
            }},
            "results": results,
        }],
    }


def write(path: Path, log: dict) -> None:
    path.write_text(json.dumps(log, indent=2) + "\n", encoding="utf-8")
