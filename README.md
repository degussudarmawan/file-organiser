# File Organiser Agent

A local agent system that tidies the top-level files of one folder — inventing
the folder scheme from what is actually in there — and can explain and reverse
every single thing it did.

> **Status:** this machine has **no API credentials for any provider**, so **no
> provider has ever completed a real request** and neither delegate agent has
> ever been run against a live model. The Anthropic path was exercised only
> through stubs. The OpenAI path — which also serves the local `compatible`
> provider — was written against the installed SDK's signatures
> (`client.responses.parse(..., text_format=Model)` → `response.output_parsed`,
> verified by introspection) but **never executed**. Prompts, response schemas
> and error handling are written and reviewed, not empirically exercised.
>
> What *has* been run end to end: the rule pass, the no-credential path,
> provider and model resolution in every combination (`cli.py config`), and
> scan / apply / log / undo on real files.

## 1. What it is

The original `file_organizer.py` was a script: a lookup table from extension to
folder name. It worked until it met a file the table had no opinion about, and
then it either guessed or gave up.

This is that script rebuilt as an agent system — one manager and two delegates:

- **The manager agent** (`organiser/manager.py`) owns the run. It walks the
  folder, applies the rule pass itself, decides what to delegate, builds the
  plan, and — only when explicitly told to — moves the files. It is the only
  component in the codebase that writes to disk.
- **The scheme proposer agent** (`organiser/scheme.py`) is a delegate that runs
  **once per run**. It is handed every file the rules could not place — all of
  them, in a single call — and it answers one question: *what categories does
  this folder actually need?* It hands back a short list of names, each with a
  one-line description.
- **The classifier agent** (`organiser/classifier.py`) is a delegate that runs
  **once per file**. The manager hands it a filename, a short content snippet,
  and the categories the proposer just chose — and it hands back one of those
  categories, a confidence level, and a one-line reason.

Both delegates are stateless, read-only, and decision-only: they are given
filenames and snippets, never a path, so they *cannot* move anything even if
they wanted to.

