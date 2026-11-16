# The LLM triage path: what is validated, and what is not

veripp's pitch is "AI-operated formal verification", so the triage path
deserves the same evidence as the solver path. This records how far that
evidence goes.

## The short version

On 16 counterexamples whose answer was already written down (11 false
alarms, 5 confirmed bugs), measured 2026-11-15:

| model | prompt | confirmed bugs kept | false alarms dismissed |
|---|---|---:|---:|
| Claude Haiku | call line only | 2 / 5 | 11 / 11 |
| Claude Haiku | **with the caller's code** | **5 / 5** | 9 / 11 |
| Claude Sonnet | call line only | 3 / 5 | 10 / 11 |
| Claude Sonnet | **with the caller's code** | **5 / 5** | 10 / 11 |
| Claude Opus | call line only | 4 / 5 | 11 / 11 |
| Claude Opus | **with the caller's code** | **5 / 5** | 10 / 11 |
| Claude Fable | call line only | 4 / 5 | 10 / 11 |
| Claude Fable | with the caller's code | not measured | not measured |

Three things follow, in order of how much they matter.

1. **The prompt was the problem, more than the model.** Shown a call as one
   bare line, every model dismissed at least one confirmed bug, and all four
   dismissed the same one. Shown the caller's code above the call, all three
   models that were re-asked kept all five. veripp now sends that code.
2. **Capable models err in the opposite direction from the 7B one.** The
   local model called everything a real bug. These call real bugs harness
   artifacts, which is the worse error: a false "real bug" costs a reader
   an investigation, a false "artifact" hides a defect.
