# Distribution runtime contract

Every `DistributionProfile` must define exactly the seven supported purposes.
Unknown, missing, zero-valued positive-purpose, and otherwise invalid fields are
rejected when the profile is constructed.

| Purpose | Unit | Runtime consumer |
| --- | --- | --- |
| `order_size` | shares | resting limit-order quantity before scenario scaling |
| `trade_size` | shares | aggressive market-order quantity before scenario scaling |
| `cancel_size` | shares | target cancellation-volume budget |
| `queue_depth` | shares | initial resting quantity at each book level |
| `limit_placement_depth` | ticks behind the same-side best | non-crossing limit placement |
| `inter_event_timing_modifier` | per mille; 1,000 is 1x | sampled interarrival duration |
| `spread_state_duration` | simulation microseconds | latent touch/depth-favoring placement-state duration |

Cancellation budgets use whole-order exchange cancellation. The command record
reports the requested and actual quantity, overshoot or unfulfilled quantity,
and every affected active simulated order ID.

The latent spread state alternates between touch-favoring and depth-favoring.
It adjusts ordinary non-crossing limit placement by one tick; it never assigns a
book price or emits a direct price command. State changes and arrivals are driven
only by simulation time.

Every configured runtime draw uses the engine-owned seeded RNG and appends a
`distribution_draw` replay record with its sequence, profile, purpose, value,
unit, simulation time, and consumer.
