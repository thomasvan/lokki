# The image routes: `/v1/images/generations` and `/v1/images/edits`

The relay passes the cliproxy's two image routes through. This page is what the routes do and what they leave alone. The code is
`core/lokki_core/images.py`; the tests are `core/test_images.py` (a fake cliproxy behind a serving relay, no quota).

## What it is for, and what it is not

It is for a caller that asks for an image itself: a script, the OpenAI SDK
(`OPENAI_BASE_URL=http://127.0.0.1:8381/v1`, `OPENAI_API_KEY` the relay's client token: the relay
reads `Authorization: Bearer` as well as `X-Relay-Token`), `curl`. It is **not** what makes a Codex
session draw an image. Codex 0.161.0 in this setup offers the model no image tool (the tool list is
`exec`, `wait`, `request_user_input*`, `clock.sleep`, `collaboration.*`), so a
"generate an image" prompt gets no picture through the relay or straight to the cliproxy; and the
provider-side `image_generation_call` item that can still arrive on `/v1/responses` is handled by
`imagegen_cut` and `payload_cap` (see "Images in a Responses session" below, and `relay-8371.md`).

## What the relay adds, and what it does not

The request goes up as it came (JSON, or multipart for an edit) and the answer comes back as it came
(a JSON document, or a server-sent stream when the request asked for `stream`). In front of that:

| | |
| --- | --- |
| token gate | as on every route: no token or a wrong one is a 401 before the body is read and before the cliproxy is reached |
| credential | the relay's own cliproxy key goes upstream; the caller's `Authorization` is not forwarded |
| drain | a relay that is stopping answers 503 with `Retry-After: 5`; an image being generated counts as a run in flight, but takes 28 to 38 s, longer than the 15 s drain, so a restart during one cuts it |
| slots and pacing | one of `MAX_CONCURRENT_IMAGES` (2) slots of their own for the whole call, a third concurrent image waits for one; they are not the text route's `max_concurrent_upstream` slots, so images cannot queue Codex traffic behind them; the `pacing_seconds` gap is the text route's, shared |
| caps | a request body up to 32 MiB (`MAX_IMAGE_BODY_BYTES`; the other routes keep 8 MiB), an answer up to 128 MiB (`MAX_IMAGE_RESPONSE_BYTES`), 600 s for the cliproxy to answer or to send its next bytes (`IMAGE_TIMEOUT`); built in, no `config.yaml` key; a chunked upload counts chunks as the other routes do (16,384 at most), so a client that streams its file should send chunks of 2 KiB or more |
| run record | one per call, in the same hash chain as the text runs |

What it does not do, deliberately:

- **No retry.** The text route retries a 429, 502 or 503 that arrived before any output, because then
  nothing was done. A gateway error on an image request may follow an image that was generated and
  billed, and a second attempt would bill it again. The status goes back to the caller.
