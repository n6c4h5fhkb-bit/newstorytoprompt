# StoryForge Lite — Platform Design Document (v2.7)

Oct 4, 2026 · @Someone

## 0. What changed in v2

v2 keeps the v1 architecture and fixes the production risks found in two external reviews, mostly by tightening protocols rather than adding stages. The reviewer set shrinks from nine roles to seven, and no user-facing concept is added. v2.1 adds contract fixes from a third review: per-episode ledger binding, rollback that never touches money records, image approval by content hash, per-unit cast coverage, and an M1 import path. v2.2 fixes issues from a full self-review: one ledger file per episode, staleness limited to what a job actually used, media stored by content hash outside git, clip deviations routed through script notes, export only after reconstruction on real images, and a minimal clip record in M1. v2.3 applies a fourth review: references only for what is visible, rules for worn items, extras and on-screen text, cards that never default silently, a defined baseline for the kill rule, and a smaller M1. v2.4 follows the production order used by existing platforms (script, art direction, assets with masters and children, storyboard, per-shot asset assignment), keeps off-screen characters as references by default with the user free to remove them, and separates voices from sound effects. The plan now includes a one-line-per-episode beat sheet, the cheapest place to fix pacing. v2.5 adopts the prompt structure seen across 303 Seedance 2.0 prompts by experienced creators: a code-written reference manifest with a role per reference, a verbatim style-lock block, a palette card, voice samples as audio references, a continuation line, and specific negatives in one final section. v2.6 adds the story rules the audience rubric encodes (section 5), drawn from two short-drama craft books and adapted for free, retention-driven platforms and for AI video. v2.7 makes the platform stop at prompts: it delivers each unit's final Seedance prompt with a reference mapping of named placeholders (@主角\_母图, @角色A\_声音, @紧张氛围音乐) and never generates, uploads, downloads or reviews media; reconstruction works from text only; and episodes are planned as 3–5 minute units cut from the story across chapters, each ending on a typed hook.

| Change | Risk it fixes |
| --- | --- |
| Scene fidelity reviewer compares the storyboard with the script scene; code checks every scene's cast line | A character (e.g. 妖商) missing from storyboard and assets passes every later check |
| Reconstruction reviewer reads the final prompt blind | A prompt that matches the storyboard but is unclear to the video model |
| Key beats are checked against original source passages | A misread in a cheap summary spreads through the whole adaptation |
| Episodes are written in order from a story state ledger | Parallel episodes contradict who knows or holds what |
| The plan stays canonical; a clip deviation enters continuity only by explicit choice | A generation error silently becomes story |
| User decisions and model costs are written to project files | The job database cannot be rebuilt; choices and costs are lost |
| Jobs bind their input versions at start; stale results never overwrite newer work | A late job overwrites a newer edit |
| Estimates (length, speech fit, negatives, source overlap) become warnings | False failures cause retry loops and needless cards |
| Over the reference limit, the storyboarder decides; code never drops a reference | A resting prop that carries continuity is deleted |
| Findings may quote the source to report an omission; blockers are never capped | Missing elements cannot be reported; a serious issue is dropped |
| Best-of-3 only for key moments; escalated cards wait for the user | Extra waiting and clicks; expensive decisions made by timeout |
| Codex gets this design as background plus one milestone spec at a time | Codex builds future milestones early |
| 16:9 only; episode length derived from content | Unneeded format support and arbitrary length rules |
| Failure-injection cases gate every milestone | Tests pass while real failure modes go uncaught |

## 1. Purpose, scope and principles

StoryForge Lite turns a novel into ready-to-generate Seedance packages for fast-paced novel-promotion short dramas, 16:9 horizontal only, with the user making a handful of choices per project. The model makes creative and analytical judgments; code runs the workflow, owns the data, and checks everything that can be checked mechanically.

**In scope (v1):** novel import, plot breakdown, cutting for the target audience, differentiation, hook selection and amplification, episode scripts, storyboard, asset list and image prompts, reference assignment, Seedance prompts, and final Seedance prompts with their reference mapping. The platform stops there.

**Out of scope (v1):** original screenplays from scratch, film or series formats, 9:16 output, version branches, generating images, audio or video, reviewing clips, final editing and publishing, multi-user accounts.

### Success metrics

Every feature is judged against these numbers on real episodes. Targets are set after the first baseline episode.

| Metric | What it measures | Direction |
| --- | --- | --- |
| User minutes per finished episode | Usability | Down |
| First-pass usable clip rate | Prompt quality, from your optional feedback | Up |
| Generations per unit | Generation cost, from your optional feedback | Down |
| Cards per episode | Automation | Toward 0 after episode 1 |
| Days from import to first finished episode | Speed to production | Down |
| Code a coding agent must read for a typical change | Maintenance cost | Small, stable |

### Design principles

1. **Model judges, code executes.** No creative rule lives in code; no procedure or state rule lives in a prompt. Word lists and thresholds that checks apply are data in skills/ and config.yaml, not code.
2. **Every model call is stateless.** Code builds each call's context from files. No long-running agent sessions.
3. **Review is independent by construction.** Reviewers never see the writer's reasoning. Blind reviewers also never see the intent (script, storyboard); fidelity reviewers do see the source, because their job is to check that nothing was lost. packets.yaml defines which is which.
4. **The plan is canonical.** Generated output never silently changes story or continuity. A change seen in a generated clip becomes canon only when you file a continuity note.
5. **Story, look, assets, then shots.** bible.md holds story entities; style.md fixes the look; production assets (masters and children) are extracted from the locked script under that style, before storyboarding, so images are made while shots are designed. The storyboard may request a missing asset but never improvises one.
6. **Files are the source of truth.** Content, user decisions and model-call costs are files in the project folder. SQLite holds the job queue and open cards and is backed up daily; anything involving money or a user decision is also written to the logs.
7. **The user sees cards and previews, not documents.** Decisions reach the user only through three fixed checkpoints and the escalation rule (section 8).
8. **Fix protocols, not stages.** Fix in the cheapest layer first: a rubric line or example, then a check, and only as a last resort a new stage. Five user-facing concepts (Project, Episode, Card, Package, Feedback); adding one means removing one.
9. **Hard checks only for unambiguous facts.** Estimates (length, speech fit, overlap) are warnings for reviewers and the user, never automatic rewrites.
10. **Nothing ships untested.** From M1 on, every milestone must catch its failure-injection cases and run one real episode end to end.

## 2. Overall architecture

