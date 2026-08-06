# PRD: AI-Generated Summaries for NoteNest

**Status:** Proposal / Pre-decision
**Author:** Product team
**Last updated:** 2026-08-01

## Problem

NoteNest users accumulate long notes — meeting notes, research dumps, lecture captures — and tell us they struggle to find the "so what" when they return to them later. In our last quarterly survey, "hard to review long notes" was the second most-cited frustration (after search quality). Power users with 200+ notes report they rarely revisit anything longer than a screen.

Support tickets referencing "too long to reread" or "can't find the key point" have risen roughly 30% over the last two quarters, though absolute volume remains modest (~40 tickets/month).

## Proposed solution

Add a one-tap **"Summarize"** action to any note. It generates a short (3–5 sentence) summary plus a bulleted list of action items detected in the note. The summary is cached and shown collapsed at the top of the note; users can regenerate or dismiss it.

### In scope (v1)
- On-demand summarization of a single note
- Summary + extracted action items
- Cached result, manual regenerate
- English only

### Explicitly out of scope (v1)
- Auto-summarization of every note on save
- Cross-note / folder-level summaries
- Non-English languages
- Editing the summary inline

## Rough cost & effort

- **Engineering:** ~1 engineer-month for v1 (integration, caching, UI).
- **Inference cost:** estimated $0.004–$0.01 per summary depending on note length and model. At projected usage (~15% of active users summarizing ~5 notes/month), this lands around $2,500–$6,000/month at current scale.
- **Ongoing:** prompt maintenance, quality monitoring, handling user reports of bad summaries.

## Open risks

- **Accuracy on long/messy notes:** summaries of unstructured, stream-of- consciousness notes may miss or invent key points. Hallucinated action items are a particular concern — a fabricated "TODO" is worse than none.
- **Trust:** if early summaries are wrong, users may distrust the feature (and the product) permanently. First impressions are hard to reverse.
- **Value concentration:** benefit skews toward power users with long notes; the median user's notes are short enough not to need summarizing.
- **Cost scaling:** if adoption is much higher than projected, inference cost could outpace the plan.

## Success metrics (proposed)

- ≥20% of active users try Summarize within first month
- ≥40% of those become repeat users (summarize again within 2 weeks)
- Summary "thumbs down" rate <15%
- No measurable increase in churn attributable to the feature