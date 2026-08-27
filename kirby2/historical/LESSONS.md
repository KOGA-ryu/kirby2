# Historical lesson packs

List and run the packaged lessons with:

```bash
python3 -m kirby2 lesson-list
python3 -m kirby2 lesson-run exact_queue_priority
python3 -m kirby2 lesson-run reconstruction_liquidity_decision \
  --auto-complete \
  --events-jsonl /tmp/reconstruction.jsonl \
  --debrief-json /tmp/reconstruction-debrief.json
python3 -m kirby2 audit-historical-lessons
```

Each versioned JSON pack contains the lesson identity, date, instrument, market,
`EXACT_REPLAY` or `RECONSTRUCTION` mode, evidence-labeled provenance and context,
full fixture time window, learning objectives, pinned local replay source, reveal
policy, structured post-session explanation, limitations, and training questions.

`lesson-run` starts in `READY` and remains incomplete by default. Its blind
presentation contains no date, instrument, market, event, outcome, title, or
authoritative final-run object. `--auto-complete` is the deterministic CLI
demonstration; applications can instead call the runtime actions one at a time.

The runtime state sequence is:

1. `READY`: no source commands have been exposed.
2. `BLIND_RUNNING`: `start`, `advance_to`, `step`, `pause`, and `resume` drive
   source activity using simulation time.
3. `QUESTIONS`: playback has reached the source-window end and configured
   responses are recorded with decision-time observable context.
4. `COMPLETE`: every response is frozen and cannot be edited.
5. `REVEALED`: protected identity and source outcome become available.
6. `DEBRIEFED`: frozen responses are shown beside declared evidence. The
   comparison records shared terms but does not make an automatic correctness
   claim when the evidence cannot support one.

`BLIND_UNTIL_COMPLETION` never hides the data boundary: users still see whether
source orders, trades, book events, and aggressor side exist and whether displayed
orders are synthetic. A source without queue capability reports the book as
`UNAVAILABLE`; it is not represented as an empty measured queue. Reconstruction
queues and trades are labelled `SYNTHETIC_RECONSTRUCTION`. Missing trade aggressor
side is returned as unavailable rather than inferred.

Player orders during the blind decision phases execute only in a separately
labelled counterfactual clone of the source prefix. They never enter the
authoritative source journal. Exact order-message sources use
`COUNTERFACTUAL_ON_SOURCE_DERIVED_BOOK`; reconstructions remain
`COUNTERFACTUAL_ON_SYNTHETIC_RECONSTRUCTION`; unsupported hidden book state is
explicitly labelled synthetic.

The JSONL session artifact stores canonical input commands, phase events,
decision-time contexts, responses, counterfactual overlays, source digest, and
result digest. Reapplying the input records to the same lesson reproduces the same
events, presentations, responses, and result digest.

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
