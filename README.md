# odoo-reviewer

Sandboxed, human-in-the-loop AI code review for GitHub pull requests — built for Odoo.

`ai_review` pulls a PR (diff, existing comments, the linked Odoo task, the touched modules'
tests, the changed files and their callers) and hands it to **Claude running fully sandboxed**
— no GitHub, no MCP, no tools. A skeptical critic pass filters the findings; you review and
edit them, then it posts them back as a **draft** GitHub review that *you* submit.

> **Visual overview:** open [`docs/index.html`](docs/index.html), or enable GitHub Pages on the
> `/docs` folder for a hosted one-pager.

## Install

Three external dependencies:

- [`gh`](https://cli.github.com/) — GitHub CLI, run `gh auth login`
- [`claude`](https://claude.com/claude-code) — Claude Code, log in once (or set `ANTHROPIC_API_KEY`)
- An **Odoo API key** for task enrichment

```bash
pip install -r requirements.txt     # optional: rich (prettier output), stdlib otherwise
cp .env.example .env                 # fill in ODOO_* (or export them in your shell)
python -m ai_review doctor           # verify gh / claude / Odoo — should print all ✓
```

## Use

One shot, from just the PR link:

```bash
python -m ai_review process https://github.com/odoo/odoo/pull/123456
```

`process` runs **fetch → review → push** and still posts a **draft** you submit yourself
(it asks before posting; add `--yes` to run unattended).

Or step by step, inspecting/editing between stages:

```bash
python -m ai_review fetch  123456 -R odoo/odoo
python -m ai_review review 123456 -R odoo/odoo     # → review.json / review.md (edit these)
python -m ai_review push   123456 -R odoo/odoo --dry-run   # preview the payload
python -m ai_review push   123456 -R odoo/odoo             # post the draft review
```

`<pr>` = a PR URL, `owner/repo#123`, or a bare number with `-R owner/repo`.

## Cheat sheet

| Command         | What it does                                              |
| --------------- | -------------------------------------------------------- |
| `doctor`        | Check `gh` / `claude` / Odoo setup (preflight)           |
| `fetch <pr>`    | Pull the PR + all context into `bundle.json`             |
| `review <pr>`   | Run the AI review → `review.json` + `review.md`           |
| `push <pr>`     | Post a **draft** GitHub review (edit `review.json` first) |
| `run <pr>`      | `fetch` + `review` (stops before push)                   |
| `process <pr>`  | One shot: `fetch` + `review` + `push`                     |

```
review:  --strategy whole|per-task|per-commit   --model NAME   --workers N
         --no-verify   --min-confidence 0-1 (0.6)   --incremental   --force
         --context pstech|rnd   (default pstech — see below)
fetch:   --no-tests   --no-context
push:    --dry-run   --yes   --separate-summary   --submit COMMENT|APPROVE|REQUEST_CHANGES
```

## Review context — PS Tech by default

The odoo-review guidelines target contributions to Odoo **core**. Most PS Tech work is
**customizations on top of Odoo**, so the review is recalibrated for that, in priority order:

1. **Maintainability** (incl. upgrade-safety — prefer `_inherit`/`super()`/xpath over editing core)
2. **Correctness** against the linked task/SOW
3. **Odoo-like, minimal code** — favor standard config / Studio / server actions / existing modules
   over bespoke Python (*the best code is no code*)
4. **Performance** at real data volume
5. **Security**

It also **challenges the business need**, **suggests better data modeling**, and ends every review
with a **Footprint & ROI** verdict. The full odoo-review skill *and* its sibling guidelines
(odoo-guidelines / odoo-web-guidelines / odoo-security + their rule files) are fetched at the PR's
base branch as the baseline. Pass `--context rnd` for an actual core contribution.

## How it works

- **The AI is sandboxed.** `claude` runs with no MCP and every tool disabled, from an empty
  directory. Only this script touches GitHub (`gh`) and Odoo (XML-RPC).
- **Context beyond the diff.** The linked `[task_id]`'s Odoo description + chatter, the touched
  modules' tests, the full changed files, target-branch callers, and the odoo-review house-rule
  skill **plus all its sibling guidelines** — all fetched by the script at the PR's base branch.
- **Signal over noise.** A critic pass refutes each finding and assigns a confidence; weak,
  refuted, or duplicate findings are suppressed. Every posted comment is anchored to a real diff line.
- **Human → AI → human.** `review` won't run until a human has reviewed; the AI adds on top; you
  edit `review.json` and submit the draft. Nothing reaches the PR without your click.
- **Hardened.** PR-supplied text is stripped of invisible characters and fenced as untrusted data.