One small local app runs both pipelines on a single backbone: code orchestrates, models judge, and plain files hold all content.

&#91;embedded content: architecture · 4 layers\]

The user works only in the Inbox or the CLI. Pipelines A (screenwriting) and B (directing and production) are two stage lists in stages.yaml on one backbone. They share the project folder and the Inbox, and connect through one contract: locked episodes (bible.md, each episode's script, and its ledger files). Episodes lock one at a time, so B can start on episode 1 while A writes episode 2.

Every stage follows the same loop:

1. runner picks the next stage and target and records the fingerprints of all its inputs.
2. packets builds the context from project files, following the role's rules in packets.yaml.
3. llm calls the model with the role card from skills/, stateless.
4. checks run: a hard failure retries with the specific error; warnings are attached for reviewers.
5. Required reviewers run with their own packets; findings go back to the writer.
6. store writes the result only if its inputs are unchanged since the job started (otherwise it keeps a stale candidate), then snapshots the project.
7. cards raises a decision only if a fixed checkpoint or the escalation rule calls for one.

### Workflow at a glance

&#91;embedded content: end-to-end workflow · UML activity diagram with swimlanes\]

Read it top to bottom. Filled blue boxes are the points where you act; everything in the code, model and reviewer lanes runs on its own. Two-headed arrows mean a reviewer's findings go back to the writer, at most two rounds before a card. Episodes flow from pipeline A into pipeline B one at a time as they lock, so production of episode 1 starts while episode 2 is still being written. The platform's work ends at B9 delivery; everything in the last band happens in your own tools.

## 3. Module boundaries

Each module owns one job and is forbidden from doing its neighbours' jobs. Methodology lives in skills/ as text; no code module contains a creative rule.

| Module | Owns | Must not |
| --- | --- | --- |
| runner | Stage order, job queue, parallel units, retries, resume after crash, binding input versions at job start | Contain prompt text or creative rules |
| packets | Building each role's context from files per packets.yaml, including source passages for key beats; enforcing must-not-see rules | Let any agent choose what another agent sees |
| llm | Model calls, structured output, schema validation, cost logging, caching by input fingerprint | Keep conversation history or sessions |
| checks | Hard checks and warnings (section 9) | Call a model, or drop or rewrite content itself |
| refs | Choosing references per unit and assigning their placeholders, applying the user's per-unit edits, reporting when a unit exceeds the model's limits | Write prompt text, or remove a reference on its own |
| cards | Creating, storing, defaulting and resolving cards; the escalation rule | Make the decision itself |
| delivery | Writing delivery folders: prompt files, reference mappings, asset sheets, overlay lists | Generate, upload or download any media |
| store | Project files, append-only logs, snapshots, staleness tracking, refusing stale writes | Interpret content |
| ui | Projects, Inbox, Episode and Settings screens | Hold business logic |
| skills/ (text) | Role cards, reviewer cards, rubric, style guide, templates, model cards | Contain procedure, state or file rules |

Two boundaries matter most. packets is the only place that decides what a model sees, which keeps reviewers independent. refs is the only place that assigns placeholders to units, which makes it impossible for a prompt to cite an asset missing from its mapping.

## 4. Concepts and data structures

The user works with five concepts; everything else is an internal file. Only storyboard JSON, assets.csv and package manifests are machine-structured; the rest is Markdown or append-only logs that both people and models can read.

| User concept | Meaning |
| --- | --- |
| Project | One novel, one folder |
| Episode | The unit of progress shown on screen |
| Card | A decision only the user can make |
| Package | A ready-to-use unit: final prompt plus its reference mapping |
| Feedback | An optional note per unit after you generate: worked first time, needed a redo, or a continuity change |

### bible.md — story entities

Canonical names and style, injected into every packet that needs them. It lists story entities only; production variants live in assets.csv. Jobs fingerprint only the bible entries they use, so adding a new character never marks unrelated work stale. 系统 and 旁白 are reserved speakers that need no entry; unnamed roles such as 路人 or 侍卫 are listed once under Extras. Each character's voice column is its voice profile; the system and narrator voices are defined once under Voices. Each voice also has a placeholder (@林恒\_声音) mapped to the units where that speaker talks; you attach the real voice sample in your video tool.

```markdown
# Bible
## Characters
| name | role | look | voice | source name |
| 云清禾 | female lead | 灰白破裙, 青绿眼睛 | cold, terse | 原名X |
## Extras
路人, 侍卫, …
## Locations
| name | look |
## Voices
| speaker | voice profile |
| 系统 | 冷静的电子女声，无情绪起伏 |
## Setting
## Differentiation card
## Visual style
```

### ledger.md — story state between episodes

What must stay true across episodes. It is stored one file per episode: ledger/ep00.md is the starting state from the plan, and ledger/epNN.md is the state after episode NN. Episode NN's ledger\_in is the previous file and its ledger\_out is its own file. Separate files keep fingerprints narrow: finishing episode 10 never marks episode 1's work stale. Episodes are written in order from the previous episode's ledger\_out; pipeline B reads only the locked ledger\_in of the episode it is working on, never a later state.

```markdown
# ledger/ep03.md (state after EP03)
- knows: 林恒 knows 云清禾's talent; 云清禾 does not know about the system
- holds: the shackles stay in 妖商's shop
- relations: 云清禾 obeys the seal, hostile
- open setups: loyalty -100 must pay off
```

### Episode script — episodes/epNN.md

One fixed format, so code can parse scenes, cast, dialogue and estimated length. Every scene lists its cast, including present characters who never speak.

```text
# EP03 标题
hook: 一句话描述开场钩子
cliffhanger: 一句话描述结尾悬念

## S01 妖商店 · 日 · 内
出场：林恒、云清禾、妖商
△ 动作描写一行
林恒：跟我走。
云清禾：你给我结了奴灵印，我也不会——
林恒（心声）：能造灵石？
系统：当前每日产出：零。
【音效】腕镣落地，铁链一声轻响
【环境】门外市集低声嘈杂
```

Every line has one kind. Voices and sounds are kept apart because they are heard by different people and prompted differently:

| Line | Kind | Who hears it | How the prompt treats it |
| --- | --- | --- | --- |
| 出场：… | Cast | — | Everyone present in the scene |
| △ … | Action | — | Visible action |
| 名字：台词 | Speech | Everyone in the scene | Lip-synced dialogue in quotes |
| 名字（画外）：台词 | Off-screen speech | Everyone in the scene; the speaker is out of frame | A voice from a direction, no lip sync on screen |
| 名字（心声）：台词 | Thought | Only the audience | The character's own voice, lips closed |
| 系统：台词 | System voice | Only the host character and the audience | The fixed system voice from Voices, no source in the scene, nobody else reacts |
| 旁白：台词 | Narration | Only the audience | The narrator voice, outside the story |
| 【音效】描述 | Sound effect | Everyone in the scene | A sound event tied to an action, at its moment |
| 【环境】描述 | Ambience | Everyone in the scene | A continuous background bed for the scene |

Music is a project setting: either none (有音效，无音乐) or a music cue referenced by placeholder, such as @紧张氛围音乐. Either way, you can still replace the music in editing.

### storyboard/epNN.json

```json
{
  "episode": 3,
  "units": [{
    "id": "ep03_u02", "scene": "S01", "seconds": 12,
    "assets": ["char:云清禾", "char:林恒", "char:妖商", "loc:妖商店@日"],
    "offscreen": [], "absent": {}, "chain_from_previous": false,
    "carry_in": "云清禾双腕空；腕镣在桌角旁地上；林恒双手空",
    "carry_out": "云清禾已出门；林恒在桌角旁",
    "shots": [{
      "id": "s1", "seconds": 2, "size": "中景", "camera": "固定",
      "on_screen": ["林恒", "云清禾", "妖商"],
      "positions": "林恒画面左；云清禾画面右，桌角旁；妖商在桌后",
      "action": "她盯着门口不动，林恒看向她",
      "dialogue": [{"who": "林恒", "kind": "speech", "line": "跟我走。"}],
      "sfx": [{"what": "腕镣落地轻响", "at": 1.2}],
      "ambience": "门外市集低声嘈杂",
      "overlays": []
    }]
  }]
}
```

Every unit accounts for every member of the scene's cast line: on\_screen in a shot, in offscreen (present but not shown), or in absent with a reason (for example 已离开). Code checks that each unit accounts for everyone; scene fidelity judges whether each absence is legitimate. carry\_in and carry\_out are the planned state; they change only through a continuity note (section 6, B10). A unit's assets list every entity present in the scene during the unit, on screen or off (section 6, B6); the user can remove references in the assignment view. dialogue kind is one of speech, offscreen, thought, system or narration, and sfx and ambience follow the sound kinds above. overlays lists on-screen text (system panels, captions) with its time in the unit: Seedance renders a blank panel and the text is added in post. chain\_from\_previous marks a continuity-critical join (section 6, B6).

### assets.csv — production assets

Created from the locked script under style.md, before storyboarding; the storyboard can request additions.

| Column | Example | Notes |
| --- | --- | --- |
| id | char:云清禾@戴镣 | type:name for masters, @variant for children |
| type | character / location / prop / ui / voice / music / layout | A prop is never baked into a master. A child may include a worn item (restraints, armor) when the character wears it across several units; the prop's own image is used only when the item is off the body. ui covers recurring interface elements such as a blank system panel. voice is an audio sample per speaker, including the system voice. layout is an optional image showing where people stand in a scene, used when blocking matters. music is a mood or theme cue, used only when the project allows music |
| name | 云清禾 | Must match bible.md |
| parent | char:云清禾 | Empty for masters |
| what\_changed | 双腕戴黑铁腕镣，短链相连 | Children only |
| image\_prompt | … | From the asset prompt worker |
| placeholder | @云清禾\_戴镣 | Unique name used in prompts; you attach the real asset in your video tool |
| status | needed / described / approved | Set by code |

### Master and child assets

A master (母资产) is the identity anchor of one entity: the look it has when the story first meets it, generated once and approved. A child (子资产, 派生图) is a variant of exactly one master for a different state; it is generated from the master image so identity carries over, and it describes only what changed. Every unit uses exactly one version of each entity.

| Entity | Master | Children, only when the look really changes |
| --- | --- | --- |
| Character | Turnaround sheet: front, side, back and a face close-up, neutral expression, plain background, in the default costume (e.g. 张大彪（屠夫装束）) | Outfit change, injury or dirt, a transformation (女人（旗袍形态） → 女人（纸人形态）), a worn item kept across several units (云清禾@戴镣) |
| Location | Empty establishing view with a fixed layout, 16:9 | Time of day, weather, damage or a changed set (妖商店@夜) |
| Prop | Isolated, several angles | A changed state: opened, broken, bloodied |
| ui | A blank interface element in the project style (system panel) | Rarely needed |

Never a child: expressions, poses, gestures, camera angles, or a character holding a prop. Those are directed in the video prompt, using the master or child plus the prop's own image as references. Placeholders follow one pattern, @名称\_版本: @云清禾\_母图, @云清禾\_受伤, @客厅\_夜景, @道具\_腕镣, @林恒\_声音, @系统\_声音, @紧张氛围音乐, @色卡.

### Package — delivery/ep03/

```text
delivery/ep03/
  u02.md        final prompt + reference mapping for one unit
  assets.md     every placeholder this episode uses, with its description and asset prompt
  overlays.md   on-screen text to add in editing: unit, start, end, text

u02.md
## EP03 · U02 · 12s · 16:9
### 参考映射
@云清禾_母图    角色形象   灰白破裙，青绿眼睛，双腕空    画内
@林恒_母图      角色形象   墨黑短发，深色粗布衣          画内
@妖商_母图      角色形象   坐在桌后                      画外右侧
@妖商店_日景    场景       木桌、门帘、货架              —
@道具_腕镣      道具       打开的黑铁腕镣，短链          画内
@色卡           色卡       项目色板                      —
@林恒_声音      音色       年轻男声，冷                  说话
@云清禾_声音    音色       年轻女声，克制                说话
### 提示词
(the six-part prompt from section 6, B7)
```

### Feedback — logs/feedback.jsonl

Optional, one line per unit you choose to report: worked first time, needed a redo (with a short reason), or a continuity note. It feeds the success metrics and updates later units; no video is involved.

### Logs — logs/

Three append-only JSON Lines files, so nothing important lives only in SQLite.

| File | One line per | Fields |
| --- | --- | --- |
| decisions.jsonl | Model decision, card answer, accepted deviation | time, card id, stage, target, options with scores, choice, by (model or user), reason, note |
| calls.jsonl | Model call | time, stage, target, model, input and output tokens, cost |
| feedback.jsonl | Your optional feedback or continuity note | time, unit, result (ok, redo, continuity), note |

### Card

```json
{"id": "c_0007", "stage": "A5", "kind": "creative | checkpoint | budget | deviation | stuck",
 "question": "开篇钩子", "options": [{"key": "A", "label": "…"}],
 "recommended": "A", "reason": "…"}
```

Cards have no default and never expire; only work that depends on an open card waits for it.

### Finding

```json
{"reviewer": "scene_fidelity", "artifact": "storyboard/ep03.json",
 "severity": "blocker | major | minor", "kind": "error | omission",
 "location": "ep03_u02 s1", "evidence": "quote from the target, or from the source for an omission",
 "problem": "…", "suggested_fix": "…"}
```

## 5. Workflow A — Screenwriting

Pipeline A turns a novel into locked episodes. The user normally touches it twice: an optional direction card and the episode 1 approval.

1. **A1 Import (code).** Split the novel by chapter and number every paragraph. Output: source/chunks/.
2. **A2 Chunk summaries (cheap model, parallel).** Per chunk: events, characters, beat tags (爽点 / 虐点 / 反转 / 打脸 / 身份揭示 / 悬念), intensity 1–5, and paragraph references for each beat. Hard check: the references exist.
3. **A3 Breakdown (strong model).** Main plot, subplots, character arcs, and the key-beat timeline, each beat with paragraph references. Output: breakdown.md.
4. **A4 Adaptation plan (strong model).** Keep / cut / merge per beat against the audience rubric; the differentiation card; the episode beat sheet (分集): one line per episode with the beats and source paragraphs it covers, its estimated runtime (3–5 minutes), its core emotions, the turn it makes and its end hook with the hook type, with a major turning point every 2–4 episodes and the retention checkpoints marked (see Episode planning below); 3–5 hook candidates; the **key moments** list (opening hook and major turning points); the moments to amplify; the first bible.md and the starting ledger (ledger/ep00.md). packets adds the original source passages for every key beat, so the plan rests on the novel, not only on summaries. Reviewer: **plan reviewer**.
5. **A5 Direction card (conditional).** Raised only if the escalation rule triggers, usually for the hook. Otherwise the model's pick is logged.
6. **A6 Amplify (strong model).** Key moments get three versions, and the **payoff judge** picks and sharpens the best; other moments on the amplify list get one version. Amplification completes the payoff chain from the rubric, including the tangible gain at the end. Source passages are included. New story entities are added to bible.md.
7. **A7 Episode 1 (strong model).** Inputs: plan slice, bible, ledger/ep00.md, source passages for its key beats. Output: the episode and its ledger\_out. Reviewers: **viewer** and **story check**.
8. **A8 Episode 1 approval (fixed checkpoint).** Approve, or reject with one note. The approved episode becomes the reference example for later episodes and for the viewer.
9. **A9 Remaining episodes, in order.** Each gets its plan slice, bible, the previous episode's ledger\_out, the previous episode's last scene and the episode 1 example, and returns the episode plus its ledger\_out. Same reviewers. Writing in order is slower than in parallel, but text is cheap and it removes cross-episode contradictions.
10. **A10 Lock, one episode at a time.** An episode locks when its hard checks pass and its reviewers report no blockers, so B can start on it immediately. After locking, a script changes only through a script note (section 9).

### Episode planning: 3–5 minutes, a hook at every end

Episodes are cut from the story, not from the chapters. One episode usually draws on several chapters, and a long chapter can feed two episodes. A4 sets the boundaries in three steps:

1. **Estimate runtime per beat.** After cuts and merges, every kept beat gets an estimated screen time: dialogue characters ÷ speech rate, plus an allowance per action and per visual payoff (a reaction, a reveal). Beats keep their chapter and paragraph references, so each episode lists exactly which source passages it covers.
2. **Find hook points.** Places in the beat timeline where the story can stop open, using the hook types below.
3. **Place boundaries.** Each boundary sits on a hook point so that every episode lands between 3 and 5 minutes. If no hook point falls inside the window, the planner cuts a scene at its peak (end before the answer arrives), moves a reveal or an arrival to the boundary, asks A6 to strengthen a weak hook, or regroups adjacent material. It never pads an episode with filler to reach 3 minutes.

| Hook type | The episode ends on |
| --- | --- |
| Danger peak | The moment before the outcome of a threat |
| Pending reveal | An identity, a test result or a truth about to come out |
| Discovery on the brink | Someone about to find out what the audience already knows |
| Reversal setup | A plan that seems to succeed, or a near miss |
| Escalation | A stronger opponent or a bigger threat arriving |
| Forced decision | A choice between two bad options |
| Turning line | An arrival or one line that flips the meaning of the scene |

**Episode shape.** About the first 20 seconds pay off or advance the previous episode's hook. Then two or three emotion cycles (conflict, turn, payoff, using the payoff chain where it fits). The last 30–40 seconds build pressure into the new hook, and the episode cuts at the peak. A 3–5 minute episode therefore holds two to four core emotion points, not one.

**Checks.** Hard: every kept beat belongs to exactly one episode, and every episode names a hook type. Warnings: estimated runtime outside 3–5 minutes. The plan reviewer judges whether each hook is strong enough; the viewer answers whether it would watch the next episode.

Runtime estimates are planning numbers, calibrated against your real clips in M0.

### Story rules the audience rubric encodes

These rules live in audience\_rubric.md, not in code. Writers follow them; the plan reviewer, viewer and story check test against them. They are starting rules, to be revised from real completion and drop-off data.

| Area | Rule |
| --- | --- |
| Main line | One conflict resolves before the next one starts. One protagonist's point of view throughout. A subplot stays only if it pushes the main line. Story time stays compressed; avoid jumps of years. |
| Opening, first 3 episodes | The protagonist appears in the first scene, already in a sharp conflict. The big goal is clear within episode 1. Pressure comes from several directions in episodes 2–3. The first major hook lands by the end of episode 3, preceded by a near-miss where the goal almost succeeds. Setups pay off within about one episode. Anger builds without release until the first checkpoint. |
| Every episode | Two to four core emotion points (爆点, 虐点 or 爽点) in a 3–5 minute episode, one dominant emotion at a time. The episode turns: the situation moves clearly toward or away from the goal. It opens by paying off or advancing the last hook and ends on a new one. |
| Payoff chain (爽点) | Pretense or hidden strength (装), the reversal (打脸), onlookers' shock (震惊), then a tangible gain such as status, money or recognition (收获). A payoff missing the last step feels incomplete. |
| Checkpoints | Retention checkpoints sit at major turning points about every 4–6 episodes. On free platforms (Hongguo, Douyin collections) their job is to stop viewers leaving, not to sell an unlock. Suspense comes before a checkpoint; a true reversal lands first and the checkpoint follows. True reversals are reserved for checkpoints; ordinary episodes turn instead. |
| Protagonist | Sets a big goal in episode 1 and reaches sub-goals every 2–3 episodes. Solves the core problem personally; help is allowed, rescue is not. |
| Antagonist | Has a concrete plan that advances, so pressure keeps rising. Never repeats the same scheme. Targets what the protagonist values most. |
| Information gaps | Each key scene deliberately chooses who knows what among protagonist, other characters and audience. Default to the audience knowing more than the characters. |
| Differentiation | Keep the familiar setup the audience recognises, then subvert its second half. |
| Scenes | Every scene has a conflict and at least one character whose state changes; a scene without either is cut. |
| Dialogue for AI video | Few, short, direct, colloquial lines; prefer a visible action to a line, since each line costs lip-sync accuracy and seconds inside a unit. Every character sounds different. Inner monologue only when nothing else can show it. |

## 6. Workflow B — Art direction, assets and prompts

Pipeline B turns locked episodes into final Seedance prompts, each with a reference mapping of named placeholders. It stops there. You create the images, voices and music in your own tools, replace the placeholders inside your video tool, generate, and edit the episode yourself. The platform never generates, uploads, downloads, stores or reviews media.

1. **B1 Art direction (strong model, once per project).** From the bible and the first locked episodes, write style.md: rendering style, palette, lighting, materials, costume and set design language, and 16:9 framing habits. It ends in a style-lock block inserted verbatim into every prompt, and includes a description of the palette card you create as @色卡.
2. **B2 Asset extraction (strong model, per episode as it locks).** From the locked script, bible and style.md, list characters, locations, props, ui elements, voices and, if the project uses music, music cues. Each visual entity is a master plus the children the script requires (section 4), and every asset gets a placeholder name. Existing assets are reused; code dedupes and assigns ids.
3. **B3 Asset sheet (cheap model).** For each new asset: an image prompt (children describe only what changed from their master), a voice brief (gender, age, timbre) or a music brief (mood, tempo). The asset sheet is what you use to create the real assets in your own tools, once per project and then as new assets appear.
4. **B4 Look approval (fixed checkpoint).** You approve style.md and the asset sheet for the main characters. You create and choose the actual images in your image tool; the platform only needs the descriptions.
5. **B5 Storyboard (strong model, one call per episode, in order).** Inputs: the episode, bible, its ledger\_in, the previous episode's final carry\_out, the asset list, style.md and the model card. Shots with size, camera, 16:9 screen positions, action, dialogue, sound and overlays; units within the model card's maximum; each unit's on\_screen, offscreen and absent lists, carry\_in and carry\_out. The storyboard uses existing assets; if it needs one that does not exist, the request goes back to B2. Hard checks: unit length, known names, cast coverage. Warnings: speech fit, shot length. Reviewer: **scene fidelity**.
6. **B6 Reference mapping (code; you can edit).** By default each unit gets every entity present in the scene during the unit, on screen or off: characters, the location, props in play and ui elements, each in the version (master or child) that matches its state in carry\_in, plus @色卡, the voice of each speaker, and the music cue if there is one. Off-screen references stay because people sharing the space can show at the edge of frame or from a new angle; you can remove or add references per unit without a card. A unit marked chain\_from\_previous also gets @上段尾帧, which you fill with the last frame of the previous clip you kept. If a unit exceeds the model's reference limits, the storyboarder decides: split the unit, reframe, or drop a reference with a reason. Code never drops a reference on its own.
7. **B7 Prompt writing (strong model, one call per scene).** The prompt has six parts in a fixed order. Code writes the first three: (1) the reference manifest, one line per placeholder with its role, name, identity notes and scale (for example @云清禾\_母图 作为云清禾的形象参考：灰白破裙，青绿眼睛，身高约1.65m; @系统\_声音 作为系统的音色); (2) the style-lock block; (3) the audio and text line (有音效，无音乐，无字幕, or the music cue). The model writes the rest: (4) a continuation line from carry\_in (承接上一段：…); (5) the shots, each with number, optional time range, size, angle, focal length, movement, subjects by placeholder, action, and dialogue with tone, kind and voice placeholder, with off-screen references placed relative to the frame; (6) one final 负面 section with specific negatives.
8. **B8 Prompt checks and reconstruction (text only).** Hard checks first, then the **reconstruction** reviewer in two calls. The blind call sees only the prompt, the description of each placeholder and the model card, never the script or storyboard; it describes what each shot will show, flags anything ambiguous or beyond the model, and flags off-screen references likely to be drawn into frame. The compare call checks that description against the storyboard unit; each mismatch becomes a finding for B7. Maximum two rounds.
9. **B9 Delivery (code).** For each episode: one file per unit with its prompt and reference mapping table, an asset checklist of every placeholder the episode uses, and an overlay list of on-screen text to add in editing (section 4). This is where the platform's job ends.
10. **B10 Your feedback (optional).** After you generate, you can record per unit whether the prompt worked first time or needed a redo (sfl feedback), which feeds the success metrics. If a clip you keep changed something later units must follow (she ended up seated, a prop moved), file a continuity note: it updates carry\_in of the following units and marks their prompts stale. A story change becomes a script note for pipeline A. Neither involves sending video to the platform.

## 7. Agents and reviewers

Every agent is one stateless model call with a packet built by code from packets.yaml. Six reviewer roles cover the whole pipeline: four run on everything they apply to, two only when their trigger occurs.

### Workers

| Role | Stage | Tier | Sees | Produces |
| --- | --- | --- | --- | --- |
| Chunk summarizer | A2 | Cheap | One chunk, known names | Beats with tags, intensity, paragraph refs |
| Breakdown | A3 | Strong | All summaries | breakdown.md |
| Adaptation planner | A4 | Strong | Breakdown, source passages for key beats, rubric, project.yaml | plan.md, bible.md, ledger/ep00.md |
| Amplifier | A6 | Strong | One moment, its source passage, its place in the plan, rubric | One or three versions |
| Episode writer | A7, A9 | Strong | Plan slice, bible, previous episode's ledger\_out, previous last scene, episode 1 example, source passages | Episode and its ledger file |
| Storyboarder | B5 | Strong | One episode, bible, its ledger\_in, the previous episode's final carry\_out, asset list, style.md, model card | Storyboard JSON |
| Asset extractor | B2 | Cheap | Locked episode, bible, style.md, assets.csv, storyboard requests | New asset rows |
| Asset prompt writer | B3 | Cheap | One asset row, parent description, style guide | Image, voice or music brief |
| Unit prompt writer | B7 | Strong | One scene's units, their reference mappings, template, style.md, model card | Prompts for the scene |
| Art director | B1 | Strong | Bible, first locked episodes | style.md and sample frames |

### Reviewers

| Reviewer | Runs | Sees | Must not see | Question |
| --- | --- | --- | --- | --- |
| Plan reviewer | Once per plan (A4, and after any plan change) | Breakdown, plan, source passages for key beats | Planner's reasoning | Does a key beat contradict the source? What breaks after the cuts? Where is the adaptation still generic compared with a typical creator's version? Does the beat sheet follow the rubric's opening shape, emotion cadence and checkpoint rules? |
| Viewer | Every episode | Episode, recap assembled by code from the hook and cliffhanger lines of earlier locked episodes (none for episode 1), audience profile | Novel, plan, ledger | Where would you swipe away? What confused you? Would you watch the next episode? |
| Story check | Every episode | Episode, plan slice, its ledger\_in, source passages for its key beats | Writer's notes | Does it follow the plan and the ledger? Who knows or holds something they shouldn't? Which setup has no payoff? Does the episode turn and end on a hook? Does the protagonist act rather than get rescued? Does the antagonist repeat a scheme? Which scene has no conflict or state change? |
| Payoff judge | Key moments only (A6) | Source passage, three versions, rubric | Amplifier's reasoning | Which version hits hardest, and how could it hit harder? |
| Scene fidelity | Every scene (B5) | Script scene, the episode's ledger\_in and previous carry\_out, the scene's units, asset list | Storyboarder's reasoning | Is any person, prop or action missing or changed? What changes between shots without being shown? |
| Reconstruction | Every unit (B8) | Blind call: prompt, placeholder descriptions, model card. Compare call: its own description and the storyboard unit | Blind call: script and storyboard | What will each shot actually show, and where does that differ from the plan? |

### Rules for every reviewer

1. Severity is blocker, major or minor. Every blocker is always reported; at most five major or minor findings are shown, and the rest are logged.
2. Every finding quotes evidence: from the target for an error, or from the source for an omission, naming what is missing.
3. No blocking issues is a valid and expected answer.
4. Reviewers may use a different model provider from the writers (section 12).

### Fix loop

1. For each blocker or major finding, the writer responds fix, or reject with a reason.
2. A fix is confirmed only by re-running the same reviewer, which checks that the problem is gone and that its source and dependents were updated. The real fix may be elsewhere (for example, adding a missing action in an earlier shot), so whether the quoted line changed proves nothing either way.
3. A rejection goes back to the reviewer once, with the writer's reason. The reviewer withdraws the finding or holds it; a held blocker becomes a card.
4. Maximum two rounds per artifact; anything still open becomes a card. Minor findings never block.

### Role card format

One page per card: the role in one sentence, the task, the output format, at most five hard constraints, and two worked examples. Rules that apply everywhere belong in the rubric or style guide; mechanical rules belong in checks.

## 8. Human checkpoints and decision cards

The user makes three kinds of fixed decisions plus occasional cards, all in the Inbox, and never reviews documents.

### Fixed checkpoints

| Checkpoint | Stage | Why a human adds value | What the user does |
| --- | --- | --- | --- |
| Episode 1 approval | A8 | Calibrates the standard for every later episode | Approve, or reject with one note |
| Project look | B4 | Style and character design are taste calls | Approve style.md and the asset sheet for the main characters |
| Token budget | Any | A spending decision | Raise the budget or stop |

### The escalation rule

A model decision becomes a card only when all three hold:

1. **Close call:** its top two options score within the configured margin.
2. **Expensive to undo:** it affects the main plot, several episodes, or generated media.
3. **Taste, not rules:** the rubric does not settle it.

Two technical cases also raise a card: an artifact still failing after two rounds, and a reviewer holding a blocker the writer rejected.

### Card format and defaults

One question, two or three one-line options, the model's pick with a one-line reason, and an optional note from the user. At most three open cards per phase; further cards wait in a queue.

Cards have no default and never expire: an escalated decision is, by definition, expensive to undo. Work that does not depend on an open card keeps running; work that does waits.

```text
【决策 2/3】开篇钩子
A ⭐ 拍卖台被当众羞辱 → 闪回揭示身份（反差最大，开场就有冲突）
B   负一百忠诚度的系统提示 → 男主一笑（悬念强，需要观众先理解系统）
C   她抓桌被奴印压制的瞬间（画面冲击强，信息量少）
```

## 9. Validation, change propagation and recovery

An artifact is done when its hard checks pass and its required reviewers report no blockers. The model never certifies its own work, and estimates never block on their own.

### Hard checks (block and retry)

| Check | Applies to | Rule |
| --- | --- | --- |
| Format | All model output | Parses as the expected JSON, screenplay or card format |
| Names | Episodes, storyboard, prompts | Every speaker, cast member, on\_screen name and asset name exists in bible.md or assets.csv (reserved speakers 系统 and 旁白, and roles listed under Extras, always pass) |
| Source references | Summaries, breakdown | Every cited chapter and paragraph exists |
| Cast coverage | Storyboard | Every unit accounts for every member of the scene's cast line: on\_screen, offscreen, or absent with a reason |
| Unit length | Storyboard | Unit seconds within the model card maximum |
| Reference limits | Packages | Counts within the model card; otherwise reported to the storyboarder (section 6, B6) |
| Reference sync | Prompts | Every placeholder in the prompt is in the unit's mapping, and every mapped placeholder is mentioned |
| Assets described | Packages | Every placeholder has an approved description in the asset sheet |
| Pipeline jargon | Prompts | No terms that exist only inside the pipeline (空间锚, 收束状态, 桌端通路, …); the list lives in skills/ |
| Beat coverage | Plan | Every kept beat belongs to exactly one episode, and every episode names a hook type |

A hard failure retries the same worker with the specific error, at most twice; then a card.

### Warnings (shown, never automatic rewrites)

Episode runtime outside 3–5 minutes; speech fit per shot (dialogue characters ÷ speech rate); negatives written inside shots instead of the final 负面 section; dialogue overlap with the source text; prompt length; readable on-screen text; ambiguous spatial or state words such as 近侧, 远侧 or 初始状态. Warnings go into the reviewers' packets, the package manifest and the shot sheet; they never become Inbox items. Source overlap is a differentiation signal, not proof of originality or platform compliance.

### Version binding

Every job records the fingerprints of all its actual inputs when it starts, including shared ones (the bible entries it uses, its ledger\_in, asset descriptions). Style references (rubric, style guide, templates, the episode 1 example) are recorded too, but changing them never marks finished work stale; it only affects future jobs. When it finishes, store writes the result as current only if every input is unchanged; otherwise the result is kept as a stale candidate and never overwrites newer work.

### Change propagation

When a content input changes, everything that recorded it is marked stale. The model card is a content input of storyboards and prompts, so switching models (for example from 2.0 to 2.5) marks them stale. Text that is not yet locked or packaged re-runs automatically. Locked episodes and packaged units are flagged in the Inbox with one re-run action.

Pipeline B never edits a script. A script problem found in B becomes a one-line note in notes/script\_notes.md; A applies it to that episode, re-runs its checks and reviewers, updates the ledger if needed, and only what depends on the change becomes stale.

### Persistence and rollback

1. Text files are in a git repository, committed after every completed stage.
2. User decisions, feedback and model-call costs are recorded in logs/decisions.jsonl, logs/feedback.jsonl and logs/calls.jsonl. SQLite is backed up daily; open cards can be re-derived by re-running the escalation check.
3. sfl revert restores creative artifacts (scripts, storyboards, prompts, delivery folders) and marks their dependents stale. It never touches logs/: rollback changes which version is current, never the record of decisions, feedback or costs.
4. Delivery folders are regenerated from the current version and never edited by hand.

### External failures

Model calls retry with backoff (three attempts), then pause that job only. Every call is cached by input fingerprint, so a resumed run never pays twice for the same call.

## 10. File organization

The repository separates methodology (skills/), execution (platform/) and configuration; each project is a self-contained folder a person can read without the app.

### Repository

```text
storyforge-lite/
  skills/
    writer/
      roles/       chunk_summarizer.md  breakdown.md  adapt_plan.md
                   amplify.md  episode_writer.md
      reviewers/   plan_reviewer.md  viewer.md  story_check.md  payoff_judge.md
      taste/       audience_rubric.md  differentiation_card.md  screenplay_format.md
    director/
      roles/       storyboard.md  asset_extract.md  asset_prompt.md  unit_prompt.md
      reviewers/   scene_fidelity.md  reconstruction.md
      taste/       style_guide.md  seedance_template.md  shot_rules.md
    shared/        card_format.md  finding_format.md
  model_cards/     seedance-2.0.yaml  seedance-2.5.yaml
  platform/
    runner/  packets/  llm/  checks/  refs/  cards/  media/  store/  ui/
    packets.yaml   what each role sees and must not see
    stages.yaml    stage order, hard checks, warnings and reviewers per stage
  config.yaml
  tests/
    fixtures/golden_project/     small novel with known-good outputs
    fixtures/failure_cases/      one folder per injected failure
  cli.py
```

### Project folder

```text
projects/<novel>/
  project.yaml           episode runtime (3–5 min), model card, music: none | cue
  source/novel.txt
  source/chunks/         paragraph-numbered
  summaries/
  breakdown.md
  plan.md                beat sheet: episodes, runtime, hooks, source refs
  bible.md               story entities and voices
  style.md               art direction and style lock
  ledger/ep00.md ...     starting state, then state after each episode
  episodes/ep01.md ...
  notes/                 script notes and continuity notes
  storyboard/ep01.json ...
  assets.csv             production assets and their placeholders
  delivery/ep01/         u01.md ... (prompt + mapping), assets.md, overlays.md
  findings/
  logs/decisions.jsonl  logs/feedback.jsonl  logs/calls.jsonl
  .git/                  text only
```

To change behaviour, read stages.yaml and packets.yaml first: adding a stage or changing what a reviewer sees never requires touching module code.

## 11. User interface and CLI

Three working screens plus settings, and the user spends almost all of their time in the Inbox. The CLI comes first (M1); the UI is a thin layer over the same functions (M2).

| Screen | Shows | User actions |
| --- | --- | --- |
| Projects | One progress bar per episode: script, storyboard, prompts | Create a project, upload a novel, start or pause |
| Inbox | Cards, approvals, stale flags and paused jobs, one item at a time | Choose, add a note, approve, re-run, retry |
| Episode | Beat sheet line, units, per-unit reference mapping | Copy a prompt, edit a unit's references, download the delivery folder, add feedback or a note |
| Settings | Models, providers, token budget, card limits, thresholds | Edit config.yaml values |

The UI never shows schemas, fingerprints or logs by default; a details link opens the underlying file read-only.

```text
sfl new <project> [--novel novel.txt] [--model seedance-2.0]
sfl import <project> --episode ep01.md --bible bible.md --ledger-in ep00.md   # M1 input path
sfl run <project> [--until B9] [--episodes 1-5]
sfl status <project>
sfl cards <project>
sfl answer <card-id> <option> [--note TEXT]
sfl refs <unit-id> add|remove <placeholder>   # edit a unit's reference mapping
sfl export <project> <episode>                # delivery folder
sfl feedback <unit-id> ok|redo [--note TEXT]  # optional, after you generate
sfl note <episode|unit> TEXT                  # script note or continuity note
sfl rerun <stage> <target>
sfl revert <target> <snapshot>
```

Every command maps to one function in runner, cards or store; the UI calls the same functions.

## 12. Models, tools and cost

All model and tool choices live in config.yaml and model cards, so switching a model or provider never touches code.

```yaml
models:
  strong:   {provider: openai, model: <strong-model>}
  cheap:    {provider: openai, model: <cheap-model>}
  reviewer: {provider: anthropic, model: <review-model>}   # optional: different provider
best_of_n: {key_moments: 3, other_moments: 1}
speech_rate_chars_per_sec: 4.5     # calibrate in M0
episode_minutes: {min: 3, max: 5}  # warning outside the range
card: {score_margin: 0.1, max_open_per_phase: 3}
fix_rounds: 2
warnings: {source_overlap: 0.15, prompt_chars: 3500}
token_budget: {per_episode: <tokens>, alert_at: 0.8}
output: {model_card: seedance-2.0, ratio: '16:9', music: none}   # music: none | cue
concurrency: {llm: 4}
```

All numbers are starting defaults to tune on real episodes. The 3–5 minute episode target can be overridden per project in project.yaml. Values in project.yaml (episode targets, model card) override config.yaml for that project.

```yaml
# model_cards/seedance-2.0.yaml
name: seedance-2.0
max_seconds: 15
refs: {images: 9, videos: 3, audio: 3}
multi_shot: true
timestamp_control: weak
readable_text: unreliable   # composite text in post
native_audio: true
```

Vendor pages describe Seedance 2.0 as accepting up to 9 images, 3 videos and 3 audio files per generation ([Atlabs guide](https://www.atlabs.ai/blog/seedance-2-prompts-guide)), and Seedance 2.5 as generating up to 30 seconds with up to 50 references ([overview](https://peerlist.io/musci/project/seedance-25)). Verify every limit on your own account; the model card is the one place to correct it.

**Delivery.** For each episode the platform writes a delivery folder: one file per unit with its six-part prompt and reference mapping table, an asset sheet listing every placeholder the episode uses with its description and asset prompt, and an overlay list of on-screen text to add in editing. You attach the real images, voices and music to the placeholders in your video tool. The platform never sends, receives or stores media.

**Cost.** Every model call is logged with its tokens and cost, and an episode that exceeds its token budget raises a card. Calls are cached by input fingerprint. Roles are not calls: each scene costs one prompt call plus a blind read and a compare per unit, so M1 records real calls, tokens and time per episode before anything is optimized. Cheap models do summaries, asset extraction and asset briefs; strong models do planning, amplification, episodes, storyboards, prompts and reviews.

## 13. Development roadmap

Production starts at M1, before any UI exists. From M1 on, a milestone closes only when it runs one real episode end to end and catches every one of its failure-injection cases; M0 is a single-scene comparison.

| Milestone | Builds | Failure cases it must catch | Exit criterion |
| --- | --- | --- | --- |
| M0 Methodology (no code) | Role and reviewer cards, audience rubric (section 5), style guide, Seedance template, model card; speech rate and episode runtime measured on real clips; a written definition of a usable clip | — | On the U01/U02 scene, three generations per prompt for the old and new templates; the new usable rate is higher. The old-template rate is the pre-platform baseline |
| M1 Prompt path (CLI) | Import path, B1–B9 (art direction, asset sheet with placeholders, storyboard, scene fidelity, reference mapping, prompts, reconstruction, delivery folders), logs, sfl feedback, call, token and time counting | A cast-line character missing from the storyboard; a standing character seated in the next shot without being shown; a resting prop pushing a unit over the reference limit; pipeline jargon in a prompt; a later episode's state leaking into an earlier one; a legitimate exit wrongly flagged; an off-screen reference likely to be drawn into frame and not flagged; a 系统 line prompted as an in-scene voice; a placeholder in a prompt missing from its mapping | One real episode delivered, generated by you, and scored with sfl feedback; first-pass usable rate compared with the M0 baseline |
| M2 Inbox | version binding, cards, ui, reference mapping view, continuity notes | A job finishing after its input was edited; an open card blocking more work than depends on it; a continuity note that should stale only the following units | You handle one full episode only through the Inbox |
| M3 Screenwriting | A1–A10, episode planning (3–5 minutes, typed hooks), ledger, source passages, plan reviewer, viewer, story check, payoff judge, rolling lock, script notes, propagation | A summary that misreads a key fact; an episode outside 3–5 minutes or without an end hook; a kept beat in two episodes or none; an episode contradicting the ledger; a script note that should stale only one episode | A novel runs to a locked episode 1 with at most two cards |
| M4 Batch | Concurrency, crash resume, token budgets, prompt caching | A crash mid-batch; an episode exceeding its token budget | Ten episodes delivered in batch with no manual repair |

**Inputs for M1.** M1 builds pipeline B only, so it imports what pipeline A would otherwise provide: an adopted script in the screenplay format with cast lines, character and location notes as bible.md, and the starting state as the episode's ledger\_in. If you already have assets for the project, their descriptions can be imported so the placeholders match what you have. M1 builds no part of pipeline A or the UI.

**Kill rule.** A clip is usable when you keep it without a redo; you report this with sfl feedback. If M1's first-pass usable rate is not above the pre-platform baseline from M0, stop building. Use the logs, feedback and failure cases to find which layer failed (script, asset descriptions, prompt or model capability) and fix that layer first. If reconstruction findings did not predict the units you had to redo, drop reconstruction as an export gate.

**Harvest from StoryForge.** The task queue, cost tracking and chapter-analysis caching, only where each is a separate module usable without the rest of the app.

**Testing.** Unit tests for every hard check; a golden fixture with a stubbed model; the failure cases above as fixtures; one real end-to-end episode with real models.

## 14. Implementation rules, out of scope and open questions

### Rules for Codex

1. Codex receives this document as background and one milestone spec as its task. It implements only that spec; anything outside it is out of scope, even if this document describes it.
2. Python 3.11+, one package per module in section 3. The UI is a small server-rendered web app with no front-end build step.
3. Content, decisions, feedback and call costs are files in the project folder. SQLite holds only the job queue, open cards (cost totals are computed from calls.jsonl), and is backed up daily; money and user decisions are also in the logs. No ORM migrations.
4. No prompt text or creative rule in Python; every model instruction is loaded from skills/.
5. Every model call goes through llm with a packet from packets. No agent sessions or history between calls.
6. Every hard check has unit tests, and every failure case of the current milestone has a fixture test.
7. When the spec is ambiguous, stop and ask instead of inventing a rule, state or field.
8. Keep the codebase small enough that a coding agent can read every module touched by a change in one session.

### Not in v1

Generating, uploading, downloading or reviewing images, audio or video; original screenplays from scratch; film or series formats; 9:16; version branches and side-by-side comparison; final editing, subtitles and publishing; multi-user accounts; an MCP server (possible after M4 as a thin layer over the CLI functions); platform analytics import (after M4, to tune the rubric from completion and drop-off data).

### Open questions

- [x] Aspect ratio: decided, 16:9 horizontal only. The style guide, storyboarder and model card assume 16:9; no 9:16 support in v1.
- [x] Decided: 3–5 minutes per episode; the episode count follows from the material.
- [x] Generation path for M1: manual upload in Jimeng / Dreamina, or API from the start?
- [x] Decided: the platform calls no image tools; you create assets from the asset sheet.
- [ ] Which provider and model for reviewers, and whether it differs from the writer's.
- [ ] Token budget per episode.
- [x] Decided: no image checks; the platform never sees images.

* [ ] Which existing scripts will feed M1, and how close are they to the screenplay format with cast lines?
