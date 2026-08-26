# Historical lesson packs

List and run the packaged lessons with:

```bash
python3 -m kirby2 lesson-list
python3 -m kirby2 lesson-run exact_queue_priority
python3 -m kirby2 lesson-run reconstruction_liquidity_decision \
  --events-jsonl /tmp/reconstruction.jsonl \
  --debrief-json /tmp/reconstruction-debrief.json
```

Each versioned JSON pack contains the lesson identity, date, instrument, market,
`EXACT_REPLAY` or `RECONSTRUCTION` mode, evidence-labeled provenance and context,
full fixture time window, learning objectives, pinned local replay source, reveal
policy, structured post-session explanation, limitations, and training questions.

`BLIND_UNTIL_COMPLETION` hides the date, instrument, market, and event in the
session view. It never hides the data boundary: users still see whether source
orders/trades/book events exist and whether displayed orders are synthetic. The
completed debrief reveals identity and explains the event, market context, what
happened next, and why the session matters.

Evidence statements use explicit categories:

- `KNOWN_HISTORICAL_FACT`
- `MEASURED_SOURCE_DATA`
- `SYNTHETIC_RECONSTRUCTION`
- `LOCAL_FIXTURE_METADATA`
- `DERIVED_FROM_SOURCE`

Zero counts are retained in the debrief inventory. This matters for the bundled
fixtures: they are legally local pedagogical data, not observations of a real
security. The exact pack replays its local source messages without synthesis but
makes no real-market claim. The reconstruction packs state that historical Level
2 is missing and label every generated order and queue as synthetic. No lesson
loader or runner acquires network or paid data.
