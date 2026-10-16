# Baselines

This directory holds **ground-truth detection reports** collected by running
`bench.sh` against the official Anthropic API (`https://www.yhshu.ai/`)
with a real `sk-ant-...` key.

Each file `<model>_<mode>.json` is the raw output of `relay-detector detect`
in that mode, against that model, on the official API. These represent what
the detector should see when there is no relay involvement: an unaltered
Claude API response, scored 100/100 when our detectors are healthy.

## Purpose

Treat these as templates for future comparisons. Specific signals worth
diffing against:

- `thinking_signature.signature_length` — expected to be ~600–900 chars on
  Anthropic. A relay returning 0 means thinking was stripped.
- `consistency.output_tokens_seq` — expected coefficient of variation < 0.05
  on Anthropic. Higher CV suggests load-balancing across different upstream
  models.
- `pdf.response_text` — should contain the magic identifier on Anthropic.
- `structured_output.sub_checks.caller.value.<keys>` — currently `["type"]`
  on Anthropic (note: this is a dict, not the string-enum the docs claim —
  Anthropic schema drift discovered during M4 verification).
- `protocol.issues` and `message_id.violations` — empty on Anthropic. Any
  entry on a relay is a real protocol-layer signal.

A relay-station report that diverges materially from the corresponding model
baseline is strong evidence of impersonation, model substitution, or
capability stripping.

## Refreshing

Re-run `./bench.sh` whenever:
- A new Claude snapshot ships (e.g. Haiku 4.5 → 4.6)
- Anthropic API behavior changes (e.g. a field's shape is updated)
- Detector logic changes meaningfully

```bash
OFFICIAL_KEY=sk-ant-...  ./bench.sh
```

After running on the deploy host, copy the output back into this directory:

```bash
scp -r root@<host>:/tmp/baselines/*.json relay-detector/data/baselines/
```

## Files

- `claude-opus-4-7_full.json`   — Opus 4.7, full mode (10/10 detectors)
- `claude-sonnet-4-6_full.json` — Sonnet 4.6, full mode
- `claude-haiku-4-5_full.json`  — Haiku 4.5, full mode
- `claude-opus-4-6_full.json`   — Opus 4.6 (legacy), full mode

(other modes / models can coexist using the same `<model>_<mode>.json` naming)