Neither delegate imports a model SDK or names a model. They ask a **provider**
(`organiser/providers/`) for one thing — "given this system prompt and this
prompt, return an object matching this schema" — so which company answers, or
whether it is a model running on your own machine, is a configuration value.
See [Choosing a provider and model](#5-choosing-a-provider-and-model).

### Why the vocabulary and the assignment are two different agents

This is the one design decision worth defending at length.

The obvious build is a single agent: hand it a file, let it name a folder. It
does not survive contact with a real `~/Downloads`. Asked to name a category
for `rates_notice.pdf`, then independently for `electricity_bill.pdf`, then for
`strata_levy.pdf`, a model will cheerfully answer `Bills`, `Utilities` and
`Property` — three folders for one concept, because each call is naming a
category in isolation and has no idea the other two exist. The failure is
structural, not a prompting bug: you cannot get a coherent vocabulary out of
decisions that never see each other.

So the vocabulary is decided **once, from the whole corpus**, before any file
is assigned. The proposer sees all the unplaceable files at once, which is what
lets it notice that those three are one group and say so. The classifier is
then a much smaller, cheaper problem: pick one of these N names, or the
fallback. It is not allowed to invent a name at all — a category outside the
allowed list is rejected in `classifier.py` and the file is left alone.

This split is also the concrete proof of the extensibility claim. The manager
was written for one delegate; adding the proposer needed **no change to the
manager's control flow** — a second protocol in `organiser/delegates.py`, a new
module that calls `register()`, and the manager resolves it by name like the
first. That is what the registry was for. See [Extending it](#11-extending-it).

The division of labour across all three passes is deliberate: rules are fast,
free, and deterministic, so they go first and handle the overwhelming majority.
The proposer is the expensive, judgement-heavy call, so it happens exactly once
per run. The classifier is the cheap per-file call, and it only ever sees the
residue the rules genuinely could not place.

That last clause is the default, not a law. The rule pass can be switched off
for a run (`--no-rules`), which hands *every* file to the two agents — better
grouping, at one model call per file. See
[Turning the rule table off](#turning-the-rule-table-off).

## 2. Architecture

```
run.py                  entry point: starts the local server
cli.py                  entry point: the command-line front end
organiser/
  manager.py            orchestrator. Owns scan/apply. THE ONLY MODULE THAT
                        TOUCHES THE FILESYSTEM.
  rules.py              the fast first pass: filename + extension table.
                        No I/O, no network, no model. Returns None rather
                        than guessing — that None is the delegation signal.
                        rule_categories() exports its fixed names.
  scheme.py             delegate #1: the scheme proposer. One call per run,
                        sees every unplaceable file at once, proposes this
                        folder's categories.
  classifier.py         delegate #2: the classifier. One call per file,
                        restricted to the proposed categories plus the
                        fallback. Read-only, decision-only, stateless.
                        Raises DelegateUnavailable rather than guessing.
  delegates.py          the registry and both protocols — Delegate (per-file
                        decisions) and SchemeProposer (corpus-level
                        vocabulary). This is what keeps the manager/agent
                        split honest.
  providers/            the ONLY place in the codebase that imports a model
                        SDK. Neither delegate does.
    __init__.py         the Provider protocol (complete_json) and
                        get_provider(). Imports are lazy and per provider,
                        so having only the SDK you use is never an error.
    anthropic_provider.py   the `anthropic` SDK.
    openai_provider.py      the `openai` SDK — serves both the "openai"
                        provider and "compatible", which is the same client
                        pointed at a base_url (Ollama, LM Studio, vLLM,
                        OpenRouter).
  config.py             which provider, and which model and effort each agent
                        uses. Resolves environment variable -> config.json ->
                        built-in default, and records which layer won for
                        every value. Holds no credentials, ever.
  categories.py         category-name safety: normalise_category(),
                        dedupe_categories(), is_safe_category(), and the
                        FALLBACK_CATEGORY. Not a vocabulary — the vocabulary
                        is proposed per run.
  plan.py               the Plan/PlanItem model and the on-disk PlanStore.
                        A plan is inert: building one moves nothing.
  paths.py              every path-safety rule in one auditable place:
                        target validation, top-level-only, non-overwriting
                        destination naming.
  undo_log.py           the append-only move log and the first-run
                        acknowledgement ledger.
  server.py             a thin JSON shell around the manager. No organising
                        logic of its own.
static/                 the single-page web UI.
```

One scan is three passes, in increasing order of cost:

```
1. rules.py            every file        local, free, deterministic
       │                                 anything it cannot place falls through
       ▼
2. scheme.py           ONE call          all the leftovers at once
       │                                 -> this run's category names
       ▼
3. classifier.py       one call per file restricted to those names + "Other"
       │
       ▼
   plan.py             an inert Plan. Nothing has moved.
```

**The invariant:** the UI holds no organising logic and has no filesystem
access. It talks to the manager exclusively through the HTTP endpoints
(`POST /scan`, `POST /apply`, `GET /plan/<id>`, `GET /plans`, `GET /log`,
`GET /health`). Every safety rule therefore lives in one place and cannot be
bypassed by a second front end — which is why the CLI and the browser behave
identically.

### The layering, plainly

There are two pluggable seams, one below the other, and they are the same idea
applied twice:

```
manager.py          knows nothing about models
    │  Delegate / SchemeProposer protocols  (organiser/delegates.py)
    ▼
scheme.py           the agents are pluggable in the manager:
classifier.py       swap one by name, no control-flow change
    │  Provider protocol                   (organiser/providers/__init__.py)
    ▼
providers/          the providers are pluggable in the agents:
  anthropic         swap one with a config value, no agent change
  openai
  compatible
```

The manager resolves an agent by name from a registry and only ever calls
`available()` / `decide()` / `propose()`. The agents resolve a provider from
config and only ever call `available()` / `complete_json()`. Neither layer
imports the layer below it directly, which is why adding a provider touches no
agent and adding an agent touches no manager code.

## 3. How categories are decided

**There is no fixed category list.** There used to be — a `CATEGORIES`
constant every agent shared — and it is gone. Categories are now proposed per
run, from the actual contents of the folder you point at.

The only fixed names left are the rule table's own (`Images`, `Documents`,
`Archives`, …, from `EXTENSION_TABLE` in `organiser/rules.py`, exported by
`rule_categories()`). Everything beyond them is invented for the run and lives
on the plan: `plan.proposed_categories` — a list of `{name, description,
is_new}` — plus `plan.scheme_reason`, the proposer's one line on how it
grouped the folder. `GET /health` reflects this: it reports
`rule_categories` and `dynamic_categories: true`, not a vocabulary.

The CLI prints the scheme before the moves that use it, so you never read the
category names as if they were a fixed list:

```
PROPOSED CATEGORIES FOR THIS FOLDER (3)
---------------------------------------
  Grouped by what the documents are about: tenancy paperwork, coursework, tax.
  Proposed for this run by the scheme proposer agent, from the contents
  of this folder. ...

  Tenancy        NEW     Leases, bond receipts and landlord correspondence.
  Lecture Notes  NEW     Course handouts, slides and week-by-week notes.
  Documents      reused  Everything else paper-shaped.
```

Note what the rule table can and cannot contribute to that list. It groups by
file **type**, so it can produce `Documents` and never `Tenancy`: point it at a
folder of forty PDFs and it returns one undifferentiated `Documents/`, which is
a filing cabinet with one drawer. The agents group by what a file is **about**,
which is where `Tenancy` and `Lecture Notes` come from. Anything the rules place
is therefore a file the agents never get to say anything about — usually a good
trade, occasionally the wrong one. `--no-rules` is the switch that makes it your
choice per run rather than a fixed policy; the honest accounting of what it
costs is in [Turning the rule table off](#turning-the-rule-table-off).

### The trade-off: drift, honestly

Nothing is persisted between runs. No scheme file, no learned vocabulary, no
memory. **The same concept can therefore be named differently on different
runs** — `Tenancy` this week, `Rental Docs` next week, if the folder's contents
have shifted enough that the proposer sees the group differently. That is a
real cost of dropping the fixed list, and it is the price of the thing the
fixed list could never do: name a group `Tenancy` at all.

There is one mitigation, and it is worth being precise about how far it goes.
The proposer is handed an `existing_categories` list before it proposes
anything, containing:

- every non-hidden folder that already exists in the target directory, and
- every category the rule pass is filling on this run,

and it is instructed to **reuse those names verbatim** where they fit rather
than coin a synonym. So a folder stabilises once it has been organised once:
after the first run creates `Tenancy/`, the second run sees `Tenancy` in
`existing_categories` and is pushed hard toward reusing it. Categories marked
`reused` (rather than `NEW`) in the CLI output are exactly the ones this
mechanism caught.

**This reduces drift. It does not eliminate it.** The instruction is a prompt,
not a constraint — nothing in the code forces reuse, and a model can still
decide that `Rental Docs` is a better name than the `Tenancy` folder sitting
right there. A brand-new folder with no subfolders yet has nothing to anchor
to at all. If you need guaranteed stability, the rule table is the part of this
system that gives it to you.

### Category-name safety

A category name is now model output that becomes a real folder on your disk.
That is a path-injection surface, so every proposed name is put through
`normalise_category()` in `organiser/categories.py` before it can become one:

- **separators and traversal stripped** — `/`, `\`, `..` and anything outside
  `[A-Za-z0-9 &_-]` are removed, so a proposal can only ever name a single
  directory component;
- **control characters and non-printables dropped**;
- **accents folded to ASCII**, so `Facturación` degrades to `Facturacion`
  rather than losing the letter;
- **reserved names rejected** — `.`, `..`, and the Windows device names
  (`con`, `prn`, `aux`, `nul`) return `None`, which the caller treats as
  "unusable, fall back" rather than trying to repair;
- **capped at 40 characters** (`MAX_CATEGORY_LENGTH`), cut on a word boundary;
- **case-insensitive duplicates collapsed** (`dedupe_categories`), because a
  model asked for a list will occasionally return both `Invoices` and
  `invoices`.

Sanitising happens in three places on purpose — in the proposer as it parses
the response, in the manager as it records the scheme on the plan, and again in
the manager at the moment a category becomes a path. And it is still not what
the safety story rests on: before any move, `Manager._validate_item` re-checks
that the destination's parent is a **direct child of the target directory**
(`paths.is_direct_child`). A doctored plan file with a hand-edited category
cannot write outside the folder you named.

## 4. Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

That installs everything, which is the convenient default. Only `flask` and
`pydantic` are actually required: the two model SDKs are
**optional-per-provider**, and you only need the one belonging to the provider
you select. `organiser/providers/` imports lazily and per provider, so an
uninstalled SDK is an error only if you pick that provider.

```bash
.venv/bin/pip install flask pydantic anthropic   # provider "anthropic"
.venv/bin/pip install flask pydantic openai      # provider "openai" or "compatible"
```

Then give the delegates credentials. Out of the box the provider is
`anthropic`, and both agents share one credential:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

or log in once with `ant auth login` — the SDK resolves either. For any other
provider, and for choosing which model each agent uses, see
[Choosing a provider and model](#5-choosing-a-provider-and-model). Whatever you
pick, `cli.py config` will tell you what is in force and whether it can run:

```bash
.venv/bin/python cli.py config
```

**With no key, the tool still works.** The rule pass runs exactly as before and
places everything it recognises. Files the rules cannot place are reported as
skipped, with the reason, and are **left alone** — the system does not fall
back to guessing, and it does not sweep unknowns into `Other` just to have an
answer. Fewer files get filed; none get filed wrongly.

The degradation is ordered, and it matters which agent fails. If the *scheme
proposer* cannot run there is no vocabulary at all, so the classifier is never
asked and every unplaceable file is left alone with that reason on it. If the
*classifier* fails partway through a run, the manager stops delegating for the
rest of the run — the same auth or network fault would hit every remaining
call — and the untouched files say so.

Check what the delegates think of their own credentials — from the CLI, which
also shows the resolved provider and models and needs no server running:

```bash
.venv/bin/python cli.py config
```

or over HTTP:

```bash
curl -s http://127.0.0.1:5001/health
```

`/health` reports the classifier's availability, the size of the rule table,
`rule_categories`, `dynamic_categories: true`, and a `provider` block —
`{name, base_url, scheme_model, classifier_model, config_path, sources,
error}` — which is the same resolution `cli.py config` renders, read fresh on
every request. There is no separate health field for the scheme proposer: both
delegates run the same credential probe, so the classifier's answer stands in
for both. The CLI prints the same availability line in the header of every
scan, and if the proposer independently fails at scan time the reason lands on
the plan, in the `Scheme:` line and on each skipped file.

## 5. Choosing a provider and model

The delegates do not know which company answers them. They hand a **provider** a
system prompt, a prompt and a Pydantic schema, and get a validated object back.
Which provider that is — and which model and effort each agent uses — is
configuration, and this section is how you set it.

### The three providers

| `provider` | Talks to | Needs |
| --- | --- | --- |
| `anthropic` *(default)* | the Anthropic API | the `anthropic` SDK; `ANTHROPIC_API_KEY` or `ant auth login` |
| `openai` | the OpenAI API | the `openai` SDK; `OPENAI_API_KEY` |
| `compatible` | any OpenAI-protocol endpoint at a `base_url` — **Ollama**, LM Studio, vLLM, OpenRouter | the `openai` SDK; a `base_url` |

`compatible` is the one that matters for the brief's *runs entirely locally*
constraint. Pointed at Ollama on `http://localhost:11434/v1`, the whole system
— filenames, content snippets, the categories it invents — stays on your
machine. Nothing leaves it.

Two things about this are deliberate and will otherwise look like bugs:

- **API keys are never stored in the config file.** They stay in
  `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`, in the environment. The state
  directory holds plans and an undo log, and it should never hold a secret.
  `cli.py config` reports whether the variable is *set*; it never reads the
  value and there is no code path that prints one.
- **Only `anthropic` has a built-in default model** (`claude-opus-5`). For
  `openai` and `compatible` you must name the model yourself, and until you do,
  both agents report themselves unavailable with a message naming exactly what
  to set. Model line-ups change; a wrong-but-plausible default would fail at
  request time with a confusing 404, where an unset one fails immediately with
  something you can act on.

### The config file

`~/.file-organiser-agent/config.json` — inside the state dir, so
`FILE_ORGANISER_STATE_DIR` relocates it along with everything else. Every key
is optional; this is the full shape.

```json
{
  "provider": "openai",
  "base_url": null,
  "scheme":     { "model": "gpt-5",      "effort": "medium" },
  "classifier": { "model": "gpt-5-mini", "effort": "low" }
}
```

Two models rather than one, because the two agents are not the same problem.
The scheme proposer makes one judgement call per run, sees the whole folder at
once, and is worth spending on. The classifier fires once per file and is where
cost and latency accumulate. A large model above a small one is the intended
shape, and `effort` follows the same logic — it is passed straight through to
the provider as a reasoning-effort setting.

The file is **re-read on every call**, so editing it takes effect immediately,
including in a running server.

**Nothing writes this file for you.** If it does not exist, `cli.py config`
prints a starter version reflecting your current settings — to paste, not
because it created anything.

### Environment variables

Six, one per setting. Each overrides the corresponding config-file key for one
run without writing anything down.

| Variable | Overrides | Example |
| --- | --- | --- |
| `FILE_ORGANISER_PROVIDER` | `provider` | `anthropic`, `openai`, `compatible` |
| `FILE_ORGANISER_BASE_URL` | `base_url` | `http://localhost:11434/v1` |
| `FILE_ORGANISER_SCHEME_MODEL` | `scheme.model` | `gpt-5` |
| `FILE_ORGANISER_CLASSIFIER_MODEL` | `classifier.model` | `gpt-5-mini` |
| `FILE_ORGANISER_SCHEME_EFFORT` | `scheme.effort` | `medium` |
| `FILE_ORGANISER_CLASSIFIER_EFFORT` | `classifier.effort` | `low` |

The credential variables (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`) are not in
this list on purpose — they belong to the SDKs, and this tool only ever asks
whether they are set.

### Precedence

```
environment variable   →   config.json   →   built-in default
        (highest)                                (lowest)
```

`cli.py config` prints which of the three won for every single value, which is
the thing you actually want when a run used a model you did not expect.

One wrinkle, because it prevents a genuinely confusing failure: **model names
in the config file are scoped to the provider that file names.** If the file
says `"provider": "openai"` and you override it with
`FILE_ORGANISER_PROVIDER=anthropic`, the per-agent model names in that file are
dropped rather than carried over, so an OpenAI model id is never sent to
Anthropic. An explicit `FILE_ORGANISER_SCHEME_MODEL` /
`FILE_ORGANISER_CLASSIFIER_MODEL` still wins over everything. `effort` is not
provider-specific and does carry over.

(The scoping only triggers when the config file itself names a provider — a
file with model names and no `"provider"` key has nothing to scope them to, and
they apply to whatever provider is in force. Worth knowing if you keep a
provider-less config file and switch providers by environment variable.)

### `cli.py config`

The command that answers "what will actually run, and why isn't it working".

```bash
.venv/bin/python cli.py config           # effective settings + where each came from
.venv/bin/python cli.py config --json    # the same, machine-readable
.venv/bin/python cli.py config --path    # just the path, for $EDITOR
```

```
EFFECTIVE SETTINGS
------------------
  provider     openai      (config file)
  base_url     —           (default)
  scheme       gpt-5       (config file)  effort medium  (config file)
  classifier   gpt-5-mini  (environment)  effort low     (default)

  api key      set         (OPENAI_API_KEY)
  config file  ~/.file-organiser-agent/config.json

  The API key is never read or stored by this tool's config —
  it stays in the environment, so the state dir holds no secret.

AGENT STATUS
------------
  scheme       available    openai/gpt-5 — ready
  classifier   available    openai/gpt-5-mini — ready
```

The AGENT STATUS block is each delegate's own `available()` probe — the same
one a scan runs — so an unset key, an unnamed model, a missing SDK and a broken
config file all surface here rather than as a scan that quietly files nothing.
It makes no network call, so it cannot tell you the endpoint is reachable, only
that the settings are coherent. A bad config file is the one case that exits
non-zero; unavailable agents are reported, not treated as a command failure.

Edit the file with:

```bash
$EDITOR $(.venv/bin/python cli.py config --path)
```

`--path` deliberately does not load the settings first — the moment you most
need the path is when what is in it is broken.

### Worked example: Anthropic

The default provider and the only one with a default model, so a key is the
whole setup and no config file is needed:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
.venv/bin/python cli.py config      # provider anthropic (default), model claude-opus-5 (default)
.venv/bin/python cli.py scan ~/Downloads
```

To use a cheaper model for the per-file classifier, which is where the volume
is, without writing anything down:

```bash
FILE_ORGANISER_CLASSIFIER_MODEL=<a-smaller-model-id> .venv/bin/python cli.py scan ~/Downloads
```

(Use a model id your account actually has. This README deliberately does not
name one beyond the built-in default: no request has ever been made from this
machine, so any id here would be a guess dressed up as a recommendation.)

### Worked example: OpenAI

The models must be named — there is no default for this provider:

```bash
export OPENAI_API_KEY=sk-...
mkdir -p ~/.file-organiser-agent
cat > ~/.file-organiser-agent/config.json <<'JSON'
{
  "provider": "openai",
  "scheme":     { "model": "gpt-5",      "effort": "medium" },
  "classifier": { "model": "gpt-5-mini", "effort": "low" }
}
JSON
.venv/bin/python cli.py config
```

Use model ids your account actually has; the two above are illustrative, not
verified. **`effort` is sent on every request and only reasoning models accept
it on OpenAI** — a non-reasoning model returns a 400, which the provider
surfaces as *"bad request — check the model id, and note that only reasoning
models accept an effort setting"*. See
[Known gaps](#known-gaps--v1-scope): there is currently no way to switch effort
off.

### Worked example: Ollama, entirely local

Nothing leaves the machine — no key, no network, no account. This is the
configuration that satisfies the brief's "runs entirely locally" constraint in
the strongest sense.

```bash
ollama serve &                 # serves the OpenAI protocol on :11434/v1
ollama pull qwen3:14b          # the once-per-run scheme proposer
ollama pull qwen3:4b           # the once-per-file classifier

mkdir -p ~/.file-organiser-agent
cat > ~/.file-organiser-agent/config.json <<'JSON'
{
  "provider": "compatible",
  "base_url": "http://localhost:11434/v1",
  "scheme":     { "model": "qwen3:14b", "effort": "medium" },
  "classifier": { "model": "qwen3:4b",  "effort": "low" }
}
JSON
.venv/bin/python cli.py config
```

```
  provider     compatible                 (config file)
  base_url     http://localhost:11434/v1  (config file)
  scheme       qwen3:14b                  (config file)  effort medium  (config file)
  classifier   qwen3:4b                   (config file)  effort low     (config file)

  api key      not set                    (OPENAI_API_KEY — usually not needed for a local endpoint)
```

`compatible` needs no API key: the SDK insists one exists, so the provider
supplies a placeholder rather than making you invent an environment variable to
run Ollama. Set `OPENAI_API_KEY` anyway if your endpoint wants one (OpenRouter
does).

Three caveats, all honest limits rather than bugs:

- **`base_url` is required** for `compatible`. Without one, `load_settings()`
  refuses to resolve at all and every command that needs it says so and exits
  non-zero — better than defaulting to a port and timing out.
- **`available()` cannot tell you the server is up.** It never makes a network
  call, so for `compatible` it reports "using `<base_url>`" as soon as the
  settings are coherent. If nothing is listening you find out at request time,
  where the provider says *"could not reach the API (is anything serving
  http://localhost:11434/v1?)"*.
- **Model choice matters more locally than it does with a frontier API.** Both
  agents depend on structured output that validates against a Pydantic schema,
  and small local models are markedly worse at that. The failure is safe — an
  unusable response raises, and the manager leaves the files alone — but a
  model too small to hold the format will simply organise nothing. None of this
  has been run here; there are no credentials on this machine and no local
  server was stood up.

### Swapping provider for one run

Nothing needs restarting and nothing is written down:

```bash
FILE_ORGANISER_PROVIDER=compatible \
FILE_ORGANISER_BASE_URL=http://localhost:11434/v1 \
FILE_ORGANISER_SCHEME_MODEL=qwen3:14b \
FILE_ORGANISER_CLASSIFIER_MODEL=qwen3:4b \
  .venv/bin/python cli.py scan ~/Downloads
```

## 6. Start the server

```bash
.venv/bin/python run.py
```

Then open <http://127.0.0.1:5001>. Options: `--host`, `--port`, `--debug`.

It binds to `127.0.0.1` — this reads and moves your real files, so it is not
something to expose on a network.

## 7. Run a dry run

Nothing in this tool moves a file without a separate, explicit apply step.
Scanning is always safe.

**In the UI:** type a folder (e.g. `~/Downloads`), hit scan, and read the plan.
Moves are grouped by category and tagged with which agent decided them; the
low-and-medium-confidence classifier picks are pulled out into their own
review block with the reason the agent gave.

**On the command line:**

```bash
.venv/bin/python cli.py scan ~/Downloads
```

You get, in order:

1. the target and the delegate availability line;
2. **PROPOSED CATEGORIES FOR THIS FOLDER** — the scheme the proposer invented
   for this run, its rationale, and a `NEW` / `reused` marker per name;
3. **PLANNED MOVES**, grouped by category, each line led by the agent that
   decided it — `[rules]` for a local table hit, `[classifier high|medium|low]`
   for a delegated pick — as `filename -> Category/destination_name`;
4. **NEEDS REVIEW**, the classifier picks below high confidence, with reasons;
5. **SKIPPED**, with the reason each file was left alone;
6. a summary and the plan id.

If no scheme could be proposed, block 2 is absent and a `Scheme:` line in the
header says so and why. A run started with `--no-rules` adds a `Rules: skipped`
line to the same header block, and block 3 carries no `[rules]` tags at all —
see [Turning the rule table off](#turning-the-rule-table-off).

A scan makes real model calls, so it is not instant. It prints a short progress
note to **stderr** while it works; that note never appears in `--json` output.

Useful flags:

```bash
.venv/bin/python cli.py scan ~/Downloads --max-delegations 20   # cap per-file calls
.venv/bin/python cli.py scan ~/Downloads --no-rules             # skip the rule table
.venv/bin/python cli.py scan ~/Downloads --json                 # raw plan JSON
```

`--max-delegations` caps the third pass only. The single scheme-proposer call
still happens — without it there would be no categories for the files under the
cap either.

`--json` prints `plan.to_dict()` verbatim, which includes `proposed_categories`,
`scheme_reason` and `used_rules` alongside the items:

```bash
.venv/bin/python cli.py scan ~/Downloads --json | jq '.proposed_categories'
```

`run` without `--apply` is the same dry run in one shot, and says so loudly:

```bash
.venv/bin/python cli.py run ~/Downloads
```

### Turning the rule table off

`--no-rules` skips the extension/filename table entirely and sends **every**
file to the two agents. It works on both `scan` and `run`:

```bash
.venv/bin/python cli.py scan ~/Documents --no-rules
.venv/bin/python cli.py run ~/Documents --no-rules --max-delegations 20
```

Over HTTP it is the `use_rules` field on `POST /scan` (default `true`), and it
is recorded on the plan as `used_rules` so a plan can always explain itself:

```bash
curl -s localhost:8000/scan \
  -H 'content-type: application/json' \
  -d '{"target_dir": "~/Documents", "use_rules": false}' | jq '.used_rules'
```

**What you get.** Better grouping, because the thing the rules are bad at is
exactly the thing the agents are for. The table groups by file *type*, so forty
PDFs become one `Documents/`; the agents group by what the files are *about*,
so the same forty become `Tenancy`, `Lecture Notes`, `Tax`. With rules on, the
proposer never even sees a file the table matched — it cannot propose `Tenancy`
for a `.pdf` it was never shown. With rules off it sees the whole folder, and
the scheme is proposed from all of it.

**What it costs.** The two passes have completely different price tags, and it
is worth doing the arithmetic before pointing this at a big folder:

| | Rule pass | Agent passes |
| --- | --- | --- |
| Cost | free | one scheme call + **one model call per file** |
| Time | instant, local | a network round trip per file |
| Determinism | same answer every time | model output; can vary run to run |
| Coverage | only what the table matches | anything, including binaries by name |

Normally the per-file cost applies only to the leftovers — the residue the
table could not place, often a handful of files. With `--no-rules` it applies
to every single file in the folder. A folder of 12 files is a rounding error; a
`~/Downloads` with 800 in it is 800 model calls and the wall-clock and bill to
match. The CLI warns about this on stderr before it starts.

**`--max-delegations` is the natural companion.** It caps how many files reach
the classifier, so it is what makes `--no-rules` safe to experiment with: you
see the scheme proposed from the whole folder, get the first N files filed
under it, and everything past the cap is left in place with that stated as the
reason. Start there before running it uncapped:

```bash
.venv/bin/python cli.py scan ~/Downloads --no-rules --max-delegations 10
```

**The degenerate case.** With rules off *and* the agents unavailable — no API
key, a broken config, a provider that cannot resolve a model — there is nothing
left in the system that can classify anything. Every file is skipped and the
run accomplishes precisely nothing. The same broken setup with rules **on**
still files everything the table recognises, which is why this is specific to
the combination. The CLI detects it before scanning and says so rather than
printing an empty plan:

```
--no-rules, but the delegate agents cannot run:
  classifier unavailable (no credentials found — set ANTHROPIC_API_KEY ...)
  Rules are off, so there is nothing else to classify with: every file will be skipped
  and this run will organise nothing. Drop --no-rules to still file what the rule table
  recognises, or fix the configuration: .venv/bin/python cli.py config
```

Note which caveat this leans on hardest: everything `--no-rules` buys is
delivered by the two agents, and per the status note at the top of this file,
**no provider has ever completed a real request on this machine**. Rules-only
runs are the part of this system that has actually been exercised end to end.
Turning the rules off multiplies your dependence on the part that has not been.
The plan is still a dry run and still needs an explicit apply, so the way to
judge it is to read the plan.

Category names still drift between runs, and `--no-rules` gives drift more
surface to work on — every file's category now comes from a model rather than a
table. See [the drift trade-off](#the-trade-off-drift-honestly).

## 8. Apply a plan

**In the UI:** review the plan, untick anything you disagree with (approval is
per item — the NEEDS REVIEW block is the one to read closely), then apply. The
first time you apply to a given folder, a confirmation dialog names that folder
and makes you say yes.

**On the command line**, applying takes the plan id from a scan:

```bash
.venv/bin/python cli.py apply 3f9c1a2b4d5e
```

Apply only some items:

```bash
.venv/bin/python cli.py apply 3f9c1a2b4d5e --only a1b2c3d4,e5f6a7b8
```

Scan and apply in one shot — the direct descendant of the original script's
`--apply` flag:

```bash
.venv/bin/python cli.py run ~/Downloads --apply
```

The first apply to any new folder prompts for an interactive `yes`. Pass
`--yes` to answer it non-interactively (scripts, cron). The command prints
moved / skipped / failed, and exits non-zero if anything failed.

## 9. Where the undo log lives

State lives outside the repo, under `~/.file-organiser-agent/`:

| Path | What it is |
| --- | --- |
| `~/.file-organiser-agent/undo-log.jsonl` | Every real move, one JSON object per line, append-only. |
| `~/.file-organiser-agent/plans/` | Saved plans as JSON, one file per plan id. |
| `~/.file-organiser-agent/acknowledged-targets.json` | Folders whose first apply you have confirmed. |

(Set `FILE_ORGANISER_STATE_DIR` to relocate all of it.)

One log line looks like this:

```json
{"timestamp": "2026-08-19T12:34:56+00:00", "plan_id": "3f9c1a2b4d5e", "item_id": "a1b2c3d4e5f6", "source": "/Users/you/Downloads/report.pdf", "destination": "/Users/you/Downloads/Documents/report.pdf", "category": "Documents", "decided_by": "rules", "confidence": "high"}
```

`decided_by` is `"rules"`, the delegate's name (`"classifier"`), or `"undo"`
for a reversal, so the log tells you which agent is responsible for any file
that ended up somewhere odd. The category recorded is the sanitised name that
actually became a folder, not the raw proposal.

JSON Lines, so `grep`, `jq`, and `tail -f` all work on it directly. Nothing in
the app ever deletes from this file.

Read it through the tool:

```bash
.venv/bin/python cli.py log --limit 20
.venv/bin/python cli.py log --plan-id 3f9c1a2b4d5e
```

**To reverse a run**, undo is a dry run first, like everything else:

```bash
.venv/bin/python cli.py undo 3f9c1a2b4d5e            # show what would move back
.venv/bin/python cli.py undo 3f9c1a2b4d5e --apply    # actually move it back
```

Undo reads the log for that plan and moves each file from its destination back
to its source, newest move first. It skips — and tells you why — any entry
whose destination no longer exists (you moved or deleted it yourself) or whose
original path is occupied again, because restoring it would mean overwriting
something. The reversals are themselves appended to the log under
`undo:<plan_id>`, so the history stays honest: the log records what happened,
not what was intended.

## 10. Safety guarantees

- **It never deletes.** The only filesystem operation in the codebase is a
  move. There is no `unlink`, no `rmtree`, no "clean up".
- **It never overwrites.** A destination that already exists gets `_1`, `_2`,
  … appended before the extension (`paths.unique_destination`). Names claimed
  earlier in the same plan are reserved too, so two same-named files in one run
  cannot collide with each other.
- **Top-level files only.** Subfolders are never entered and never moved.
  Hidden files (dotfiles) and symlinks are skipped. Enforced in
  `paths.should_skip` and `paths.is_direct_child`.
- **Dry run by default.** Scanning builds an inert plan object. Applying is a
  separate call with a plan id, in both the CLI and the HTTP API.
- **The first apply to any folder needs explicit confirmation**, named folder
  and all, recorded in `acknowledged-targets.json` so you are asked once per
  folder rather than nagged forever.
- **Every move is logged** with its source, destination, plan, deciding agent
  and confidence — enough to reverse it by hand if you never touch this tool
  again.
- **Absurd targets are refused.** `/` and `$HOME` itself are rejected as too
  broad (`paths.validate_target_dir`).
- **An unavailable agent means "leave it alone", never "guess".** No API key,
  no network, a model error, a scheme that could not be proposed, or a category
  outside the allowed list all resolve to "the rules had no opinion and the
  delegates could not help, so this file stays where it is." The manager never
  invents a vocabulary of its own when the proposer fails.
- **Model-produced category names are sanitised before they are paths.**
  Separators, traversal, control characters and reserved names cannot survive
  `categories.normalise_category()`, names are capped at 40 characters, and the
  manager still re-validates that every destination sits directly inside the
  target. See [How categories are decided](#3-how-categories-are-decided).
- **Delegates cannot touch the filesystem.** They are handed a snippet and a
  filename, not a path. Moving is the manager's job alone. This holds for the
  scheme proposer too: it sees a list of `FileSummary` objects, which carry a
  filename, a snippet and a size, and nothing else.

## 11. Extending it

`organiser/delegates.py` defines **two** protocols, and which one you implement
depends on the shape of the question your agent answers:

| Protocol | Called | Gets | Returns |
| --- | --- | --- | --- |
| `Delegate` | once per file | `DelegateRequest` — filename, snippet, size, allowed categories | `DelegateDecision` — category, confidence, reason |
| `SchemeProposer` | once per run | `SchemeRequest` — every unplaceable file, plus the categories already in use | `SchemeProposal` — a list of `ProposedCategory`, plus a rationale |

Both are `@runtime_checkable` Protocols with an `available() -> (bool, str)`
probe, and both register into the same registry by name.

The scheme proposer is the worked example of adding an agent: it went in as a
new protocol plus a new module, and `manager.py`'s control flow did not change
to accommodate it — the manager resolves it by name (`Manager(scheme_name=...)`)
exactly as it resolves the classifier.

Adding a third agent takes one file and one function call. For a per-file
agent, implement `Delegate`:

```python
from organiser.categories import FALLBACK_CATEGORY
from organiser.delegates import (
    DelegateDecision, DelegateRequest, DelegateUnavailable, register,
)


class DedupeAgent:
    name = "dedupe"

    def available(self) -> tuple[bool, str]:
        return True, "ready"

    def decide(self, request: DelegateRequest) -> DelegateDecision:
        # Read-only. You get request.filename, request.snippet,
        # request.snippet_is_binary, request.size_bytes and
        # request.allowed_categories — and no path to write to.
        # allowed_categories is whatever the scheme proposer chose for
        # this run plus FALLBACK_CATEGORY; returning anything outside it
        # is rejected and the file is left alone.
        # Raise DelegateUnavailable if you cannot answer; never guess.
        return DelegateDecision(
            category=FALLBACK_CATEGORY,
            confidence="medium",
            reason="looks like a duplicate of an earlier download",
            decided_by=self.name,
        )


register(DedupeAgent())
```

For a corpus-level agent — a different way of choosing the run's vocabulary,
say, or one that clusters by project rather than by document type — implement
`SchemeProposer` instead:

```python
from organiser.delegates import (
    ProposedCategory, SchemeProposal, SchemeRequest, register,
)


class ProjectSchemeAgent:
    name = "project-scheme"

    def available(self) -> tuple[bool, str]:
        return True, "ready"

    def propose(self, request: SchemeRequest) -> SchemeProposal:
        # request.files is every file the rules could not place, as
        # FileSummary objects. request.existing_categories is what is
        # already on disk and in use — reuse those names where they fit.
        # Raise DelegateUnavailable if you cannot answer; never guess.
        return SchemeProposal(
            categories=[ProposedCategory("Thesis", "Chapter drafts and refs.")],
            reason="most of the loose files are chapters of one document",
            proposed_by=self.name,
        )


register(ProjectSchemeAgent())
```

Import the module once so `register()` runs — `manager.py` and `cli.py` both
import `organiser.classifier` and `organiser.scheme` for that side effect
alone, each guarded so a missing SDK degrades to "no delegate" instead of
taking the process down. Then select it with `Manager(delegate_name="dedupe")`
or `Manager(scheme_name="project-scheme")`. The manager itself needs no edit:
it resolves both delegates by name through the registry and only ever calls
`available()`, `decide()` and `propose()`.

There is no vocabulary constant to widen any more. To change what categories
are possible, either edit `EXTENSION_TABLE` / `FILENAME_RULES` in
`organiser/rules.py` (the fixed, deterministic half) or change the scheme
proposer's prompt or swap in a different `SchemeProposer` (the proposed half).
`organiser/categories.py` only decides whether a name is *safe*, never whether
it is *allowed*.

## Note on the rule table

**The original `file_organizer.py` was not in the workspace when this was
built.** The extension and filename tables in `organiser/rules.py` are a
reconstruction from the project brief, not a copy of the original mapping. They
are reasonable, but they are not authoritative.

Swapping the real ones in means editing `EXTENSION_TABLE` and `FILENAME_RULES`
in `organiser/rules.py` and **nothing else**. `EXTENSION_RULES` and
`rule_categories()` are both derived from `EXTENSION_TABLE` at import, and
there is no separate vocabulary constant to keep in sync any more — whatever
category names your original table uses become the rule pass's names, and are
handed to the scheme proposer as "already in use" automatically.

Until you do swap them in, `--no-rules` is the way to take a reconstructed
table out of the loop for a run without editing it — see
[Turning the rule table off](#turning-the-rule-table-off). That is a workaround
for the wrong table, not a fix for it: it trades a table that may mis-file
things for one model call per file.

## Known gaps / v1 scope

- **Top-level files only.** No recursion into subfolders, and existing category
  folders are never reorganised.
- **No watch mode.** Every run is something you start; nothing runs in the
  background.
- **No persistence of the proposed scheme between runs.** The vocabulary is
  invented fresh every time. Existing folder names are offered back to the
  proposer for reuse, which is a hint, not a guarantee — see
  [the drift trade-off](#the-trade-off-drift-honestly). A saved per-folder
  scheme is the obvious v2 feature and does not exist.
- **No dedupe agent.** The registry now demonstrably takes a second agent —
  the scheme proposer is one — but nothing detects duplicate files.
- **Never run against the live API.** No credentials on this machine, so the
  classifier and the scheme proposer have been exercised only through their
  failure paths. Their prompts and response schemas are unvalidated against
  real model output.
- **No undo from the web UI.** Reversal is CLI-only (`cli.py undo`); the UI can
  show the log but not act on it.
- **One folder at a time**, and no scheduling, profiles, or per-folder rule
  overrides.
