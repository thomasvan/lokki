# What a cut costs and saves, measured (2026-10-04)

Every number here is measured on this host, and each one names where it came
from. The meter is the upstream's own, in whole percent: one point is the
resolution, and that limit is stated wherever it bites rather than smoothed over.

**A request the relay cuts is billed, and costs less than one left to finish.**
The provider finalizes no response on a cut request, so it reports no usage, and it
still charges for the cached prefix it had already read when the socket dropped:
"no usage event" is not "no charge". The relay lowers the bill and does not remove
it; this page measures by how much.

**The saving is about 2× on a replayed turn of the owner's own cache-warm Codex body**, and
0.78 of the direct spend over a whole Codex task, a pelican drawing on `gpt-6-astra` (0.64–0.95 on the
meter's rounding, `relay-8371.md`). The ratio
**per upstream token** is 30× or more on cache-cold filler bodies, but a client
turn is not one upstream token: the relay re-sends the context across a measured
3.5 segments, so the ratio **per client turn** is ~6× on cache-cold filler bodies
(45× in the cleanest window) and about **2×** on the owner's own turns. Filler
bodies do not describe a real client's turn; the 2× is the realistic number and is
measured in the arm G section at the end. The two ratios are kept apart on purpose.

## What the saving rests on

- **The request is kept unfinished on purpose, where it can be.** The relay asks the model to follow its
  answer with a marker, an appendix of filler and a call to a padded placeholder tool
  (with `text_calls`, to write the command as text), so the answer arrives while the
  request is still open, and it closes its read there. The appendix is text a direct
  request would not contain, so the saving is not text the provider was spared; it is
  that a request cut at the marker or at a written command is charged as an unfinished
  one. A request cut after the reasoning and a real tool call have finalized is charged
  near a finalized one: the pelican run's three relayed dispatches cost 1.0 point each,
  against 0.18 to 0.24 for a cut with nothing finalized (*The same pair on a client's
  own command*). On a tool-calling turn there can also be fewer requests, and part
  of a relayed run's lower cost can be work it did not do: a relayed run that stops before its file is written is not like-for-like
  with a direct one (*The same pair on a client's own command*).
- **The provider's accounting of a request that never finishes.** The fit below puts
  a cut dispatch's uncached input at under about 1/30 of the finalized rate, and the
  charge follows the cached prefix, not the output (*What this does not say*). That
  is read off the meter on one plan and one model family, not documented by the
  provider, and it can change. If the provider counted a cut request in full, the
  saving would shrink to the requests avoided; the method that would show it is the one
  used throughout this page, a meter read before and after traffic with
  byte-identical bodies on each side.

## The two rates

| path | rate | measured by | ratio |
| --- | --- | --- | --- |
| **finalized** (no cut, or a cut that did not hold) | ~1 point per **10,538 uncached input tokens** at `gpt-6-astra`'s price — the *uncached-input* coefficient of the spend model below, measured on cache-cold bodies; a cache read counts ~0.10× that per token and output ~5.1×, and other models are priced differently | arms D and S: 6 identical-shape requests each, 172,428 tokens → +18 points, 2,057 tokens → +1. Reproduced in the same window as arm C by arm D2: 172,757 tokens → **+18** | 1× |
| **aborted** (the cut fired) | **between** ~1 point per **250,000** and ~1 point per **2,200,000** aborted input tokens *on cache-cold filler bodies* — and **not ~0 on a real body**: arms H4 and H5, 12 relayed turns of the owner's own body each, cost +5 and +4 against +8 for the same 12 turns direct. See *The cut removes the usage event, not the charge* | arm P3-scale: 1,721,425 tokens → +6 after subtracting 18,863 uncached tokens of another client's traffic. Arm C-scale: 4,274,417 tokens → **+2**, in a window with no other finalized traffic at all | **30×–230×** on filler, **~2×** on a real body |

Per **token** on cache-cold filler bodies, then, the cut is at least ~30× cheaper,
and the larger of the two arms says it may be ~230×. That is a statement about
filler bodies and about the *input* they abort; on a real Codex body the same
method gives about **2×**, because there the provider has the client's own
conversation cached and charges each dispatch for the prefix it reads (see *What
the difference is: the charge follows the cached prefix*). The old wording —
"**≥96% of the charge is not incurred**" — is not supported and is withdrawn.

**Why the two filler arms differ by ~8×.** Arm C is 2.6× the volume and its window
is cleaner: its only oracle event is a *failed* request of zero tokens, no other
client's traffic ran, and cliproxy's own log shows an even 73/73 split of the
dispatches across the two metered accounts, so neither account can be hiding the
charge behind the other's rounding. Arm P3-scale's window held 18,863 uncached
tokens of another client's finalized traffic (~1.9 points). What the 1-point
integer meter cannot exclude in either arm is a charge that posts later than the
read: arm D2 bounds the posting latency at **under 2.5 minutes** for a finalized
charge, and arm C's reading was unchanged 3 minutes later, but no arm here can
bound it for an aborted one. The 2026-10-05 campaign settles what those two arms
were measuring: both used cache-cold filler, and a cache-cold dispatch of the
owner's own body charged **0**, so the two are spread around a floor rather than
two rates for an aborted token. The per-token ratios above therefore remain a
filler-body bound; the transferable price is the per-dispatch one measured below.

## The number that matters for a turn: two matched pairs

A client turn is not one upstream request. The cut re-sends the context to
continue, so the same client work becomes several requests. Both pairs below post
**byte-identical bodies** on each side, so the bodies — and their token density —
cancel, and the meter does the rest.

| pair | body | direct / unfixed path | through the cut | ratio | saved |
| --- | --- | --- | --- | --- | --- |
| filler, smaller windows (arms D vs P3-scale) | 60,140 chars, unique random text, cache 0 | 6 requests → **+18 points = 3.00/request** | 12 requests, 58 segments → **+6 points = 0.50/request** | 6.0× | **83%** |
| filler, one clean window (arms D2 vs C-scale) | the same shape, different filler seeds | 6 requests → **+18 points = 3.00/request** | 30 requests, 144 dispatches, 4,274,417 aborted tokens → **+2 points = 0.067/request** | 45× | **97.8%** |
| the real leaking request (arms A vs B+E) | the recovered Codex body, 61,545 chars, `text.format: json_schema` | arm A: 4 requests → **+3 points = 0.75/request** | arms B and E: 14 requests → **+3 points = 0.21/request** | ~3.5× — **not resolvable** | — |

Three pairs, two body shapes. Only the first two can carry a ratio, because a
ratio needs both sides to be bigger than the meter's floor: their deltas are 18
and 6 points, 18 and 2. The third pair's two sides are **1–3 integer points each**,
so its ratio could be anywhere from ~1.7× to ~7× and it is not quoted as one. What
it establishes is exact and qualitative: **4 of 4 runs finalized with four usage
events before the fix, 14 of 14 cut with zero events after it** — the leak class is
closed, and that is what that pair is for.

The floor for the per-turn number on **cache-cold filler bodies** is therefore the
conservative pair: **≥6× / ≥83% saved per client turn**. The clean single-window
pair says **45× / 97.8%**, matching the 30–230× per token above. The gap between
the conservative and the clean reading is the same disagreement, seen per turn
instead of per token, and it is not resolved here. Neither is the number for a
real client: both pairs pay the full uncached input price on every token, the
spend model below shows a cache-warm Codex turn does not, and — decisively — the
relay's own side of both pairs is measured against an assumption that an abort
charges nothing, which section *The cut removes the usage event, not the charge*
refutes. The number to use for this workload is arm G's **~2×**, and it is the
only one of the three that was measured with the relay's charge in view.

What all three agree on is the *mechanism* of the multiplier: the relay re-sends
the context to continue, ~2.9–4.8× the body volume for one client turn. That is a
real cost, not a rounding error, and it is why the per-turn ratio is smaller than
the per-token one.

## Turns per 5h window (the number a user feels)

At the matched-pair rates, the pool's 300 points of 5h capacity buy, for a turn
of the shape the arms used (a 60,000-character body):

| path | points per turn | dispatches per turn | points per dispatch | turns per 100-point window (one account) |
| --- | --- | --- | --- | --- |
| direct, the owner's own body (arm G-A) | **0.67** | 1 | — | ~150 |
| relayed, the same body (H4 / H5 / O6) | **0.25–0.42** | 1.6 | **0.158–0.21** | ~240–400 |
| direct, 60,000-character cache-cold filler (arm D) | 3.00 | 1 | 3.00 | ~33 |
| relayed, the same filler (C-scale-30) | 0.067 | 4.8 | 0.014 | ~1,500 — cold prefix, see below |

**For the workload that exists — the owner's own Codex turns — the measured answer
is about 2×.** The 6× and 45× rows that used to head this table are properties of
cache-cold filler bodies; the 2026-10-05 arms measure why (a dispatch with no
cached prefix reads nothing to be charged for) and price the transferable unit,
which is the **dispatch**, not the turn: the relay re-sends the client's context on
every continuation, so a turn's cost is its dispatch count times the price of the
cached prefix. The earlier version of this report said 30× per turn and ~410 turns
per window; that was the per-token ratio applied to the wrong unit, and this table
is now measured per turn *and* per dispatch instead of derived from either.

## What the relay did in one day (its own run log; costs nothing to produce)

The run log separates the two traffic classes by a fact in each record rather
than by a guess: a real Responses-API client forwards its own parameters
(`params_forwarded` non-empty — `client_metadata`, `include`,
`parallel_tool_calls`, `prompt_cache_key`, `reasoning`, `text`, `tool_choice` in
every one of today's runs), while this campaign's probes and the scheduled
abort-only arm send a bare body and forward none.

| traffic class | runs | aborted | finalized | leak rate | aborted upstream chars | segments/run (aborted runs) |
| --- | --- | --- | --- | --- | --- | --- |
| Codex CLI client | 123 | 102 | 21 | **17.1%** | 46,085,152 | 3.50 |
| measurement traffic (this campaign's probes, the scheduled abort-only arm) | 78 | 75 | 3 | 3.8% | 7,025,732 | 2.84 |
| **whole day** | **205** | **177** | **28** | 13.7% | **53,110,884** | — |

The table is a snapshot of the run log at seq 319 (`2026-10-04T13:04Z`); the log
grows while the relay runs, so a reader recomputing it later will see more runs
than this — which is why every cell is also reproducible by replaying the counts
from that seq. `segments/run` is the mean over that class's **aborted** runs (the
finalized ones would drag it down without meaning anything: they produce one
segment fewer).
The four runs of arm A are excluded from the client row: they are this
campaign's replays of a client body, they are identified by their seq (316–319),
and counting them as client traffic would inflate the leak rate with the
campaign's own bill. The campaign's deliberate *finalized* arms (D, S, the
canaries, D2) are not in this table at all: they are posted straight at cliproxy,
without the relay, which is the point of them.

What the day cost, at the rates above. The finalized side is exact (the provider
reported it); the aborted side is arithmetic from a character count, and the two
readings below are why it is quoted as a range:

| source | volume | points | how |
| --- | --- | --- | --- |
| client runs that finalized anyway (the leak) | 135,357 uncached input tokens, 5,178 output | **14–17** | exact tokens at the fitted spend model (*The meter is a spend meter* below): 12.8 + 2.5 = 15.4 points, and the range is the meter's own ±1 |
| client traffic that was cut | 46,085,152 chars over 102 turns | **4–88** | four readings, printed because the evidence does not choose between them: conservative aborted rate + this client's density (5.99) → 27–31; the same, charging only the uncached 41% → 11–13; conservative rate + filler density (2.095) → 77–88; the larger arm's rate → ~4 |
| the scheduled abort-only arm | 34 runs, 0.06M chars | ~0 | below the meter's floor check after check |
| this campaign's measurement traffic | 78 runs, 7.03M aborted chars, plus the finalized arms in the arms file | ~2–8 aborted, ~63 finalized | the arms file's own meter deltas: D +18, S +1, canaries +1, P3-scale +8, A +3, B +1, C-scale and D2 recorded in the arms commit |
| **the relay, one day (client row only)** | 46.1M aborted chars over 357 dispatches | **~56–75** | measured *per dispatch* at the 2026-10-05 arms' rate (0.158–0.21 points on a cached prefix), not per turn — the day's turns averaged 3.5 dispatches against the arms' 1.6 |

The counterfactual is **not** "the same aborted volume, finalized" — that volume
only exists *because* the cut re-sends the context. It is the same client turns
sent once each: ~16.1M characters of client body (123 turns × the measured mean
last-segment body of 131,001 chars), which at this client's density is 2.69M
tokens, 41% of them uncached at the leak sample's measured share → **~92–138
points**. The relayed side of that day is **~56–75 points** at the per-dispatch price the
2026-10-05 arms measure (357 dispatches × 0.158–0.21 points on a cached prefix).
That is above the ~41–52 the same arms' *per-turn* rate produced, and the
difference is why the price has to be quoted per dispatch: the arms' turns needed
1.6 dispatches while the day's averaged 3.5, because the day contains the
`cap`-cut prose turns that deliver thousands of characters and take up to six
dispatches. Against the unchanged counterfactual of ~92–138 points, the day's
saving is **about a third on the central values** (81 against 115), with the two
ranges overlapping at the bottom — not the 43–70% this paragraph carried before,
and not the 60–95% before that. The headline ~2× is a real measurement of the
12-turn A/B, where both sides are the same body and the same turn shape; it is not
a day-level figure. The counterfactual itself is unchanged; what changed, twice,
is that the relayed side is neither free nor priced per turn.

## The leak, and what closed it

The first version of this report counted 23 of 125 client runs as having reached a
real `response.completed`; with this campaign's own four replays excluded the
corrected count is **21 of 123 (17.1%)**, and the mechanism is now known rather
than guessed: those runs are
Codex CLI's **structured-output** requests (`text.format: json_schema, strict`),
where neither abort trigger can be written — the model cannot emit the marker
line and cannot call the padding tool without violating the schema the client
asked for. In the diagnostic trace, structured-output runs bill at **8 of the 12**
the trace held before the cut, against **3 of 110** for prose; the cross-tab that
printed that table from the trace's own frames is gone with the rest of the
research tooling, so it is re-derived rather than re-run.

The `itemdone` cut closes that class: when upstream closes a text item on a
request that asked for a structured output, the run ends there instead of reading
the provider's terminal frame. The arms above are its proof — arms A and B are the same request, before
and after (`relay-8371.md`, "The `itemdone` cut").

## The owner's own workload: a controlled A/B, and it is lower (2026-10-05, arm G)

Everything above was measured on synthetic bodies. The relay was then routed into
the owner's Codex CLI and the same test was run on **his own request**: a pelican
turn of 136,577 characters, 22 input items, four custom tool calls with their
outputs — the body recovered from the relay's own trace — posted **12 times
straight to cliproxy** and **12 times through the relay**, in adjacent windows,
each arm followed by a 15-minute settle (a charge can land long after the
requests that earned it: arm F).

| arm | requests | upstream chars | finalized events | meter, settled |
| --- | --- | --- | --- | --- |
| A — direct | 12 | 1,639,000 | **12** | **+8 points** (acct-2 +4, acct-3 +4) |
| B — relay | 12 | 2,753,832 over 20 dispatches | **0** | **+2 points** (acct-3 +2) |

**~2× cheaper per client turn — roughly half — not 83–98%, and not the ≥4× the
first revision of this section printed.** The direct side is 8 points. The relay
side is not 2 but 4–5: two later arms of exactly this shape, H4 (+5) and H5 (+4
within five minutes of a settled baseline), reproduce it, and their own 12 runs
finalized nothing and wrote no usage event. A relayed turn of this body costs
about **0.33–0.42 points** against the direct path's 0.67, which is the number
this workload supports. The full measurement, and why the oracle is silent while
the meter moves, is in *The cut removes the usage event, not the charge* below.

The reason the relay is not free is not that aborted requests bill in full: it is
that the provider charges for the cached prefix it had already read when the cut
fired, without ever sending a usage block — and the 2026-10-05 arms measured
which part: the cache reads, not the output. The direction of the earlier hedge was
wrong — a late post from the direct arm would have made the direct side costlier,
not the relay side free.

The same measurement is why the owner's two real sessions looked like a wash in
the raw meter (+7 points direct, +7 relayed): those readings were taken across a
5h window rollover with charges still posting late, and the settled, same-body
pairs are the ones to believe.

## The meter is a spend meter, not a token counter (fitted and tested 2026-10-05)

Nine windows where every finalized request is attributable and the meter delta is
bracketed by two reads give one three-term fit — **points = a·uncached input +
b·cache read + c·output** — with an RMS residual of **0.80 points**:

| token class | 1 point is | against uncached input | across leave-one-out |
| --- | --- | --- | --- |
| uncached input | 10,538 tokens | 1× | 1/10,112–1/11,013 |
| cache read | 104,896 tokens | **0.100×** | **0.053×–0.129×** (weakest) |
| output | 2,055 tokens | **5.13×** | 3.7×–5.2× |

Rounded: 1 point ≈ 10,500 uncached input ≈ 2,000 output ≈ 105,000 cache-read
tokens — at `gpt-6-astra`'s price; other models are priced differently, below.
Two of the nine windows were run later that night as a controlled campaign on one
account (only one account enabled) in an empty 5h window, with radio silence until
a settle read 35 minutes after each arm's last request:

| arm | what it posts | its own oracle tokens | observed | predicted |
| --- | --- | --- | --- | --- |
| H1 cold-direct | 5 × unique 60k-character bodies | unc 144,162 / cache 0 / out 3,536 | **+15** | 15.40 |
| H2 warm-direct | 10 × the **same** 60k body | unc 61,292 / cache 227,328 / out 8,278 | **+11** | 12.01 |
| H3 out-direct | 3 × a 331-token prompt, long answer | unc 993 / cache 0 / out 9,097 | **+5** | 4.52 |
| H4 relay-owner | 12 × the owner's body, through the relay | **0 finalized**, 20 dispatches, 2,753,424 aborted characters | **+5** | — |

H2 is the arm the cache coefficient needed: H1 and H2 carry the same instruction
on the same body, and the only difference is that H2's ten requests are
byte-identical, so the provider served 28,416 of its 28,862 input tokens from
cache on eight of them. "Cache reads are free" was already rejected by the six
windows (RMS 1.35 against 0.75); H2 rejects it at the ratio — its eight warm
requests carry 61,292 uncached tokens where H1's five cold ones carry 144,162, and
the two arms cost 11 against 15. Over the nine:

| hypothesis | RMS, other coefficients frozen at the fit | RMS, others refitted |
| --- | --- | --- |
| cache reads are free (`b=0`) | 1.509 | **1.244** |
| cache reads cost the same as uncached (`b=a`) | 11.500 | **5.858** |
| output is free (`c=0`) | 2.870 | **1.899** |
| the fit | 0.798 | — |

Both columns reject all three; the refitted column is the one that answers "could
some other coefficient absorb this term and still fit the data?" 

**The coefficients are correlated and only the ratios are robust.** The arms'
input:output ratios are similar enough that `a` and `c` trade against each other
and the cache term trades against both: dropping H3 moves the cache coefficient
from 0.100× to 0.129×, dropping arm G-A moves it to 0.053×, and `c` ranges
1/1,920–1/2,897. The nine windows, residuals and the leave-one-out are in
`cost_model_2026-10-05`.

### Prices are per model

The same night's terra arm (3 × unique 60k bodies, `gpt-5.6-terra`, all
finalized) carries an astra-equivalent cost of 9.33 points and moved the meter
by **+2** — a factor of **0.21**. Read with the luna result, the meter is a
*multi-model spend meter with per-model prices*, not a switch:

| model | factor, astra = 1 | evidence |
| --- | --- | --- |
| `gpt-6-astra` | 1.0 | the nine-window fit |
| `gpt-5.6-terra` | **0.21** | 3 requests, astra-equivalent 9.33 → +2 |
| `gpt-6-luna` | **< 0.04** | 25 requests carrying 1,195,240 input tokens (150,760 uncached, 1,044,480 cache reads, ~5,000 output), worth ~27 astra-equivalent points across the day, meters flat |
| `gpt-5.6-luna` | **not bounded** | 2 events, ~18k input tokens, ~0.6 astra-equivalent points — too little traffic to bound anything. It is a different model from the `gpt-6-luna` row above |

That is what settles the confound the earlier draft carried: 20 finalized requests
carrying 448,256 cache reads that moved the 5h meter by 0. Fourteen of those 20
are `gpt-6-luna`, and luna carries all but 2,057 of the input and all 448,256 of
the cache reads — worth 17.3 astra-equivalent points and charged nothing, which
bounds luna's factor below 0.06 on its own and below 0.04 with the day's traffic. It is a
**model** control, not evidence about the price of a cache read. The confound
note's own two reads recorded 0; arm S's reads, bracketing the same traffic more
widely, recorded +1 on acct-2, inside the ~2.0 points arm S's own six tiny astra
requests predict.

It also turns the last open question in #26 into a quantitative fit. The owner's
own direct session (14:26–14:30Z; 6 astra + 8 terra + 2 luna events, 446,947
input tokens of which 374,784 were cache reads, 72,163 uncached, 8,089 output)
was recorded at **+7**: astra-only predicts 5.97, per-model factors predict
**7.63** (astra 5.97 + terra 7.78 × 0.21), and pricing every model as astra
predicts 14.36. The +7 is not a coincidence of a single read; it is the model.

The ratios sit in the same neighbourhood as this family's published prices (cache
reads at 10–25% of input, output at 4×–8×): the fit's 10.0% is at the bottom of
the first range and its 5.1× sits inside the second, which is what a *spend*
meter should look like.


## The cut removes the usage event, not the charge (measured 2026-10-05)

Every revision of this report before this one rested on the same three-step
inference: an aborted upstream request never finalizes at the provider, so the
provider never sends a usage block, so the charge is never incurred. The first two
steps are true and were re-verified here. **The third is false.**

**What is true.** Arm H4 posted the owner's own body through the published relay
12 times: 20 upstream dispatches (cuts `toolcall` 4 / `marker` 8), every run
`real_completed: false`, and the usage oracle recorded **zero** events. cliproxy's
own log records all 20 dispatches and every one of them ends at or before the end
of the relay run that owns it — 0 of 20 end later; the last one ends exactly at
the last run's end. So the relay's mid-read close does reach the provider and the
request really is terminated. (A note for anyone correlating these two logs:
cliproxy timestamps in **UTC+8** while this host runs UTC+7. A first pass that
read the log as UTC paired the wrong arms and appeared to show dispatches
outliving their runs. It does not.)

**What is false.** The meter moved anyway, twice, on one account, in empty
windows, with settled baselines:

| arm | turns | dispatches | aborted chars | cuts | usage events | meter |
| --- | --- | --- | --- | --- | --- | --- |
| G-B relay (first pass) | 12 | 20 | 2,753,832 | marker 10 / toolcall 2 | **0** | +2 (15-minute settle, split across two accounts, one unreadable) |
| H4 relay-owner | 12 | 20 | 2,753,424 | marker 8 / toolcall 4 | **0** | **+5** |
| H5 relay-owner, settled baseline | 12 | 19 | 2,616,646 | marker 7 / toolcall 5 | **0** | **+4 at the first read (5 min after the last dispatch)**, unchanged at the second (20 min) |
| arm G-A, the same 12 turns direct | 12 | 12 | — | — | 12 | **+8** |

So a relayed turn of this body costs the meter **0.25–0.42 points** (0.33–0.42 in
H4/H5, 0.25 in the 2026-10-05 warm arm O6; all of them 1.6 dispatches a turn, i.e.
**0.158–0.21 points per dispatch**) against the direct path's 0.67, and the saving
is **~1.6–2×**, not ≥4× and not 6×–45×. H5's
window had no scheduled abort-only arm in it (that cron fired on odd hours) and,
per cliproxy's log, no other `/v1/responses` traffic at all, so its +4 is its own.
H4's window is one point weaker: the scheduled abort-only arm also fired inside it
(01:05:55Z, 4 runs, 8 dispatches of ~2.8k characters each), so up to
about one of H4's five points could belong to that instead — which is exactly why
H5 exists, and why H5's settled baseline is the arm to quote.
The provider does not have to finalize a request to bill for it: it can charge for
the cached prefix it had already read when the connection dropped, and that
charge never appears as a usage event (priced 2026-10-05: the cache reads, not
the output). **"No event" is
not "no charge"**, and this repository's accounting rested on treating them as the
same thing.

**What the difference is: the charge follows the cached prefix (measured
2026-10-05).** The filler arms charge far less by the same method: C-scale-30 — 30
relayed turns, 144 dispatches, 4,274,417 aborted tokens — moved the pool by +2.
The two bodies differ in a way that turns out to be measurable. The filler is
60,000 characters of unique random words and carries no `prompt_cache_key`; the
owner's body is a Codex conversation of 136,577 characters that does carry one,
and the provider caches it. Seven replays of that body straight at cliproxy (no
relay, `prefix_cache_2026-10-05`): the first after ten idle minutes reported
`cached_tokens: 0`, the same body again immediately reported **27,776 of 30,487
input tokens (91%) read from cache**, and a 16-character nonce in the first
characters of the request drove `cached_tokens` back to 0 in two consecutive
replays with two different nonces.

If the aborted charge is the *cache-read term of the same fitted model* — the
provider bills the cached prefix it read, and nothing else, when the stream is cut
— then a dispatch of this body is ~22,800 tokens at the document's 5.99
characters per token (~28,300 at the provider's own count on the finalized
counterpart of the same body, which is 4.82 characters per token: see the tension
noted below), ~90% of them cache reads, i.e. `0.18–0.24` points, and H5's 19
dispatches predict **3.5–4.6 points against the +4 observed**. The filler's zero
cache reads predict ~0 against
+2 over 144 dispatches. Both readings come from the same model rather than a
second one, and this prices the continuation that the earlier revision could only
bound: on this body the second dispatch carries most of the delivered text — 956 of
O6's 1,237 characters, 77% — and it re-sends the client's own prefix: 137,295 of a
two-segment run's 275,217 upstream characters are that second dispatch.

That reading was tested rather than argued (`continuation_2026-10-05`): four arms
of the same body, 12 turns each, on the relay's operator path — whose run records
match the client path's shape, 1.6–1.7 dispatches and 95–106 delivered characters
per turn — at concurrency 2 so the provider does not throttle, in an empty 5h
window, with a meter and oracle read before and after every arm and a 25-minute
settle between them:

| arm | what changes | turns | dispatches | delivered/turn | 5h meter | points/turn |
| --- | --- | --- | --- | --- | --- | --- |
| O6 | nothing — the production knobs | 12 | 19 | 103.1 | **+3** | 0.25 |
| O6c | a unique 16-character nonce in the first tool description of every request, so no dispatch can be served from cache | 12 | 14 | 152.8 | **0** | 0.00 |
| O6c2 | the same, with the nonce prepended as an inert `developer` message instead — the behaviour-matched cold arm | 12 | 22 | 143.6 | **+1** | 0.083 |
| O1 | `max_segments: 1` — no continuation | 12 | 12 | 44.2 | **+2** | 0.167 |

The result is a **price per dispatch, not per turn**. The warm arm charged +3
points over 12 turns and 19 dispatches (0.25 a turn); the `max_segments: 1` arm +2
over 12 dispatches; the two cache-cold arms 0 and +1 over 14 and 22 dispatches. Per
dispatch that is **0.158–0.167 warm** — and 0.21 for H5, the same body and the same
19 dispatches on the client path — against **0.00–0.045 cold**. The fit predicts
0.18–0.24 for this body, and the measured price sits at the bottom of that band. The
band is honest about a tension this report does not resolve: the same 136,577-character
body is ~22,800 tokens at the 5.99 characters per token this document uses elsewhere,
but the provider counted **28,333** input tokens on the finalized counterpart of that
body (4.82 characters per token), and at that count the prediction is 0.24 — above
every warm arm. Either the aborted dispatch's billable prefix is smaller than the
finalized request's or its cache share is lower, and nothing here separates those.
The `max_segments: 1` arm is the consistency check that makes the unit the right
one: it charged the same per dispatch as the warm arm (0.167 against 0.158) while
making seven fewer of them, and its turn cost fell by exactly those seven
dispatches (0.25 to 0.167 a turn).

The cold arms are not perfect behavioural twins, and the second is the one to
quote. The first put its nonce in item 0's first tool description, which also
changed the text the model read: 10 of its 12 turns answered in segment 0 instead
of calling the padding tool (against 7 of 12 continuing in the warm arm). The
second prepends an **inert** `developer` message instead — no client item and no
tool changes, and the marker adds ~25 characters to `prompt_chars`, so the run log
shows it ran in all 12 turns. It then behaved much like the warm arm (10 of 12
turns cut `toolpad`+`marker`, 22 dispatches against 19, 143 delivered characters
a turn against 103) and still charged **one point over those 22 dispatches** — the
meter's own floor, i.e. at most 0.045 a dispatch. That arm's bracket also contains
the scheduled four-run arm (07:05Z, eight dispatches of a ~250-character prompt),
which is why the one point is quoted as a ceiling rather than as the cold
price; the clean cold reading is the first arm's **0 over 14 dispatches**, whose
bracket holds nothing but itself. No reading of these four arms has the cold prefix
costing anything like the warm one.

**What can be improved, and what it costs.** The continuation is not the lever.
`max_segments: 1` saves 0.08 points a turn — exactly the seven dispatches it
removes, at the same per-dispatch price — and costs more than half the delivered
characters (103 to 44 a turn), with 8 of its 12 turns delivering nothing a client
can read at all, because on those the run ends at a `toolpad` cut whose padding
call is withheld from the client. The lever is the prefix: a dispatch the provider cannot match is charged at
nothing measurable. On this body that would take a relayed turn from 0.25–0.42
points to at most 0.08, and the day's aborted side from ~56–75 points to **at most
~16** (357 dispatches at the cold bound).

The provider pays for it. Every dispatch would be processed from scratch instead of
read from cache, and the arms show what that costs in time: the behaviour-matched
cold arm's median request took **93.7s against the warm arm's 48.5s**, for less
than twice the delivered text — and since it *did* deliver more, part of that gap
is generation rather than cache. It is also a change in what the relay sends, not a
policy knob: `build_body`'s own docstring says the relay only *appends* to the
client's request and never rebuilds it, and a cache-busting marker has to precede
the client's first tokens. This report records what that would buy and what it
costs; whether to ship it is the operator's decision, and it is not part of this
change.

**The same pair on a client's own command (2026-10-05).** Every arm above drives
the relay's own path with a body the harness chose. The command a client runs is
one `codex exec` agentic turn, and on that the cut is cheaper by less, and the
cheaper arm is the one that stops early:

| arm | wall | `/v1/responses` dispatches | cuts | 5h meter | weekly | new file |
| --- | --- | --- | --- | --- | --- | --- |
| through the relay (`model_provider=relay`) | 48.2s | 3 | `toolcall` x2, `imagegen` x1 | **+3** (24 -> 27, unchanged at 5 and 15 min) | +0 | none |
| direct on cliproxy | 179.4s | 6 | none (finalized) | **+9** (15 -> 24, unchanged at 5, 15 and 30 min) | +2 | `pelican-bicycle.svg` |

One 5h window, the same prompt, directory, model and `model_reasoning_effort =
xhigh`, each arm's baseline settled for at least four minutes and its delta
re-read three times. Where each number lives: both deltas are in
`billing_reads.jsonl` (labels `direct-pelican-*` and `relay2-pelican-*`), the
relayed arm's dispatches and cut names are in the relay's own run log, and the
direct arm's 6 dispatches are counted from cliproxy's `logs/main.log` — a
git-ignored file that holds account addresses and cannot be committed, so that
one number is re-derivable only on this host. The cut is ~3x cheaper here, and
the records separate the two ingredients: the relayed run made half the
dispatches (3 against 6) **and** a cut
dispatch is priced below a finalized one. The second effect is the one the fit
above predicts, but not at the size it predicts — the relayed run's 3 dispatches
cost 1.0 point each, where the fit's cached-prefix price for a body this size is
0.18-0.24, because the cut lands *after* the model has generated its reasoning and
its whole tool call: the relayed run's upstream items are `reasoning` x2 and
`custom_tool_call:exec` x2 before the image item it dies on. What the cut saves is
the part of the turn the provider had not finalized when the socket closed, and at
`xhigh` on a tool-calling turn there is little of it.

Which is also why the pair is not like-for-like, and the third column is where
that shows: the relayed run ends at the frame that names an image item
(`imagegen_cut`, the published default), so it stops where the direct run was
still working, and its directory is unchanged afterwards. Part of what the meter
did not charge for is work the client never received. A command whose artifact has
to land belongs on `imagegen_cut: false`, priced like the direct arm, not like this
one.

**What follows from it.**

- The oracle is a token record, not a bill. Its silence bounds nothing about cost,
  which is the opposite of what `usage-oracle.md` and `relay-8371.md` were written
  to say; both are corrected.
- The relay's policy levers do **not** buy much: a turn's cost is its dispatch
  count times the price of the cached prefix it re-sends, so `max_segments: 1`
  removes seven dispatches out of nineteen and takes more than half the delivered
  answer with them (measured 2026-10-05). What *would* buy something is making the
  dispatch's prefix uncacheable, which is a change to what the relay sends rather
  than a knob — measured, priced above, and left to the operator.
- **Nothing watches the leak automatically.** The arms above price it, and a run
  record carries what a reader needs to find it — `real_completed`, `seg_chars`,
  and `usage` when the provider emitted one — but nothing scans for it on a
  schedule and nothing alerts, and the fit that priced a window of leaked runs is
  not in this tree.

## What this does not say

- The meter is an integer percent. A charge that posts late, or below one point,
  is invisible to every number above; the aborted rate could be lower than the
  lowest rate printed here and this method could not tell. The pairwise arms are
  the strongest shape available for that reason: identical bodies and a
  same-window counterpart, so the meter's floor is the only resolution limit and
  it applies equally to both sides.
- **The two aborted-rate arms' ~8× disagreement** (arms P3-scale and C-scale, the
  same construction at 12 and 30 requests) is what the 2026-10-05 arms explain:
  both used cache-cold filler, so both were measuring a charge near zero — a prefix
  the provider has nothing cached for — and the difference between them is spread
  around that floor rather than two different rates. The per-dispatch price on a
  *cached* prefix is measured separately and is stable across shapes (0.158–0.21
  points).
