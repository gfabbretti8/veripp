# The LLM triage path: what is validated, and what is not

veripp's pitch is "AI-operated formal verification", so the triage path
deserves the same evidence as the solver path. This records exactly how far
that evidence goes, because the two halves are in very different states.

## Validated: the path runs end to end against a real model

Exercised with a local ollama server (`qwen2.5-coder:7b`) through veripp's own
loop, not a stub:

```
$ veripp verify llmcase.c --function scale_by --model ollama:qwen2.5-coder:7b
note: triage via ollama
Result: counterexample
Violated property: division by zero
Counterexample inputs:  n = -1   d = 0
Diagnosis: real_bug: ... violates the precondition that `d != 0` ...
```

Prompt construction, the provider call, response parsing and integration into
the report all work. 19.8s for one function.

## Measured: a 7B local model is not good enough for this

`benchmarks/eval_triage.py` grades live triage against ground truth
established by reading call sites and validating every proposal with ESBMC:

| model | classification | solver accepted | time |
|---|---|---|---|
| `ollama:qwen2.5-coder:7b` | 0/2 | 0/2 | 44.4s |

Both failures were the same shape, and it is the shape that matters: a
`missing_assumption` reported as `real_bug`. The model over-reports real bugs.
That is the expensive direction — a false "this is a bug" costs a human an
investigation, where a false "this needs a precondition" costs a retry the
solver rejects for free.

It is visibly inconsistent about it too. On the example above the same answer
says *"not reachable in practice since there are no call sites provided"* and
then labels the finding `real_bug`.

## Not measured: whether a capable hosted model does better

Nothing here has an API key, so no Anthropic, OpenAI, Gemini, Groq, DeepSeek,
Mistral or OpenRouter model has been graded. That is the single largest gap in
this project's evidence, and it sits under the half of the product the name
advertises. One command closes it:

```bash
export ANTHROPIC_API_KEY=...
./benchmarks/eval_triage.py --models claude-haiku-4-5,claude-sonnet-5,claude-opus-5
```

## One observation worth testing when a key exists

A Claude Haiku *agent*, given veripp's skill and the ability to read the
source, triaged five cJSON findings and agreed with a careful manual pass on
all five, including the count (see CORPUS.md). veripp's own triage gives a
model far less: the counterexample and a source excerpt, with no ability to go
and read the call sites that decide reachability.

So the fair question is not only "which model" but "how much context does the
triage prompt need". A model asked to judge reachability without being able to
look at callers is being asked to guess, and that is roughly what the 7B
result looks like. Worth testing both prompt shapes against the same model
before concluding anything about model size.