- **No cut.** There is nothing to end early; an image is billed when it is generated.
- **No rewriting, no redirect.** An upstream refusal (400, 429, 503...) goes back with its status, its body and
  its `Retry-After`. An upstream that cannot be reached, or whose answer breaks off (a body shorter than its
  `Content-Length`, a chunk that stops) or is over the cap, is a 502 that names no address; so is a redirect,
  which is not followed (the request carries the relay's credential). A stream that fails after its headers, in
  the middle of a chunk, ends with an `event: error`, since a status is no longer possible. A stream the cliproxy
  ends cleanly, or cuts at a line, looks finished: nothing on the connection says otherwise.
- **A body that is not valid JSON, an unknown model, a missing prompt** are the cliproxy's to refuse,
  and it does (400, `invalid_request_error`); the relay validates none of it.

## The run record

`route: "images"`, `path`, `client` (the token's name), `request_kind` (`json`, `multipart`, `other`),
`request_bytes`, then what the request says about itself when it has the shape of a setting: `model`,
`n`, `size`, `quality`, `stream`, `prompt_chars` (a count), `images_in` (a count). From the answer:
`status` (the cliproxy's; when `error` is set the caller was answered 502), `response_bytes`, `images_out`, `events` (a stream), `usage` (the token counts the cliproxy
reported, whole numbers only, with `usage_source: "provider"`), `error`, `upstream_error` (the error's
`code` and `type` when they are identifiers, never its message, which can quote the prompt),
`client_gone`, `elapsed_s` (it includes the wait for a slot and the pacing gap). The record never holds the
prompt, a revised prompt, an image or a file name. It is written before the caller has its answer (a stream: when
it ends), the order the text route keeps. Two consequences: `client_gone` is set for a stream whose caller leaves or
stops reading, and cannot be for an answer that was recorded before it was sent; and a run log that cannot be
written is a 500 for the caller with the image lost, as it is for a text run. `revision` and `config_fingerprint`
ride on it as on every record.

A harness that reads the run log for text runs and looks for `cut` finds none on these records: filter on
`route`.

## Behaviour of the cliproxy that the relay inherits

As measured on the clone `:8318`:

- The models are `gpt-image-2`, `gpt-image-2.5`, `gpt-image-2.5-flare`, `gpt-image-2.5-sunburst`,
  `gpt-image-1.5` (and `grok-imagine-*` names, which were not tried); any other name is a 400 that lists them.
- `/v1/images/variations` and a GET on either route are 404.
- **`size` is not honoured**: a request for `1024x1024` came back as a 1254x1254 PNG, through the relay and
  straight to the cliproxy alike.
- A streamed generation sends no `partial_image` even when one is asked for (`partial_images: 1`): only keep-alive
  events and the finished image.
- An edit takes the image as multipart (`image`) or as JSON (`images: [{"image_url": "data:..."}]`).
- A 1254x1254 PNG is 2.4 MB, 3.2 million characters of base64; a generation took 28 to 38 s.

## Checking it

The recorded smoke coverage used `core/experiments/images_smoke.py`, removed in
`21f14bc9d33c4966831f6fdac6348ac831008518`: the token gate (401, no record), validation errors matching the cliproxy's, and each
billed call returning a valid PNG whose run record equals the answer and holds no content. A block of four images costs about a
point of the 5h meter at most. Not tested: `quality`, `background`, `output_format`, `moderation` and the other parameters (they
pass through, the cliproxy decides), `n` above 1, an edit with a mask, a `gpt-image-2.5` model, and a caller disconnecting from
the real cliproxy mid-generation (tested against the fake cliproxy for a stream, which is let go and recorded `client_gone`).

## Images from inside Codex

The routes are for callers that ask for an image themselves. They do not make a Codex session draw one: Codex 0.161.0 here offers
the model no image tool, and the model prefers the provider's own tool to anything the relay could offer in its place.

## Images in a Responses session

The provider's own image tool can still be chosen by the model on `/v1/responses`, and its item (`image_generation_call`)
is the relay's to handle. Two settings decide how, both in `config.yaml` (each also an environment
variable, `RELAY_IMAGEGEN_CUT` and `RELAY_PAYLOAD_CAP`; both are re-read on `SIGHUP` for the runs that start after it):

| | `imagegen_cut` | `payload_cap` |
| --- | --- | --- |
| built in | `true`: the run ends at the frame that names the image, before it exists | `65536`: a payload field above this many characters is delivered as a 1x1 PNG |
| to let the image through | `false` | `0` (no cap) |

They are read together: `imagegen_cut: false` alone makes the provider produce the picture and hands the client a
1x1 placeholder. The built-in pair is the saving the relay was built around (the run ends in seconds, no picture is made);
the other pair is "forward it as the provider sends it", which is what a client that asked for a picture needs. A bad
`payload_cap` (not a whole number: a fraction, a boolean, infinity, or negative) and an `imagegen_cut` that is not a clear
true or false (also read: 1/0, yes/no, on/off) are refused, at start-up and on a reload, which keeps the running values: a
typo must not turn the cut off, because the off state is the one that bills a generated image. On `/v1/responses` a caller
can set either only where `allow_client_tuning` is on; `/api/run` takes them from a request with an operator token.

**What a Codex session gets with the second pair:** the relay passes the item whole (the run is not cut, nothing is replaced) and the
provider makes the pictures, but Codex 0.161.0 in `exec` mode writes no picture file, `--ephemeral` or not, and the model ends by
saying it found no usable artifact; in the one session that kept a rollout it then searched the disk for any recent PNG to copy to
the name it was asked for, so a pass-through session can end with a wrong picture, not only with none. A picture stays in the
history of the next request or two (inputs of 3.1 to 3.5 million characters) until Codex compacts the history, and the relay refuses
a request body over 8 MiB with a 413, which three pictures held at once would exceed. For a picture on disk the routes above (a
script, the SDK, `curl`) are the path that works; from inside Codex none was found.