- The aborted charge follows the **cache-read** tokens of the dispatch. Fourteen
  cache-cold dispatches of the owner's body carried ~301,000 uncached input tokens;
  charged at the fitted uncached rate that would be 28.6 points, and the arm
  measured **0** — so an aborted dispatch's uncached input is charged at under
  ~1/30 of the finalized rate, while its cached prefix is charged at the finalized
  cache-read rate (one dispatch of this body, ~90% cached, costs 0.18 by the fit
  and 0.158–0.21 measured). The *finalized* split is measured too (cache read
  ~0.10×, output ~5.1×, RMS 0.80 points over nine windows, per model).
- The spend-model fit has one model (`gpt-6-astra`), one plan (plus), three
  accounts and nine windows; a per-request term is not separable from its three
  coefficients at a 1-point resolution, and the provider's own definition of what
  the percent measures is still not observable — the coefficients are a fit to
  observed movement, not a price list. Its current status is the fitted values
  above plus the controlled single-account campaign (`campaign_2026-10-05`) and
  the nine windows it added.
- **Which models the percent counts is a separate question, and it is not
  settled.** `gpt-6-luna` traffic never moves it (25 requests, 1,195,240 input
  tokens, above). Whether `gpt-5.6-terra` is counted is open, and it matters
  because the owner's own CLI mixes models: his direct session of 16 requests —
  6 `gpt-6-astra`, 8 `gpt-5.6-terra`, 2 `gpt-5.6-luna`, 446,947 input tokens of
  which 374,784 were cache reads and 72,163 uncached, plus 8,089 output — is
  worth **15.4 points** if every model counts and **6.10** if only the astra
  events do. That session was recorded as **+7** when it ran, which is the
  astra-only reading; but it came from a single read rather than a bracketed
  pair, so it is weaker evidence than any window in the fit. A terra arm is
  running to settle it. If the percent is astra-only, then every point figure in
  this document is an astra-traffic figure and the total spend of an account is
  larger than the meter shows.
- **"The cut removes the charge" was an inference from the absence of a usage
  event, and it is now refuted for this workload.** Arm B's eight aborted runs wrote
  no event while arm A's four finalized runs wrote four, into the same collector,
  minutes apart — the absence is real and precisely measured. The charge is also
  real: arms H4 and H5, the same shape with settled baselines, moved the meter by
  +5 and +4. The provider bills the cached prefix it read before the connection
  dropped, and it does not have to send a usage block to do it. The provider's own
  accounting is still not observable; what is observable is the meter, and the
  meter moves.
- The conservative aborted rate rests on the 12-request arm, whose window contained
  another client's 18,863 finalized input tokens, worth ~2 points, which were
  subtracted; that subtraction is the weakest step in that reading and is named
  rather than buried. With the cache measurement in place, neither that arm nor
  the larger one is quoted as a *rate* any more: both were cache-cold and both sit
  near the floor, which is the spread the bullet above describes.