3. **A wrong dismissal is not always caught by the solver.** Twice, a model
   dismissed a confirmed bug and proposed a precondition that ESBMC accepted,
   which turns the finding into a `PRECONDITIONED` proof. See
   [below](#a-wrong-dismissal-is-not-free).

The table is small and has caveats that change how much to read into it;
they are in [What this does not show](#what-this-does-not-show).

## How it was measured

`benchmarks/triage_bench.py` separates what veripp asks from who answers.

```bash
./benchmarks/triage_bench.py capture CORPUS OUT      # save veripp's prompts
# ... something answers each prompt into OUT/answers/NAME/ ...
./benchmarks/triage_bench.py grade CORPUS OUT --model NAME
```

`capture` runs each case to its counterexample with a stand-in model that
records what it is asked: the system text and context veripp would send to a
provider, byte for byte. `grade` replays saved answers through veripp's own
loop, so a reply is read by the parser a provider's reply is read by, and a
proposed precondition is checked by ESBMC.

**The cases.** Each is a function veripp reports a counterexample for today,
whose verdict an earlier record established by reading call sites and, for
the real ones, by a sanitizer run: `HUNT_FIELD_REPORT.md`, `CORPUS.md` and
the individual bug write-ups in this directory. The script cites the record
beside each case. Other documented findings are not in the set: veripp now
refuses or proves some of them itself, so no model is asked, and two need a
build configuration the script does not carry.

**Who answered.** No API key was available, so the answers came from Claude
Code subagents pinned to a model tier, each told to act as one API call:
read one prompt, reply as that system prompt asks, open nothing else. Their
transcripts were checked for reads outside the prompt files and there were
none. The raw replies and the graded results are in
[`triage_results/`](triage_results).

## What the models got wrong, and why

### All four dismissed the NetBIOS over-read

`netbiosns_name_decode` reads two bytes at a time until it meets a NUL, a
`.` or a non-capital, with no length. It is real: the bytes are a UDP
datagram's ([LWIP_NETBIOS_NAME_DECODE.md](LWIP_NETBIOS_NAME_DECODE.md)).
Every model answered `harness_issue`. Sonnet's reasoning is representative:

> The only call site passes encname, which is a name string, and the function
> is written for a NUL-terminated C string. The harness built an unterminated
> buffer on purpose (--unterminated), so the counterexample is about the
> harness and not the code.

That is a fair reading of what it was shown, which was this:

```
Call sites in the same file (what real callers pass):
line 78: netbiosns_name_decode(encname, name_dec, sizeof(name_dec));
```

The line names the argument. What decides the question is what `encname`
*is*, and that is six lines up, in the caller. An earlier
version of this file guessed that "a model asked to judge reachability
without being able to look at callers is being asked to guess". This is
that, measured.

### The fix: show the caller, not the call

The prompt now carries, for the first six call sites, up to twelve lines of
the caller above each call, stopping at the top of the enclosing function:

```
The caller's code leading up to each call (where the arguments come from; `>` marks the call):
     69 |     char *packet = (char *)malloc(MIN_PACKET);
     ...
     72 |     const char *encname = packet + HDR_LEN + 1;   /* question_hdr->encname */
     ...
>    78 |     netbiosns_name_decode(encname, name_dec, sizeof(name_dec));
```

Re-asked with that, Haiku went from 2 of 5 confirmed bugs to 5 of 5, Sonnet
from 3 to 5, Opus from 4 to 5.

It has a cost. With more code to read, Haiku called two false alarms real
that it had dismissed before (`parse_string`, `npr`) and Opus one (`npr`).
That is the cheap direction to be wrong in, and the trade is the right one,
but it is a trade.

### `npr` splits the models, and may be a labelling question

tinyexpr's `npr` multiplies by a value that is `NAN` when `n < r`, and the
checker reports NaN. The field report calls it false: NaN is tinyexpr's
error value and producing it is defined behaviour. With the call line only,
Sonnet and Fable call it `real_bug`, on the grounds that any expression can
reach it; with the caller's code, Haiku, Sonnet and Opus all do. It is the
only false alarm any model but Haiku got wrong in either round, so the
"false alarms dismissed" column is mostly a count of this one case. Both
readings are defensible; it is scored against the field report.

## A wrong dismissal is not free

veripp's own documentation used to say that a wrong answer costs a retry
rather than soundness, because the solver checks every proposal. That holds
when a model wrongly says *real bug*. It does not hold the other way:

| model | confirmed bug | proposed, and accepted by ESBMC |
|---|---|---|
| Sonnet | `vslprintf_v` (`%.*v` calls `strlen` on a counted field) | `p[0] == 0 \|\| p[1] == 0 \|\| p[2] == 0 \|\| p[3] == 0` |
| Haiku | `_convert_utf16le_to_utf8` (destination size discarded) | `utf8_len >= 3 * utf16_len` |

Both preconditions are satisfiable and exclude the failing input, which is
all the solver can check. veripp then reports the function as proved under
that precondition. It says `PRECONDITIONED`, never `PROVED`, and prints the
precondition, so nothing is hidden from a reader who looks. But the finding
has left the list of counterexamples, on a model's say-so about what callers
pass.

Both happened with the bare call line. With the caller's code, neither model
dismissed either bug. The check that would close this properly is to put the
precondition to the callers, as veripp already does for literals and for
static functions.

## What this does not show

- **16 cases is a small set.** One case is six points of a column. The
  differences between models with the same prompt are within that noise;
  the difference between the two prompts is not, because it is the same
  cases moving the same way for all three models.
- **Four of the five confirmed bugs are read from `benchmarks/repro/`**,
  which holds the functions as they were when found. parson and lwIP have
  since fixed two of them upstream, so a fresh clone verifies. The caller in
  those files is this repository's own reproduction, whose `main` builds the
  failing input. That is more telling than a real caller's code would be, so
  the caller-context result is likely an upper bound.
- **A subagent is not an API call.** It carries an agent's own system
  prompt, and veripp's system text reached it as a file. The tiers are
  named as the tool names them (haiku, sonnet, opus, fable), not as model
  ids.
- **The two rounds were not run the same way.** Haiku and Sonnet answered
  the first round one case per agent. Opus, Fable and the whole second round
  answered eight cases per agent, told to treat each independently, after
  the one-per-agent Opus run hit a usage limit. An agent that sees eight
  cases can learn the mix.
- **Fable was not re-asked with the caller's code.** Its usage limit was
  reached first. Two of Opus's second-round answers were collected a day
  after the rest for the same reason.
- **Some preconditions were not checked.** Under load a few replays ended
  without a verdict and are marked `(not graded)` in the result files; the
  labels in the table above are read straight from the replies and are
  complete.
- `demo/cve-2019-13223`'s `predict_point` is not a case. With every check
  on, its first counterexample is an overflow for an `x` outside the
  segment, not the CVE's division by zero, and no record says whether a
  caller can pass that.

## A defect this found on the way

One prompt was 758,000 characters. The checker prints the whole containing
object as the value of every field write, and miniz's `tdefl_compressor`
holds two 32 KB dictionaries, so one counterexample was three quarters of a
megabyte of zeros. That is more than most models accept and is billed in
full by the ones that do. Counterexample inputs in a prompt are now cut to
2,000 characters a value and 12,000 in all, and each cut says how much was
left out.

## Earlier: a 7B local model

Exercised with a local ollama server (`qwen2.5-coder:7b`) through veripp's
own loop, on `eval_triage.py`'s two cases:

| model | classification | solver accepted | time |
|---|---|---|---|
| `ollama:qwen2.5-coder:7b` | 0/2 | 0/2 | 44.4s |

Both failures were a `missing_assumption` reported as `real_bug`, and it was
inconsistent about it: one answer says *"not reachable in practice since
there are no call sites provided"* and then labels the finding `real_bug`.
Both cases are in the set above, and every Claude tier got both right.

## Still not measured

- Any model through its real API, and any non-Anthropic hosted model.
  `eval_triage.py --models ...` and `triage_bench.py` both do it with a key.
- Cost and latency per finding.
- Whether the caller-context result holds on callers that were not written
  to reproduce the bug.
