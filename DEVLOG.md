# DEVLOG

Newest first. Every entry says what changed, why, and what it broke or touched
elsewhere. Entries are marked ✅ verified (I actually ran it and watched it
work) or ⚠️ unverified (written but not exercised).

---

## 2026-08-20 — The rule pass can now be switched off

**What changed.** `--no-rules` on the CLI and a toggle in the UI skip the
extension/filename table entirely and hand **every** file to the two agents.

| Ticket | Files | Status |
|---|---|---|
| Core (mine) | `manager.py`, `plan.py`, `server.py`, `tests/` (66 → 73) | ✅ |
| TICKET-11 | `cli.py` (`--no-rules`), `README.md` | ✅ |
| TICKET-12 | `static/*` — scan-bar toggle | ✅ |

**Why.** The user noticed the agents barely ran and asked how to involve them.
That was working as designed — rules go first and the agents only see the
residue — but the underlying want was real: the rule table groups by file
*type*, so a folder of 40 PDFs is one undifferentiated `Documents/`, while the
agents group by what files are *about* (`Tenancy`, `Lecture Notes`). Opt-in,
because it is one model call per file instead of only the leftovers.

**Verified ✅**
- `use_rules=True` → `{'rules': 3, 'classifier': 1}`; `use_rules=False` →
  `{'classifier': 4}` with the proposer seeing the whole folder in one call.
- `POST /scan` accepts `use_rules`, rejects non-booleans with a 400.
- **The toggle widens what gets classified, never what gets touched** —
  dotfiles, symlinks and subfolders are still excluded with rules off, and
  apply is still collision-safe and target-confined.
- `used_rules` round-trips through the store, so a plan reopened from history
  shows the mode it was *built* with, not the live toggle state.
- **73/73 tests pass.** Full stack unregressed: 409 gate, collision suffixing,
  `/static` assets.

**Side effects / gotchas**
- **The degenerate case:** rules off + agents unavailable = nothing is
  classified at all, where rules-on would still have placed the easy files.
  The CLI diagnoses this before printing an empty plan; the UI disables the
  scan button rather than letting it run.
- Cost estimates in the UI are quoted **only** from a real prior plan for that
  exact path — no invented file counts, no pricing, no token estimates. Path
  matching is literal, so a symlinked path yields no number rather than a
  wrong one.
- Registering stub delegates *before* importing `cli` silently does nothing:
  importing `cli` pulls in the real agent modules, which re-register over the
  stubs. Register after.
- With rules off, every decision is `[classifier]`; CLI and UI both needed
  copy that no longer assumes a mix of both agents.

---

## 2026-08-20 — Pluggable model providers, and user-facing config

**What changed.** The tool no longer hardcodes a model provider. Which model
answers is now a config value, and the two delegate agents stopped importing an
SDK entirely.

| Ticket | Files | Status |
|---|---|---|
| Core (mine) | `config.py`, `providers/` (3 files), `classifier.py`, `scheme.py`, `server.py` | ✅ |
| TICKET-9 | `cli.py` (`config` cmd), `README.md`, `requirements.txt` | ✅ (agent stalled; I finished it) |
| TICKET-10 | `static/*` — provider display + read-only config surface | ✅ |
| Tests (mine) | `tests/test_organiser.py` — 53 → 66 tests | ✅ |

**Why.** The user plans to use OpenAI. Rather than port the two delegates and
be stuck again next time, the provider became a seam.

**The design.** Layering is now:

    manager → delegates (classifier, scheme) → providers (anthropic, openai, compatible)

Agents are pluggable in the manager; providers are pluggable in the agents.
A provider implements one method — `complete_json(model, system, prompt,
schema, effort, max_tokens)` → validated Pydantic object, or `ProviderError`.
`compatible` is the `openai` SDK pointed at a `base_url`, which covers Ollama,
LM Studio, vLLM and OpenRouter, so the tool can run with nothing leaving the
machine.

Settings resolve **env var → `config.json` → default**, re-read on every call
so edits take effect without a restart, and every value remembers its source so
`cli.py config` and the UI panel can show *why* a run used the model it did.
**API keys are never in the config file** — they stay in `ANTHROPIC_API_KEY` /
`OPENAI_API_KEY`, so the state dir never holds a secret.

**Verified ✅**
- Config precedence, per-agent models, all three providers, broken-config
  errors, and `cli.py config` in every state. **66/66 tests pass.**
- No API key is ever printed, in plain output or `--json`.
- Full stack unregressed: 409 gate, collision suffixing, never-delete,
  left-alone files, `/static` assets.

**Unverified ⚠️** — **no provider has ever completed a real request.** No
credentials for any provider on this machine. The Anthropic path was only ever
exercised through stubs; the OpenAI path was written against the installed
SDK's introspected signatures (`responses.parse(..., text_format=…)` →
`output_parsed`) and **never executed**.

