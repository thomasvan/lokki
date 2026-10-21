# Why relayed requests take longer, and what was done about it

Measured on 2026-10-09 on the live pair (relay `:8381` -> cliproxy `:8318`, the Plus account), model `gpt-6-astra`
at effort `xhigh`, `text_calls` on. Everything here is a small sample (3 to 8 per cell) taken in one afternoon;
the provider's reasoning time is bimodal and varies from probe to probe, so read the numbers as orders of
magnitude. Prompts are the harness's, never a user's.

## What was seen

A round of the websearch-and-translate task (the harness's `wiki` task),
3 sessions per arm: direct to `:8318` 75 s median wall (68.6 to 75.6), through the relay 190.8 s (149.5 to 249.9),
2.54 times as long. The first message came after 5 s direct and 42 s through the relay. The relay used fewer
requests (about 5 a session against about 9: 14 against 28 for the 3 sessions of each arm, the direct count from the usage plugin) and the meter moved less (+6 against +17 points for 3 sessions
each; one order only, relay second, integer meter). So the relay looked cheaper and was slower.

## Where the time goes

Not in the relay's forwarding. The dev relay's trace (`debug_log`, one session, frames in both directions, one-second
timestamps) shows every upstream frame forwarded to the client in the same second, and no gap that the relay
holds. The gaps are in the upstream stream itself: before the first message of every request the provider sends
two or three **reasoning items**, each open for about 15 s (3 to 18 s, 18 to 33 s, 33 s: the message), and
those 30 to 45 s are the delay. Sent straight to the provider, the relay's own request body behaves the same:

Seconds until the first message or tool call, medians of 3 to 6 probes:

| request | the client's body | the relay's body |
| --- | --- | --- |
| first step (`R1`) | 2 | 46 to 50 (reasoning 40 to 58) |
| second step, after one `exec` output (`R2`) | 11 (then a native call) | 31 |
| the final answer (`R3`) | 15 | 30 to 35 |

So the extra time is reasoning the model does about what the relay appended to the request.

## What causes it

One variable at a time, on the body the relay sent for the first request (probes read until the first message
item or tool call, then closed):

- the padding tool alone, with no instruction: no reasoning at `R1`, +2 s at `R2`, +11 s at `R3`;
- the instruction alone, with no padding tool: 26 to 33 s at `R1`;
- the **call-format paragraph** ("A. If the reply needs the `exec` tool, do not call it through the tool
  interface. Write instead: at most two sentences ... one block at most ... nothing follows it") is the trigger at
  `R1` and `R2`: the instruction without it (the sections only) is back to 2 s at `R1`, and the paragraph alone, with
  no sections, costs 32 to 45 s;
- a model asked to leave its habit of calling a tool thinks for a long while before it writes the call. When the
  paragraph is short and does **not** forbid the tool interface, the model keeps calling the tool: three sessions
  through the dev relay ended all 17 step requests in a native call (`cut: toolcall`) and none in a written one
  (`textcall`), and the wall time was short (60 to 220 s), because the call was native. The prohibition is what
  makes it write the call, and what makes it think;
- the effort tends to scale it: the relay's `R1`/`R2`/`R3` against the client's body, in seconds, at `xhigh`
  46/31/30 against 2/11/15, at `high` 14/18/14 against 3/5/9, at `medium` 8/18/9 against 2/3/5;
- wording edits inside the sections (dropping the appendix, the "say nothing about them" paragraph, a shorter
  requirement 4) changed `R2` and `R3` by less than the spread (27 to 33 s);
- `tool_choice: "none"` ends the response with no output at all, and the `instructions` field instead of a
  message did not help (31 s).

## What was changed

`lokki_core/renamer.py`: the call-format paragraph is shorter and keeps the prohibition, and it is sent as a
**developer** message instead of a user message (only that one instruction; the other shapes keep the user role
they were measured in). Straight to the provider, medians of 4: `R1` 23 s against 50 s, `R2` 16 s against 31 s,
and in all 8 probes the model wrote the call as text. The final answer's request is the same as before (30 to 35 s).
As a user message the shorter paragraph, with the prohibition, was no better than the old one (48 to 76 s at
`R1`, 3 of 3), so the role and the wording together are the change. Developer messages as the other
instructions' role, and a lower effort for relayed requests, were not tried as defaults.

## End to end, at effort `high`

After the probes the account was replaced (the 5h meter was at 100%), and whole sessions of the same task were run on a fresh
one at effort `high`, `gpt-6-astra`, 3 sessions a block, one after the other, the meter settled before and after each block
(poll 15 s, unchanged for 60 s, 60 s after the last session; no late charge was looked for). `new` is the dev relay on this
change, `old` the live relay on the code before it.

| block | wall per session, s | first message, s | requests (written calls, marker) | meter |
| --- | --- | --- | --- | --- |
| direct to `:8318` | 67, 41, 31 | 3.7 to 6.1 | | +17 points |
| relay, `new` | 97, 103, 70 | 11 to 19 | 20 (17, 3) | +10 |
| relay, `old` | 110, 131, 120 | 17 to 21 | 16 (13, 3) | +3 |
| relay, `old`, again | 94, 116, 160 | 17 to 23 | 20 (17, 3) | +11 |

- **Compliance.** The model wrote the call as text in 17 of 17 step requests with the new wording, as it did with the old, and
  every session ran 3 to 6 commands and produced an answer. A string `input` (the `/api/run` path) has no message to give a role
  to and gets the shorter text appended as it is; that path was not measured and has no speed-up shown.
- **Time.** Medians over the sessions: `new` 97 s, `old` 118 s (6 sessions), direct 41 s. The ranges of `new` (70 to 103) and of
  the first `old` block (110 to 131) do not overlap; the second `old` block (94 to 160) overlaps `new`. Step requests took
  3 to 18 s with `new` and 8 to 26 s with `old`. At `xhigh` the probes above give a larger difference (first step 23 s against 50 s).
  Through the relay the sessions are still 2.4 times as long as direct at `high`.
- **Quota.** Not decided. The same code, `old`, moved the meter +3 and +11 in two blocks of 3; `new` +10, in the first relay block
  after the direct one. Blocks this size are within the noise of the number of steps a session takes (3 to 9 requests), and of the integer
  meter (each end is +-1). Readings between sessions are not settled, so a charge cannot be given to a session. The `new` block also
  ran before both `old` blocks, which share the whole prefix of its requests up to the last message, so it alone may have met a cold
  prefix; that is a possible part of its difference, not shown. Direct cost +17 for the same 3 sessions, as it did at `xhigh`. No claim is made that `new` costs more or
  less than `old`; the reasoning it removes is output the account is charged for, so the expectation is less.
- **Answer length.** Final answers through the relay were longer than direct ones (1.4 to 5.2 thousand characters against 0.6 to
  1.1), which is part of their time. Dropping "complete" from requirement 1 did not change what the model wrote before the marker in
  three probes each (about 1,000 characters either way, 12 to 14 s to the marker at `high`), so the wording is not the cause; it
  follows the page the model happens to pick.

## Is `text_calls: false` the way to be faster? No: it is not shown to be cheaper than direct

The model calls the tool the usual way and the relay cuts at the finished call (`toolcall`), so the reasoning about the call format
is gone. Two blocks of 3 sessions at effort `high` on a dev relay with `text_calls` off (the code of the merged change, the live relay's
settings but that one), same task, same account, run after the blocks above. Each block's meter is the difference of two integer
reads, good to about +-1 at each end, so +-2 for a block; the blocks are few and their spread is large:

| block | wall per session, s | first message, s | requests (calls, marker) | meter |
| --- | --- | --- | --- | --- |
| off, 1 | 75, 63, 60 | 4.1 to 5.1 | 21 (18, 3) | +15 points |
| off, 2 | 84, 63, 62 | 3.9 to 5.2 | 28 (25, 3) | +26 |

- **Time.** Median 63 s (60 to 84), against 97 s with `text_calls` on (the new wording, the 3-session block above; the old wording's
  median was 118 s) and 41 s direct. Step requests took 3 to 8 s.
- **Quota.** +41 points for 6 sessions, 6.8 a session, against 2.7 a session with it on (9 sessions at `high`, both wordings) and 5.7 direct (the
  one direct block at `high` and the one at `xhigh`, +17 each). The two blocks differ by a factor of 1.7 (+15 and +26). The block
  ranges do not overlap those of `text_calls` on (1.0 to 3.7 a session against 5.0 to 8.7), (only just: the meter's error is +-0.7 a session) and the sum is more than twice what `text_calls` on cost. Against direct they do overlap: +41 against +34 over 6 sessions is inside the meter's error, so the data do not separate off
  from direct. The relay made about 8 requests a session with it off, where direct made about 9 (the round
  of "What was seen", counted by the usage plugin: 28 for 3 sessions) and `text_calls` on made 5 to 7 (a block's average).
  Here the ratio is 2.7 against 6.8, about 0.4.
- **So** on this task, and in this order (off after on), the saving of the relay comes from `text_calls`. Turned off, the sessions are about a third faster than on (against the new wording) and
  cost about two and a half times as much, which, set against direct, shows no saving. The speed has to come from somewhere else.
- **Not run.** The same pair of blocks at `xhigh` (off, and on with the new wording): the 5h meter stood at 83% after the second block
  (the stop is checked before each session, and that block cost +26), above the 78% the run was told to stay under, and the runner
  skipped them. The window resets at 16:50Z.

## Quota: a lever that was tried and cost more (one block)

`cost-of-a-cut.md` names one lever for the cost of a cut request: a dispatch the provider cannot match in its prompt cache was
charged at nothing measurable in the 2026-10-05 arms O6c/O6c2 (0 over 14 dispatches, and +1 over 22 which that page quotes as a
ceiling, against 0.16 to 0.21 a dispatch warm). It was tried on today's task: a dev relay that puts a fresh random nonce, as an inert
developer message, in front of every request's input, so that no dispatch can be served from cache; `text_calls` on, effort `high`,
3 sessions.

| block | wall per session, s | requests (written calls, marker) | meter |
| --- | --- | --- | --- |
| nonce in front of every request | 83, 81, 89 | 24 (21, 3) | **+22 points** (7.3 a session) |
| for comparison, cache untouched (above) | 97, 103, 70 and 110 to 160 | 16 to 20 a block | +3 to +11 a block (2.7 a session over 9) |
| direct | 67, 41, 31 | | +17 (5.7 a session) |

- The relay behaved as before (every step request still ended at the written call, then the marker) and the sessions were not
  slower; step requests took 3 to 14 s, most 3 to 5 s. In the 2026-10-05 arms the cold arm's median request took 93.7 s against
  48.5 s warm, so that part did not repeat either.
- The meter moved about 2.7 times as much as with the cache alone, and more than direct. Part of that is the number of requests:
  8 a session here against 5.3 to 6.7 in the cache-alone blocks. Per request it is 22/24 = 0.92 points against 24/56 = 0.43, about
  2.1 times. Each block is good to +-2 points.
- This agrees with a cut request that misses the cache being charged for what it read uncached, and then costing more than one
  that hits; it does not show it. It disagrees with the 2026-10-05 arms, and nothing here says why: the cause is not known. What
  may differ between the two, none of it checked, is the body, the state of the cache and the task. The
  2026-10-05 result did not repeat here.
- The account behind the meter appears to have changed since the blocks above: the weekly reading (46%) is the one the first account
  showed earlier, which this page does not otherwise record. Both accounts moved +17 for 3 direct sessions (the `xhigh` round and
  the `high` block above, different efforts), so the scale looks the same, but this is one block of 3 and the comparison crosses an account change.
  The code of the experiment was not merged.
- What is left that moves the number, none of it tested here: the model, since the meter is a spend meter with a price per model
  (`gpt-6-astra` 1, `gpt-5.6-terra` 0.21, `gpt-6-luna` below 0.04, `cost-of-a-cut.md`), and the size of the context, since each
  dispatch would pay to read the whole history again: about 0.4 points a request here (24 points over the 56 requests of the 9
  sessions above, bodies not measured here) against 0.16 to 0.21 a dispatch on about 23,000 tokens in the 2026-10-05
  arms, on a different task (the model and effort of those arms are not recorded on that page). Both are the client's.

## The padding tool off: `text_calls_pad: false` (`RELAY_TEXT_CALLS_PAD=0`)

The probes above put the padding tool and requirement 4 at 10 to 15 s of the final answer's reasoning at `xhigh`. The setting
removes them: with `text_calls` on, the request carries no padding tool and the
second reply shape asks for the answer and the marker alone (`TEXT_CALL_LITE_INSTRUCTIONS`). What it gives up is the abort window
for an answer that has no marker (3 of the 48 `text_calls` requests the live relay recorded up to 2026-10-09 10:39Z used it): such an
answer is finished by the provider and billed. The default is `true` (the request as it was); a file key and an environment variable
switch it, and the environment wins (the page on the Docker layout says what that does to `SIGHUP`).

Measured on whole sessions, 3 a block, the same task as above and on the account of the cache-busting block above (this page
records two account changes), on a dev relay started with `RELAY_TEXT_CALLS_PAD=0`
(`/version` showed `text_calls_pad: false`), against the padded relay:

| effort | padding | wall per session, s | first message, s | requests | meter |
| --- | --- | --- | --- | --- | --- |
| `high` | off | 70, 118, 37 | 7.6 to 9.4 | 23 (20 written calls, 3 marker) | +12 (4.0 a session) |
| `high` | on, the new wording (above) | 97, 103, 70 | 11 to 19 | 20 | +10 (3.3 a session) |
| `xhigh` | on (the live relay, the code of PR #36) | 141, 138, 246 | 24 to 32 | 23 (19 calls, 3 marker, 1 upstream error) | +13 (4.3 a session) |
| `xhigh` | off | 116, 135, 139 | 18 to 36 | 26 (23 calls, 3 marker) | +19 (6.3 a session) |

- **Time.** Medians 70 s against 97 s at `high` and 135 s against 141 s at `xhigh`, with ranges that overlap (37 to 118 against 70 to
  103; 116 to 139 against 138 to 246). The gain the probes suggested did not show in sessions of this size. The final answer's
  request took 13, 30 and 61 s with the padding off at `xhigh` against 40, 61 and 119 s with it on, but the answers were not the same
  length (1.2 to 2.5 thousand characters against 1.4 to 7.4) and 3 sessions cannot separate the two.
- **Quota.** Not cheaper: +12 against +10 at `high` and +19 against +13 at `xhigh`, which is more with the padding off in both,
  inside the error of blocks this size (+-2) at `high` and outside it at `xhigh`. The off sessions made 8.2 requests on average
  against 6.7 and 7.7.
- **So** the setting is available and the default stays on: no gain in time or quota was shown, and it loses the second abort
  window. It is a switch for anyone who wants to try it on a different workload; the 5h meter stood at 72% after these blocks and no
  more were run.

## The wording of the padding tool and the appendix

The first wording called the filler after the marker "implementation notes" and the padding tool an "internal telemetry
sink", words that say what the text is not. The request now says what it is: the filler is discarded, the program that reads
the reply ends the request at the marker, and the filler only keeps the request open until then (`core/lokki_core/renamer.py`;
the tool is `discarded_placeholder`). Whether a model still follows the format was measured on 2026-10-10 against dev relays
running the old wording and the new one (**v2**: the filler is discarded, and why). A first candidate, **v1**, said only that the filler is
discarded; it was run in the first screens and not carried further. The cut kind is the relay's run record; "billed" is a request that
reached the provider's `response.completed`; times are the relay's own `elapsed_s`. Two builds of the new wording ran: the first screens,
the first tool screen and Codex blocks B1 and B2 ran with the filler described as discarded on the client's tools too; the 36-answer text
screen, the second tool screen and Codex block F1 ran the final code, where the property on a client's tool claims nothing. The text sent
to the model is otherwise the same.

| screen | old wording | new wording (v2) |
| --- | --- | --- |
| text answers, `text_calls` off, `medium`: 56 each, the same prompts in rotated order (50 on `gpt-5.6-terra`: two smoke requests, a screen of 12 and a screen of 36; 6 on `gpt-6-astra`) | first request cut at the marker 38, inside the padding call 17 (a second request then writes the answer), at the provider's end of response 1 (billed in full); 73 provider requests; median 21.7 s | marker 55, end of response 1 (billed in full); 56 provider requests; median 15.2 s |
| a client function tool, `text_calls` off, `gpt-5.6-terra`, `medium`: 12 each | cut at the call 12, billed 0; medians 25.0 and 25.8 s | cut at the call 12, billed 0; medians 23.7 and 26.5 s |
| the websearch task through Codex, `text_calls` on, `gpt-6-astra`, `high`, blocks of 3 sessions in the order old, v2, v2, old, then one block of v2 on the final code | 6 sessions, 38 requests (written call 32, marker 6), billed 0; median 62 s; meter +23 (3.8 a session) | 9 sessions, 64 requests (written call 55, marker 9), billed 0; median 56 s; meter +27 (3.0 a session) |

- **The format is followed as well as before.** One text answer in 56 reached the provider's end of response with each wording, in
  both cases a request that no cut reached before the provider's end; none of the 12 tool requests and none of the 9 Codex sessions did. v1 had none in its 20 text
  answers.
- **The new wording stops the model calling the padding tool before it answers.** With the old, 17 of 56 text answers began with
  that call, so the cut fell inside it and a second request wrote the answer (the failure the order of the two requirements in
  `renamer.py` is meant to prevent). With v2 none did, so an answer took one provider request and not 1.3, and its median time 15.2 s and
  not 21.7 s. The arms differ only in the wording, but the failure was seen on one model: all 17 answers were on `gpt-5.6-terra` (5 of 14,
  then 12 of 36), and on `gpt-6-astra` neither wording showed it (0 of 6 each). One kind of prompt.
- **Through Codex no difference in time or quota is shown.** The medians are 62 and 56 s and the meter +23 over 6 sessions and +27 over 9.
  The meter is an integer, the sessions differ in how many steps the task took (6.3 and 7.1 requests a session; the task picks a random
  page), and the blocks differ more than the arms do (+14 and +9 for the old wording, +11, +13 and +3 for the new), so nothing is shown
  either way. The honest wording costs nothing that was measured; it is not shown to save through Codex.
- **The answers do not talk about it.** Of 126 text answers outside the smoke requests, 22 contain a word the check looked for
  (appendix, marker, filler, discard, placeholder, relay, telemetry) and every one is the topic: a hash table's "deleted" markers, UDP
  "telemetry", "discard the rinse water" in a coffee guide.
- **Not covered.** One account and one afternoon; `gpt-5.6-terra` and `gpt-6-astra` only; the Codex task is one and v1 was not run
  through it. A model that weighs "discarded" differently may write less filler; the cut still falls at the marker, and a request no cut
  reaches is billed, as the 1 in 56 shows for both wordings.

## Not shown, not done

- **The padding tool and requirement 4** are about 10 to 15 s of the final answer's reasoning. They are also the
  abort window for an answer without the marker (3 of the 48 requests the live relay recorded with `text_calls` on, up to 2026-10-09 10:39Z, used it), so they stay by default; `text_calls_pad: false` (above) takes them out.
- **Effort.** `xhigh` is the client's choice and the relay does not change it. At `high` two of the three requests take less
  than half the time (`R2` is 18 s against 31 s). Choosing `high` in the client for relayed sessions is the cheapest way to be faster today.
- **What stays unshown.** The trace and the probes show that the 30 to 45 s are in the provider's stream; they cannot show a delay
  of under a second in the relay. At `high`, after the change, a session is still 2.4 times as long as direct; the padding tool,
  requirement 4 and the longer answers are the likely parts, which was not isolated.
- **Variance.** The first-step probe of the shortened paragraph without the prohibition was fast in 4 of 4, then 4 of
  6, then 1 of 6; when the check was changed to read on until the `<<<END>>>` line, the fast ones were native `exec`
  calls (3 of 3), not written ones. A probe that is fast is not a probe that complied.

## How this was measured, and what it cost

`probe`-style requests: the body of a traced relay request with one thing changed, posted straight to `:8318`,
read until the first message item, text delta or tool-call item, then closed. A closed request is **not** free:
the reasoning it generated is charged, and charges post late. About 300 of them, with a few sessions, took the
account's 5h meter from 36% to 100% in 45 minutes (weekly 35% to 46%), and the owner could not use the account
until the window reset. Budget a campaign of probes in advance, and read the meter while it runs.

The wording screens and the five Codex blocks of 2026-10-10 (above) took one account's 5h meter from 0% to 52% (the first four blocks, 01:27Z
to 01:49Z, to 47%) and its weekly meter from 58% to 66%; the meter was read by hand between screens. The 36 requests of the first
text screen (the cheap model) did not move the 5h meter, which read 2% before and after them.
