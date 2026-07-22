# Product Design

## Product definition

Immortal Memory is local personal memory infrastructure for AI users. Its job is not to draw a monitoring dashboard. Its job is to preserve important traces, turn them into evidence-backed and correctable understanding, prepare only relevant context for a task, and show the user what was used and what happened next.

The core promise is: **the system can remember and assist, but every important belief stays attributable, reviewable, private by policy, and reversible by version.**

## Product loop

```text
Remember -> Claim -> Living Self / Judgment -> Context -> Agent -> Outcome -> Review
```

1. **Remember** captures recoverable source traces without confusing the user with other speakers.
2. **Claim** expresses one evidence-backed assertion with scope, confidence, attribution, validity, and privacy.
3. **Living Self** composes confirmed Claims into a versioned model of the user.
4. **Judgment** preserves decision patterns together with situation, alternatives, result, and lesson.
5. **Context** lets the user preview exactly what a task will receive, remove allowed items, and compile a bounded pack.
6. **Outcome** binds real results back to the exact pack used.

## Seven-module dashboard

| Module | User question | Valuable result |
|---|---|---|
| Home | What changed today? | New memories, changed understanding, confirmations, latest use, blocking health |
| Memory | What evidence exists? | Search, filters, pagination, source detail, attribution, correction entry |
| Self | How does the system currently understand me? | Living Self sections, confidence, evidence, conflicts, version diff and restore |
| Judgment | What patterns can help this decision? | Judgment cards, similar cases, outcomes, failures, confirmation and correction |
| Use | What will the Agent actually receive? | Preview, exclusions, compiled pack, delivery state, explicit consume and outcome |
| Trust | Where might the system be wrong? | Attribution risks, weak evidence, privacy exclusions, stale projections, corrections |
| System | Is the product operational? | Collection, verified index, backup, scheduler, service, version, logs and maintenance |

Health is supporting evidence, not the product's main value. A healthy state stays compact. A blocking condition names the failed proof and the safe next action. Unknown is never rendered as healthy.

Open the product through `immortal-memory dashboard`, not a generated file. The legacy `dashboard-export` output is marked non-live, preventing historical HTML from being mistaken for a running system.

## Correction and version behavior

Original evidence is immutable. A user correction supersedes a Claim and creates a replacement while keeping the chain. The Living Self is rebuilt into a new immutable version. Version restore also creates a new descendant version, so audit history remains monotonic. Judgment and Context actions require expected versions, request IDs, idempotency keys, actor identity, and a reason.

The interface must distinguish pending, confirmed, rejected, superseded, stale, expired, consumed, failed, and unknown. It must not reduce these states to a green or red decoration.

## Privacy labels

Every modeled item uses one explicit label:

- `private`: may support local reasoning but its body never enters a Context Pack.
- `restricted`: may enter only when task scope and policy explicitly permit it.
- `context_safe`: may enter a scoped Context Pack, still subject to relevance, budget, and evidence rules.
- `public`: explicitly classified as public material, still subject to relevance, budget, and evidence rules.

The preview reports excluded counts and reasons without leaking excluded bodies. See [Privacy](./PRIVACY.md) for the full contract.

## Production acceptance

A production-ready installation proves all of the following:

- source capture is current and correctly attributed;
- JSONL source and SQLite index have identical unique ID sets;
- the validation receipt is fresh and binds the exact database generation;
- Claim, Living Self, Judgment, Context, and Outcome streams replay cleanly;
- an external portable backup passes strict hash and replay verification;
- all seven modules use real APIs and persist after refresh and service restart;
- desktop, mobile, keyboard, reduced-motion, CSP, origin, host, content-type, idempotency, version-conflict, and body-size checks pass;
- clean install works in an isolated `HOME` without access to an old vault;
- v1.0 compatibility and rollback are exercised, not merely documented.

Suggested health commands are `daily-status`, `backup-status --verify --max-age-hours 168 --json`, `health --max-age-hours 72`, `doctor`, `preflight`, and one real `agent-context` request.

## Product boundaries

The product can recall sourced evidence, mirror verified expression preferences, pre-evaluate routine decisions, draft, review, and explain its reasoning. It cannot claim to replace a person, make irreversible decisions without approval, expose raw private messages by default, collect an unverified account, or treat one generated inference as permanent truth.

The v1.0 `project` command is outside the seven-module authority. It was a
personal Obsidian projection bound to legacy index and card formats. A user may
keep an audited project projection as a Git-external local extension, but the
public runtime must not dynamically execute Python from the vault. Public
release packaging is likewise a repository concern, not a live-vault feature.