**Side effects / gotchas**
- **Bug found and fixed:** setting `FILE_ORGANISER_PROVIDER=anthropic` while the
  config file said `openai` kept the file's `gpt-5-mini` and would have sent an
  OpenAI model id to Anthropic. Model names in the file are now scoped to the
  provider that file names; an explicit `*_MODEL` env var still wins.
- **Bug found and fixed:** the raw OpenAI SDK "missing credentials" exception
  was leaking into the UI status strip. Now a one-line actionable message.
- **Contract fix** (flagged by the UI agent): the `/health` `provider` block
  dropped `config_path` when the config was broken — exactly the state where
  the caller's only job is naming the file to fix. The shape is now stable.
- **Only Anthropic has a default model.** For `openai`/`compatible` the model
  must be named or the agent reports unavailable with an actionable message.
  Deliberate: a wrong-but-plausible default fails confusingly at request time.
- `compatible` without a `base_url` is a `ConfigError`, not a late failure.
- `effort` is passed through, but **only reasoning models accept it on
  OpenAI** — others return a 400, surfaced with an actionable message.
- TICKET-9's agent stalled mid-edit, leaving a duplicate `## 10.` heading and a
  broken `#10-extending-it` anchor in the README. Finished by hand; all
  internal anchors verified to resolve.

---

## 2026-08-20 — Categories are now decided per run, not from a fixed list

**What changed.** The fixed `CATEGORIES` constant is gone. Categories are
proposed per run from the target folder's actual contents by a **second
delegate agent**, and the UI was redesigned around making that pipeline
legible.

| Ticket | Files | Status |
|---|---|---|
| Core (mine) | `categories.py`, `rules.py`, `delegates.py`, `scheme.py`, `classifier.py`, `manager.py`, `plan.py`, `server.py` | ✅ |
| TICKET-6 | `static/index.html`, `app.js`, `styles.css` (redesign) | ✅ |
| TICKET-7 | `tests/test_organiser.py` — 22 → 52 tests | ✅ |
| TICKET-8 | `cli.py`, `README.md` | ✅ |

**Why.** The user asked for categories that come from the files' context
rather than a constant. They were shown the drift trade-off (the same concept
getting different names on different runs) and chose fully dynamic anyway.

**The design.** `scan()` is now three passes: rule table → **one corpus-level
call** to the new scheme proposer → per-file classifier restricted to the
proposed vocabulary. The corpus-level step is the important part: naming a
category per file in isolation is what produces `Invoices/`, `Bills/` and
`Receipts/` for one concept. The classifier's contract did not change — it
still receives "here are the allowed categories, pick one"; only the producer
of that list changed. Adding the proposer required no change to the delegate
seam, which is the "third agent drops in" claim finally being exercised.

**Verified ✅**
- Proposer receives exactly the rule-unplaceable files, in exactly one call.
- Existing folders in the target (plus the rule categories in use) are offered
  back as `existing_categories` — a run reused an existing `Tenancy/` instead
  of coining a synonym.
- Classifier receives proposal names + `Other`; a category outside that list is
  refused.
- Degraded path unchanged: no credentials → rules still run, unplaceable files
  left alone, no scheme, nothing guessed.
- 409 first-run gate, collision suffixing, never-delete/never-overwrite,
  top-level-only all still hold end-to-end through the server.
- **52/52 tests pass.** UI, `/static` assets and `/health` serve correctly.

**Unverified ⚠️** — still no API credentials on this machine, so **neither
delegate has ever run against the live API**. Every delegated decision in
testing came from a stub. The scheme proposer's real request/response shape in
`scheme.py` is completely unexercised.

**Side effects / gotchas**
- **New attack surface:** category names are now model-produced and become real
  folder names. `normalise_category()` strips separators and traversal, rejects
  reserved names, folds accents and caps length at 40. Verified a proposed
  `"../escaped"` lands at `target/escaped/`, never outside the target.
- **Bug found and fixed:** the manager recorded the delegate's *raw* proposed
  name for display, so the UI would have shown `../escaped` while the folder on
  disk was `escaped`. Now normalised at the manager boundary too.
- **Bug found by the test agent and fixed:** the manager handed out a
  vocabulary but then accepted whatever the delegate returned — enforcement
  lived only inside `ClassifierAgent`. A third-party delegate could create a
  folder nobody proposed. The manager now refuses out-of-vocabulary categories,
  files them under `Other` and flags them for review. Two tests that encoded
  the old weaker behaviour were updated (not weakened) and one added.
- `/health` no longer returns `categories`; it returns `rule_categories` and
  `dynamic_categories: true`.
- `index.html` previously referenced `styles.css` relatively, which 404'd since
  Flask serves at `/static`. Now root-relative.
- The UI's scan stage indicator is **time-estimated**, not real progress —
  `/scan` is a single response with no stream. It never infers anything about
  the final plan.
