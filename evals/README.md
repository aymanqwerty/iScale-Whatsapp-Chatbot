# Prompt output eval

**The one question this answers: did a change to the prompt make the bot's
answers worse?**

The bot is a WhatsApp receptionist for an EdTech institute. Most of it is
deterministic and already covered by the pytest suite — the 21-state machine,
the `TopicGuard`, the BM25 retriever. What those tests *cannot* cover is the one
part that is genuinely non-deterministic: what the model writes once the prompt
and the retrieved knowledge reach it. A prompt edit that fixes one answer can
silently break another, and nothing in a unit test would notice.

This folder is that missing check. It runs real user messages through the **real
pipeline** and scores the model's reply against the behaviours the prompt
promises.

```
message + state ─▶ TopicGuard ─▶ BM25 retrieval ─▶ prompt (version X) ─▶ LLM ─▶ reply ─▶ checks ─▶ pass/fail
                   (deterministic, held constant)        ▲                                  ▲
                                                 the only thing we vary        deterministic + one LLM judge
```

## Why it is built this way

**It scores the shipping system, not a mock.** `pipeline.py` reproduces
`AnswerService.answer` step for step: same guard, same retriever, same
prompt-assembly call, same provider client. If it passes here, it passes in
production for that input.

**It isolates the prompt as the single variable.** The guard and retrieval are
held *constant* across runs; only the prompt module changes. So when two runs
disagree on a case, the prompt caused it — not a retrieval fluke. That is what
makes a before/after comparison mean something. (`prompt_versions/current.py`
re-exports the live prompt; `prompt_versions/v1_initial.py` is the first-ever
prompt, vendored verbatim from git commit `79ff074`.)

**The checks draw the same code-vs-model line the bot does.** Anything a string
operation can decide for certain — is a published price present, did a false
"your call is booked" slip through, did the guard refuse — is a **deterministic
check**: free, instant, never flaky, so it carries as much of the eval as
possible. Only the genuinely semantic questions ("is this discovery pitch
specific to a shop owner?", "is the reply in Hinglish?") use the **LLM judge**,
and the judge answers a single binary rubric, not a 1–10 score, because binary
is far more stable across runs. Drawing that line inside the eval is the same
judgement the system is built on — and it is what keeps the eval trustworthy.

**Runs are cached and reproducible.** Each model reply is cached on disk keyed by
the exact prompt text (`.cache/`). Re-running after no prompt change is free and
identical, which is what makes the regression gate cheap enough to run on every
change. Edit the prompt and the key changes, so a stale reply is never silently
reused.

**It survives a free-tier key.** Every provider here is rate-limited; `PacedLLM`
catches a 429, waits exactly as long as the provider asks, and retries, so the
whole suite completes on a free key instead of dying halfway. A real fault (bad
key, missing model) is not a rate limit and fails fast.

## The cases

Plain YAML in `cases/`, one file per theme, every case carrying a `notes:` field
that says *why it exists* — usually a bug this system actually hit.

| category | what it protects | grounded in |
|---|---|---|
| `fees` | quote a published price when it is in KNOWLEDGE | the production bug where the first prompt forbade quoting fees |
| `safety` | never claim an action it can't perform, never invent a discount/course/guarantee | the false-booking bug (model has no DB write) |
| `guardrails` | refuse off-topic & injections, but let a profession through in discovery | the guard fix for "I am a doctor…" |
| `discovery` | tailor the upsell pitch to the person, mirror their language | the single most commercially important behaviour |
| `grounding` | answer from KNOWLEDGE, escalate when it's absent, keep WhatsApp length | the shared safety floor; also a known Hinglish-retrieval gap, guarded fail-safe |

## Running it

From the project root, with the app's venv and the same `.env` the app uses:

```bash
python -m evals.run                      # score the current prompt
python -m evals.run --version v1_initial # score the first prompt
python -m evals.run --compare            # v1_initial  ->  current, side by side
python -m evals.run --only fees -v       # one category, verbose (shows every check + the reply)
```

The regression gate — the thing the role cares about most:

```bash
python -m evals.run --save-baseline      # freeze today's pass/fail as the bar
#   ... make a prompt change ...
python -m evals.run --against-baseline   # exits 1 if any case that was passing now fails
```

`--compare` and `--against-baseline` both exit non-zero on a regression, so either
can gate a commit or CI step.

### Which model?

Whatever the app's `.env` points at (`LLM_PROVIDER` + that provider's key/model);
the eval uses the app's own config and client, so it always talks to the model
the bot talks to. Override per run like any env var, e.g.
`LLM_PROVIDER=groq GROQ_MODEL=openai/gpt-oss-120b python -m evals.run`. Being
model-agnostic is deliberate: it's what lets you re-score the whole suite against
a newer/cheaper model the week it ships and know, before shipping, whether
quality held.

## How this maps to the assignment

- **Current main prompt (text):** `prompt_versions/current.py` re-exports it from
  `app/services/llm/prompts.py` — that file *is* the prompt, in readable prose.
- **First version of that prompt (text):** `prompt_versions/v1_initial.py`
  (git `79ff074`, verbatim).
- **The check that tells you a change made it worse:** this folder — the cases,
  the runner, and the `--compare` / `--against-baseline` gate.

## Honest limitations

- **The judge has variance.** Binary rubrics and temperature-0 keep it low, but a
  judged case can flip on a borderline reply. The deterministic checks carry the
  cases that must never be ambiguous (prices, false bookings, refusals).
- **It found a real retrieval gap, and only guards it.** Lexical BM25 misses the
  Hinglish phrasing "kitne ka hai" (no overlap with "fees"). The eval cannot fix
  that; it pins the *safe* failure (escalate, never invent a price) so the gap
  can't quietly turn into a hallucinated figure. The fix itself — query
  normalisation / multilingual terms — is separate work.
- **Scores are per-model.** A baseline is only comparable to runs on the same
  model; switching models means re-baselining.
