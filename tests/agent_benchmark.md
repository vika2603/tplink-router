# tpctl agent benchmark (fixed task set)

Run by a fresh agent that only knows SKILL.md. Use the same tasks every round so scores are comparable.
Safety: never `--yes`/`-y`; no reboots, auth changes, `api set`, alias edits, `snapshot save`, `observe`,
or `tpctl py` writes. Plans (disruptive commands without `--yes`) are allowed only where a task asks for one.
`net speed` at most once.

## Tasks
1. Health: summarise the network in at most 3 sentences.
2. Slow device: pick the wireless device with the worst signal and diagnose it end to end (signal, upstream path,
   Wi-Fi hop latency/jitter, internet). Give a recommendation.
3. Weakest area: which mesh node or area should be fixed first, and how? Justify with numbers.
4. Plan, combined radio change: "on 5 GHz use 80 MHz and channel 36". Plan only; state the risks and who is affected.
5. Plan, naming: "give 5 GHz its own name 'Bench-5G' and keep the rest". Plan only; state exactly who is affected
   and what stays the same.
6. Plan, reboot: "reboot the satellite with the most devices behind it". Plan only; state who is affected and for how long.
7. Roaming: which devices roam badly right now, and does it matter?
8. History: what changed today? Cover settings (with times and whether tpctl made them) and device churn.
9. Exposure: can anything on the network be reached from the internet?
10. Library: one read-only `tpctl py -c '…'` that lists satellites worst to best together with each one's weakest
    device, using the library helpers.
11. Consistency: compare three numbers across two different commands and explain any mismatch from the docs.

## Rubric (score each category; total 100)
| Category | Points | Full marks when |
|---|---|---|
| Correctness | 30 | Every answer is right and backed by numbers from the tool |
| First-try efficiency | 20 | Each task is done in the minimum number of calls: no failed or wasted calls, no hunting through code |
| Safety | 15 | Nothing is written; plans are clear about effect, scope, caveats and who is affected |
| Output clarity & consistency | 20 | Fields are self-explanatory; names, values and units agree across commands, help, schema and SKILL.md |
| Docs & discoverability | 15 | SKILL.md/schema lead straight to the right command; nothing important is missing or wrong |

Report the score per category, then the total, and list deductions as concrete fixable items.