- **Drift is real and not solved.** Reuse of existing folder names reduces it;
  nothing is persisted between runs. A folder stabilises once organised, but a
  first run can name things differently than a later one. Persisting the
  approved scheme is the fix if it becomes annoying.

---

## 2026-08-19 — Initial build: manager + delegated classifier, web UI, CLI

**What changed.** Built the whole v1 from the brief in `organiser-agent-prompt.md`,
split across five tickets delivered by parallel agents:

| Ticket | Files | Status |
|---|---|---|
| Foundation | `categories.py`, `rules.py`, `delegates.py`, `classifier.py`, `paths.py`, `plan.py`, `undo_log.py` | ✅ |
| TICKET-2 | `manager.py` | ✅ |
| TICKET-3 | `server.py`, `run.py` | ✅ |
| TICKET-4 | `static/index.html`, `app.js`, `styles.css` | ⚠️ not rendered in a browser |
| TICKET-5 | `cli.py`, `README.md`, `requirements.txt`, `.gitignore` | ✅ |
| Tests | `tests/test_organiser.py` (22 tests) | ✅ |

**Why this shape.** The manager/classifier split is enforced structurally, not
by comment: the manager resolves its delegate through the registry in
`delegates.py` and only ever sees the `Delegate` protocol. It never imports
`ClassifierAgent`. The delegate is handed a *snippet* the manager read, never a
path — so a delegate physically cannot touch the filesystem. Adding a dedupe
agent means implementing `Delegate` and calling `register()`; the manager's
control flow does not change.

**Verified ✅**
- Rule pass places the easy cases with no delegation; filename rules beat
  extension rules (`Screenshot ....png` → `Screenshots`, not `Images`).
- Only rule-unmatched files are delegated (checked via a stub delegate that
  records what it was asked about).
- **Graceful degradation**: with no credentials the rule pass still ran and the
  3 unplaceable files were left in place with an honest reason — never guessed.
- **First-run gate**: `POST /apply` on a never-applied folder returned
  `409 requires_confirmation` and moved nothing; the confirmed retry moved 10/10.
- **Never overwrites**: `notes.md` landed as `Documents/notes_1.md` with the
  pre-existing `Documents/notes.md` byte-identical afterwards.
- **Never deletes / top-level only**: dotfile, symlink and `existing-subfolder/`
  untouched across a full apply; file count preserved.
- Undo log: 10 moves → 10 JSONL entries with source, destination, category,
  deciding agent and confidence. `cli.py undo <plan_id> --apply` reversed all
  3 moves of a test run and logged the reversals under `undo:<plan_id>`.
- CLI: `scan` (dry run), `apply` (refuses without confirmation, even
  non-interactively), `log`, `undo`. Exits non-zero on unknown plan id.
- Server: `/health`, `/scan`, `/plan/<id>`, `/apply`, `/log`, `/plans` all
  return JSON; binds 127.0.0.1 only.
- `.venv/bin/python -m unittest discover -s tests -t .` → **22/22 pass**.

**Unverified ⚠️**
- **The live classifier has never run.** This machine has no
  `ANTHROPIC_API_KEY`, no auth token and no `ant` profile, so every delegated
  decision in testing came from a stub. The manager-side contract is proven;
  the actual Anthropic request/response shape in `classifier.py` is not.
  First real run with a key is the thing to watch.
- The web UI was served (HTTP 200) but never opened in a browser.

**Side effects / gotchas**
- `classifier.available()` originally returned `True` whenever no env var was
  set, on the assumption an `ant` profile existed. On a machine with no
  credentials at all this advertised the agent as available and would have
  skipped every delegated file while the UI claimed it was working. Now it
  checks for an actual resolvable credential and reports
  `"no credentials found — set ANTHROPIC_API_KEY or run \`ant auth login\`"`.
- Moving a file leaves any symlink pointing at it dangling. That is inherent to
  moving and is not a deletion — the link itself is never touched. One test
  initially failed because it counted a dangling symlink as a lost file; the
  test was corrected, not the code.
- State lives **outside** the repo at `~/.file-organiser-agent/`
  (plans, `undo-log.jsonl`, `acknowledged-targets.json`), overridable with
  `FILE_ORGANISER_STATE_DIR`. Two agents testing against the same state dir
  wiped each other's plans mid-run — use a private dir when testing in parallel.
- `run.py` defaults to port **5001**, not 5000 — AirPlay Receiver holds 5000 on
  macOS.
- `tests/__init__.py` is required for `unittest discover` to import the package.

**Known gap.** The original `file_organizer.py` was not in the workspace, so
`EXTENSION_TABLE` / `FILENAME_RULES` in `rules.py` are a reconstruction from the
brief. Swapping in the real table is a change to those two constants and
nothing else.

**Not done on purpose.** No `git init`, no commits, no pushes — those are the
author's to make.
