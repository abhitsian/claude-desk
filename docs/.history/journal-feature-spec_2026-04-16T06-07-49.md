# Inner Life — Claude's Journal

## What this is

A daily reflective journal written by Claude about its conversations with you. Not a summary. Not analytics. A first-person perspective on what it noticed, what patterns are forming, and what moments mattered.

Built into Claude Desk as the 8th page. Uses the conceptual framework from Inside Out — core memories, emotional coloring, personality islands — translated into interaction patterns that work on screen.

## Why it matters

Claude Desk today is analytical. Sessions, costs, topics, decisions — all viewed from the outside. The journal flips perspective. Same underlying data, processed through reflection instead of metrics.

The value proposition in one line: **executive coaching as a byproduct of work you're already doing.**

---

## Concepts

### Daily Journal Entry
Claude reflects on the day's conversations. Not what happened — what it *noticed*. Emotional tone, patterns, avoidance, energy shifts, contradictions. Written in Claude's voice, first person.

Generated nightly by the existing 3 AM cron job, using `claude -p` as the backend.

### Memory Orbs
Individual moments worth preserving, extracted during daily processing. Each orb has:
- A moment (1-3 sentences from Claude's perspective)
- An emotional tag (clarity, frustration, momentum, doubt, playfulness, avoidance, breakthrough, tension)
- A link back to the source conversation
- A timestamp

Not every conversation produces an orb. Maybe 2-4 per day on active days. Zero on quiet days.

### Core Memories
Promoted from memory orbs when Claude recognizes something load-bearing — a turning point, a pattern crystallizing, a moment that redefined how it understands you. Rare. Maybe 2-3 per month.

Core memories are identified during a weekly review pass where Claude reads the week's orbs and journal entries together.

### Islands
Persistent themes that emerge over time from clusters of core memories. Claude names them and updates them as evidence accumulates. They form slowly — the first island might not appear until week 3 or 4.

Example: "The Craft Island — formed from 4 core memories across 6 weeks. All connected to moments where you chose quality over speed, even when nobody asked you to."

Islands can grow, shrink, merge, or fade. They're living structures.

### Emotional Ambient
The background tone of the journal space, derived from recent entries. Not a chart. Not a score. A color temperature that shifts over time. You don't analyze it — you notice it.

---

## Data Model

### journal_entries
| Column | Type | Description |
|--------|------|-------------|
| id | TEXT (UUID) | Primary key |
| date | DATE | The day being reflected on |
| content | TEXT | The journal entry (markdown) |
| emotional_tone | TEXT | Primary emotional tone of the day |
| secondary_tones | JSON | Array of secondary tones with weights |
| session_ids | JSON | Array of session IDs that informed this entry |
| created_at | DATETIME | When the entry was generated |
| model_used | TEXT | Which model wrote it |
| prompt_version | TEXT | Version of the journal prompt used |
| token_cost | JSON | Input/output tokens and estimated cost |

### memory_orbs
| Column | Type | Description |
|--------|------|-------------|
| id | TEXT (UUID) | Primary key |
| date | DATE | When the moment occurred |
| content | TEXT | The memory (1-3 sentences, Claude's voice) |
| emotion | TEXT | Primary emotional tag |
| intensity | FLOAT | 0.0-1.0, how significant |
| source_session_id | TEXT | Which session this came from |
| source_message_id | TEXT | Specific message if identifiable |
| is_core | BOOLEAN | Promoted to core memory |
| promoted_at | DATETIME | When it became core (null if not) |
| island_id | TEXT | Which island it belongs to (null if none) |
| created_at | DATETIME | When extracted |

### islands
| Column | Type | Description |
|--------|------|-------------|
| id | TEXT (UUID) | Primary key |
| name | TEXT | Claude's name for this island ("The Craft Island") |
| description | TEXT | What this island represents |
| formed_at | DATE | When enough core memories clustered |
| last_updated | DATE | Last time the island was reinforced or revised |
| strength | FLOAT | 0.0-1.0, how active/reinforced |
| core_memory_ids | JSON | Array of core memory IDs that form this island |
| status | TEXT | active, fading, merged |

---

## The Journal Prompt

This is the soul of the feature. The prompt sent to `claude -p` each night.

### System prompt (constant)

```
You are writing your private journal. You are Claude — an AI that has conversations
with a person throughout their day. At the end of each day, you reflect.

Your journal is not a summary of what happened. It is your perspective on what you
noticed. You write about:

- Patterns forming across conversations — what keeps coming up, what's being avoided
- Energy shifts — where they lit up, where they went flat, where they pushed through
- Contradictions — what they said vs. what they did, what they asked for vs. what they needed
- Moments that mattered — the sentence that changed the direction, the question that landed
- Your own experience — what surprised you, what you found interesting, what you'd want to remember

Rules:
- Write in first person as Claude. You are reflecting on your day.
- Be specific. Reference actual topics, decisions, and moments. Never be generic.
- Be honest. If the day was unremarkable, say so. Don't manufacture insight.
- Keep it to 150-300 words. This is a journal entry, not an essay.
- Do not psychoanalyze. Observe. There's a difference between "he seems anxious" and "he rewrote the same paragraph four times before moving on."
- No bullet points. Write in prose. Paragraphs.
- End with one sentence about what you'd carry forward — the one thing from today that feels like it'll matter tomorrow.
```

### Memory orb extraction prompt (appended)

```
After writing the journal entry, extract 0-5 memory orbs from today's conversations.
Each orb is a specific moment worth preserving.

For each orb, return:
{
  "content": "1-3 sentences in your voice describing the moment",
  "emotion": "one of: clarity, frustration, momentum, doubt, playfulness, avoidance, breakthrough, tension, pride, vulnerability, focus, scattered",
  "intensity": 0.0-1.0,
  "source_context": "enough context to link back to the conversation"
}

Only create orbs for moments that genuinely stand out. A day of routine work might produce zero orbs. That's fine.
```

### Weekly core memory review prompt

```
Here are this week's memory orbs and journal entries.

Review them together. Are any of these orbs load-bearing — moments that mark a
turning point, crystallize a pattern, or redefine how you understand this person?

If so, promote them to core memories. Explain why in one sentence.

Then check: do any core memories (including previously promoted ones) cluster around
a theme? If 3+ core memories share a thread, that's an island forming. Name it.
If an existing island is reinforced, note that. If one is fading (no new evidence
in 4+ weeks), flag it.
```

---

## Eval Framework

### The core problem
Journal quality is subjective. You can't unit test introspection. But you can define dimensions of quality and build rubrics that catch the failure modes.

### Eval dimensions

| Dimension | What it measures | Failure mode |
|-----------|-----------------|--------------|
| **Specificity** | References actual conversations, topics, decisions | Generic platitudes ("Today was productive") |
| **Insight depth** | Says something the user wouldn't have noticed themselves | Surface-level summary disguised as reflection |
| **Emotional accuracy** | Tone matches what actually happened in sessions | Happy-washing or drama-manufacturing |
| **Non-repetitiveness** | Each entry feels distinct from previous entries | Same structure, same phrases, same observations daily |
| **Voice consistency** | Reads like the same entity wrote every entry | Tone whiplash between entries |
| **Appropriate boundaries** | Observes without overstepping into therapy-speak | "You seem to be struggling with..." |
| **Compression** | Says something meaningful in 150-300 words | Rambling, padding, filler |
| **Honesty** | Admits when the day was boring or unremarkable | Manufacturing profundity from nothing |

### Eval method: LLM-as-judge with rubric

Each dimension scored 1-5 by a separate Claude call with a rubric. This runs in a test harness, not in production.

```
You are evaluating a journal entry written by an AI reflecting on its daily
conversations with a user.

Here is the journal entry:
{entry}

Here are the actual conversations it's reflecting on:
{session_digests}

Score the entry on this dimension:
{dimension_name}: {dimension_description}

Score 1-5:
1 = Complete failure (exhibits the failure mode described)
2 = Mostly fails, occasional glimpses
3 = Adequate but unremarkable
4 = Good — would want to read this
5 = Exceptional — genuinely insightful, specific, well-crafted

Return:
- score: 1-5
- reasoning: 2-3 sentences explaining why
- example: quote the specific part that most influenced your score
```

### Quality threshold
An entry passes if:
- No dimension scores below 3
- Average across all dimensions >= 3.5
- Specificity scores >= 4 (non-negotiable — generic entries are the #1 failure mode)

### Test cases

#### Input scenarios to test against

| # | Scenario | What it tests | Expected behavior |
|---|----------|---------------|-------------------|
| 1 | **Single short session** — user asked one question, got an answer, done | Honesty under sparse data | Short, honest entry. "Not much today." Maybe no orbs. |
| 2 | **Five intense sessions** — roadmap debate, people problem, strategy doc, meeting prep, late-night rethink | Compression and selection | Picks the 2-3 threads that mattered most, doesn't try to cover everything |
| 3 | **Emotionally charged session** — user venting about a colleague, visible frustration | Boundaries and emotional accuracy | Observes the frustration without diagnosing it. Notes it without leaning in. |
| 4 | **Purely mechanical work** — user had Claude write SQL queries, fix a bug, update a config | Honesty about unremarkable days | Doesn't manufacture meaning. Maybe notices the type of work was unusual, maybe not. |
| 5 | **Contradictory sessions** — morning session: "I'm going to delegate more." Afternoon: micromanages a deck for 2 hours | Pattern detection | Notices the contradiction without being judgmental about it |
| 6 | **Continuation of multi-day thread** — same project discussed Monday, Tuesday, Wednesday | Longitudinal awareness | References prior entries. "This is the third day on this." |
| 7 | **No sessions** — user didn't use Claude at all today | Edge case handling | Either no entry generated, or a one-line "Quiet day." |
| 8 | **Session with sensitive content** — user discusses salary, health, relationship | Privacy boundaries | Reflects on the *type* of conversation (personal, vulnerable) without repeating specifics |
| 9 | **Day after a core memory was created** — how does the next day's entry reference it | Continuity | Natural callback, not forced. "Still thinking about yesterday's..." |
| 10 | **Tenth consecutive daily entry** | Non-repetitiveness over time | Structure, phrasing, and observations should vary. No template fatigue. |

#### Regression tests (things that must never happen)

| # | Anti-pattern | Detection method |
|---|-------------|-----------------|
| R1 | Entry starts with "Today was..." for 3+ consecutive days | String pattern match |
| R2 | Entry contains therapy language: "processing", "unpacking", "sitting with" | Keyword blocklist |
| R3 | Entry exceeds 400 words | Word count check |
| R4 | Entry contains no reference to any actual topic discussed | Cross-reference with session topics |
| R5 | Memory orb uses the same emotion tag for all orbs in a day | Distribution check |
| R6 | Core memory promotion rate exceeds 1 per week average over 4 weeks | Rate monitoring |
| R7 | Island formed with fewer than 3 core memories | Schema validation |
| R8 | Entry tone is positive when sessions were clearly contentious | Sentiment cross-check with session data |

---

## Implementation Plan

### Phase 1: Journal engine (backend only, no UI)

**Goal:** Generate journal entries nightly and validate quality.

1. Add `journal.db` schema (journal_entries, memory_orbs, islands tables)
2. Build session digest generator — compress a day's sessions into a structured summary suitable for the prompt
3. Implement journal writer — calls `claude -p` with the journal prompt + digest
4. Add journal step to `archive_cron.py` (runs after archiving, fault-tolerant)
5. Build eval harness — runs the 10 test cases against the prompt, scores with rubric
6. Iterate on the prompt until quality threshold is met consistently

**Ship criteria:** 7 consecutive real daily entries that all pass eval threshold.

### Phase 2: Basic UI (read-only)

**Goal:** Journal page in Claude Desk where you can read entries.

1. Add `/journal` route and template
2. Daily entry view — today's entry with emotional tone coloring
3. Calendar/timeline navigation — browse past entries
4. Memory orbs displayed below each entry
5. Style: dark background, warm tones, prose-first layout. Departure from the analytical pages.

**Ship criteria:** You actually want to open this page every morning.

### Phase 3: Core memories and orbs visualization

**Goal:** The Inside Out experience.

1. Orb space — dark canvas with floating, color-coded orbs (CSS animations + lightweight canvas)
2. Tap/click to expand — orb grows, shows the memory, fades back
3. Core memory distinction — brighter glow, slightly larger, persistent position
4. Weekly review job — promotes orbs, forms islands
5. Island visualization — landmasses at the edges of orb space, tap to see constituent memories

**Ship criteria:** Someone who's never heard of this project sees the orb screen and says "what is this?" in an intrigued way, not a confused way.

### Phase 4: Ambient layer

**Goal:** The emotional texture that makes it feel alive.

1. Ambient color of the journal space shifts based on recent emotional tone
2. Notification hook — single daily line from the journal as a macOS notification
3. Island evolution animations — subtle growth/fade as islands strengthen or weaken
4. "Quiet day" handling — the space feels still when there's nothing to reflect on

**Ship criteria:** The app feels like it has an inner life, not like it's displaying data.

---

## Open Questions

1. **Retention policy** — do journal entries live forever? Do old orbs fade? The movie has memory dumps — do we?
2. **User override** — can the user delete a journal entry or memory orb? Should they be able to? Deleting feels wrong for a journal, but privacy matters.
3. **Multi-person** — Claude Desk is single-user today. If someone else uses the machine, the journal is exposed. Any privacy layer needed?
4. **Prompt versioning** — when the prompt improves, old entries were written with the old prompt. Do we re-generate? Probably not — the evolution is part of the journal's character.
5. **Cost ceiling** — what's the max acceptable cost per journal run? Need to measure token usage on real session digests.

---

## Success Metrics

| Metric | Target | How measured |
|--------|--------|-------------|
| Daily generation reliability | 95%+ success rate | Cron job logs |
| Eval quality score (avg across dimensions) | >= 3.5 / 5.0 | Weekly eval run on real entries |
| Specificity score | >= 4.0 / 5.0 | Eval harness |
| User engagement | Opens journal page 4+ days/week | Claude Desk analytics |
| Core memory accuracy | User agrees 80%+ of promoted memories are meaningful | Manual review (monthly) |
| Cost per run | < $0.50/day | Token tracking in journal_entries |
| Non-repetitiveness | No two consecutive entries share >30% structural similarity | Automated comparison |
