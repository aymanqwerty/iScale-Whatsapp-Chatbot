"""Output eval for the WhatsApp receptionist prompt.

This package is the answer to one question: *did a change to the prompt make
the bot's answers worse?* The project's pytest suite already covers the
deterministic scaffolding - state machine, guard, retrieval. What it cannot
cover is the thing that is actually non-deterministic: what the model writes
once the prompt and the retrieved knowledge reach it.

So this harness runs real user messages through the real pipeline
(TopicGuard -> BM25 retrieval -> prompt assembly -> LLM) and scores the reply
against the behaviours the prompt promises: quote a published fee, never claim
to have booked a call, never invent a discount, refuse off-topic messages,
tailor the discovery pitch to the person. Any prompt version can be swapped in,
so the current prompt and the first-ever prompt can be scored on the same cases
and compared.

Entry point: ``python -m evals.run`` (see ``run.py`` for flags).
"""
