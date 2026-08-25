"""Accepted findings, so CI can fail on new ones only.

A verifier pointed at an existing codebase reports everything at once. cJSON
gives 33 counterexamples on the first run: fail the build on those and the
check is removed the next day, so the usual advice is to make it non-blocking
-- which turns it into a check nobody reads.

A baseline is the way out. Record what is already there, then fail only on
what appears after. The file is plain JSON and meant to be read in a pull
request: it is a record of accepted risk, and a reviewer should be able to see
what was accepted and why without running anything.

Findings are keyed on (file, function, property) and deliberately not on line
numbers, which change whenever anything above them moves. The signature is
stored for a reviewer but not keyed on, so adding a `const` does not silently
resurrect every finding in a file.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

BASELINE_VERSION = 1
DEFAULT_NAME = ".veripp-baseline"


class BaselineError(Exception):
    """The baseline file could not be used."""


@dataclass(frozen=True)
class Key:
    """What makes two findings the same finding."""

    file: str
    function: str
    property: str

    def as_dict(self) -> dict:
        return {"file": self.file, "function": self.function, "property": self.property}


@dataclass
class Entry:
    key: Key
    signature: str = ""
    accepted: str = ""
    reason: str = ""

    def as_dict(self) -> dict:
        out = self.key.as_dict()
        out["signature"] = self.signature
        out["accepted"] = self.accepted
        if self.reason:
            out["reason"] = self.reason
        return out


@dataclass
class Baseline:
    entries: dict[Key, Entry] = field(default_factory=dict)
    path: Path | None = None

    # -- reading -----------------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> Baseline:
        try:
            raw = json.loads(path.read_text())
        except FileNotFoundError as exc:
            raise BaselineError(f"{path} not found") from exc
        except json.JSONDecodeError as exc:
            raise BaselineError(f"{path} is not valid JSON: {exc}") from exc

        version = raw.get("version")
        if version != BASELINE_VERSION:
            # Refuse rather than guess: a baseline read wrongly suppresses real
            # findings, which is the one failure mode that must never be quiet.
            raise BaselineError(
                f"{path} is version {version!r}, this veripp understands "
                f"{BASELINE_VERSION}. Regenerate it with `veripp accept`."
            )

        entries: dict[Key, Entry] = {}
        for item in raw.get("findings", []):
            try:
                key = Key(item["file"], item["function"], item["property"])
            except (KeyError, TypeError) as exc:
                raise BaselineError(f"{path}: malformed entry {item!r}") from exc
            entries[key] = Entry(
                key=key,
                signature=item.get("signature", ""),
                accepted=item.get("accepted", ""),
                reason=item.get("reason", ""),
            )
        return cls(entries=entries, path=path)

    @classmethod
    def load_if_present(cls, path: Path | None) -> Baseline | None:
        if path is None:
            return None
        return cls.load(path)

    # -- writing -----------------------------------------------------------

    def save(self, path: Path, note: str = "") -> None:
        payload = {
            "version": BASELINE_VERSION,
            "generated": date.today().isoformat(),
            "note": note or (
                "Findings accepted as known. veripp fails CI only on findings "
                "absent from this file. Review it like any other change: each "
                "entry is a risk someone decided to carry."
            ),
            # Sorted so the file is diffable and two people generating it get
            # the same bytes.
            "findings": [
                entry.as_dict()
                for _, entry in sorted(
                    self.entries.items(),
                    key=lambda kv: (kv[0].file, kv[0].function, kv[0].property),
                )
            ],
        }
        path.write_text(json.dumps(payload, indent=2) + "\n")

    # -- using -------------------------------------------------------------

    def covers(self, key: Key) -> bool:
        return key in self.entries

    def split(self, keys: list[Key]) -> tuple[list[Key], list[Key]]:
        """(new, known) for the findings of this run."""
        new = [k for k in keys if k not in self.entries]
        known = [k for k in keys if k in self.entries]
        return new, known

    def stale(self, keys: list[Key]) -> list[Key]:
        """Accepted findings that did not occur this run.

        Worth surfacing: an entry that no longer matches anything grants
        permission for a finding that cannot happen, and will go on granting
        it to some future finding that happens to match.
        """
        seen = set(keys)
        return [k for k in self.entries if k not in seen]


def key_for(source: Path, function: str, property_text: str, root: Path | None = None) -> Key:
    """A finding's identity, with the path made relative so the baseline
    survives being checked out somewhere else."""
    path = Path(source)
    base = root or Path.cwd()
    try:
        relative = path.resolve().relative_to(base.resolve())
    except ValueError:
        relative = path
    return Key(str(relative), function, property_text)
