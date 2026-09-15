# Portable assets: evidence appendix

This appendix supports the [RFC](portable-assets.md). It records versioned
findings, their limits, and the consequences for compilation. Dates refer to the
September 13–14, 2026 research baseline, not an ongoing support promise.

## Reading the evidence

| Evidence class | What it establishes | What it does not establish |
|---|---|---|
| Specification | The published author/client contract. | That every client implements every optional component. |
| Official documentation | The vendor's documented behavior for the named surface. | Undocumented details, release parity, or resolution of contradictory pages. |
| Released implementation | Behavior in the inspected release code path. | A complete installed end-to-end workflow. |
| Development implementation | Behavior in a recorded development revision. | Shipped availability. |
| Static compilation/measurement | Generated files, metadata, bytes, and structural checks. | Runtime discovery or model compliance. |
| Runtime evidence | Observed behavior under recorded conditions. | Universal behavior on arbitrary tasks and configurations. |

This RFC has no newly executed harness-runtime qualification. Where existing
PRs report behavioral checks, that is labeled separately from committed tests.
An unavailable numeric limit is recorded as unknown, not unlimited.

### Baselines

| Subject | Inspected baseline | Evidence |
|---|---|---|
| Toolchain | PR73 merge `36b6b213c93fa184154677c98da640b0b965751e`; feature `6d795cd50af79c9710060f215804985b72d8dfd9` | Source and committed tests. |
| Hub inventory | `6c779ebecc0a7f898ddc51dfe15e8d251feeb646` | Source inventory; release 0.4.13. |
| PR73 role examples | Hub PR1345 head `42927ff8e359f139ca4db40b1c73b692efc89bd2` | Proposed role content; not assumed to be the inventory revision. |
| Codex | 0.154.0, `6b9826e3aa83b1a5947db50f4332cb9c65f1b340`, September 9 | Released code plus official docs. |
| Gemini CLI | 0.59.0, `fb0d535af931b27c51e87e5e6ade72905b1e8390`, September 8 | Released code plus official docs. |
| OpenClaw | 2026.9.4, `3a9d69db306cd7f081e06254cb89c4bcc14a7107`, released September 11 | Released code plus official docs. |
| Hermes | 2026.9.11, `939e45c91d751fadd94dcd1b873ac3cb44846213`, September 11 | Released code plus official docs. |
| Devin | Stable changelog through 3000.10.21, September 10 | Official documentation/changelog. |
| Claude Code and Cursor | Official documentation inspected September 13–14 | No release-pinned runtime experiment. |
| Copilot | Official surface-specific docs; VS Code development revision `e6a2323c8796874e969e970de97eed3696ed6fef` | Development observations must not be labeled shipped behavior. |

Source links appear with each finding below. These baselines intentionally remain
fixed for review; future profile updates should include a dated evidence diff.

## Standards and history

### Timeline

| Date | Event | Interpretation |
|---|---|---|
| July 24, 2026 | The specification repository records the change marking 1.0 published. Commit[^1] | Repository publication marker, distinguished from public launch. |
| August 6 | Agent Plugins 1.0 publicly announced. Vercel announcement[^2] | Public launch of the common packaging format. |
| August 12 | GitHub documents availability in VS Code, Copilot CLI, SDK, and app. GitHub changelog[^3] | Dated product availability statement. |
| August 21 | Devin stable changelog records Agent Plugins support. Devin changelog[^4] | Later client adoption, not the standard's creation date. |

Agent Plugins is an open project with its own technical governance. Do not
describe this timeline as an ISO or IETF ratification, or infer that all eight
harnesses adopted the common format on one date.
Governance[^5]

### Agent Skills versus Agent Plugins

Agent Skills defines `SKILL.md` and resources. Required fields are `name` and
`description`; optional fields include `license`, `compatibility`, string-map
`metadata`, and experimental `allowed-tools`. Its length guidance for the body is
not a universal runtime rejection threshold.
Agent Skills specification[^6]

Agent Plugins 1.0 defines root `plugin.json`, immediate `skills/*/SKILL.md`
discovery, and root `mcp.json`. Required manifest fields include `$schema` and
`name`; manifest versions and plugin release versions are separate. The core
components are skills and MCP. The root manifest is closed: invented support or
adaptation fields are nonconforming even though clients report and ignore unknown
root fields. Keep Hub reports separate. Unknown client namespaces do not acquire
portable semantics. Plugin manifest[^7],
Client extensions[^8]

A conformant client need not implement every component or transport. Invalid
components can be skipped independently, so a plugin's successful load is not
proof that every asset or server works. Compiler checks should detect statically
knowable schema, discovery, and capability mismatches before distribution. Runtime
failures require separate checks; intentional omissions remain recorded.
Loading and discovery[^9]

### Portable MCP boundary

Portable server configuration uses explicit, closed variants for stdio,
Streamable HTTP, and optional legacy SSE. A stdio command is one executable token,
with arguments separate. The portable fields do not accept an arbitrary native
MCP client's extra settings.
MCP authoring[^10]

`${PLUGIN_ROOT}` and `${PLUGIN_DATA}` expand in stdio arguments, environment
values, and cwd; they do not expand in the command or remote URL/headers.
Unrecognized placeholders remain literal. `PLUGIN_DATA` is persistent mutable
state; the package is not a portable credential store. Omitted stdio cwd means
the plugin root. Ambient variable inheritance is unguaranteed; authentication
and credential storage remain client concerns.
MCP runtime[^11]

## Package and surface matrix

| Harness | Native/distribution route | Portable route | Important boundary |
|---|---|---|---|
| Claude Code | `.claude-plugin`, native components and marketplace | No native root Agent Plugins loader established in the inspected docs | Claude-native compatibility must not be inferred from the portable manifest. |
| Codex | Native `.codex-plugin`; desktop/CLI plugin surfaces | Root Agent Plugins plus client extensions | Package route changes skill injection; do not extrapolate to every IDE/cloud surface. |
| Cursor | `.cursor-plugin`, native components/marketplaces | Portable skills and MCP | Editor/Agents window, CLI, and cloud have different documented surfaces. |
| Gemini CLI | `gemini-extension.json`, commands, context, skills, agents, hooks, MCP | No root Agent Plugins loader established in 0.59.0 inspection | Membership in standards governance does not prove loader implementation. |
| Copilot | Native plugins and surface-specific settings | Agent Plugins plus `com.github.copilot` extensions | CLI, VS Code host variants, app, SDK, and cloud require specific evidence. |
| Devin | `.devin` and Claude-compatible plugins | Root Agent Plugins | Native markers have precedence; local and cloud capabilities differ. |
| OpenClaw | Native runtime plugins or imported content bundles | Root Agent Plugins | Imported component detection may not imply execution. |
| Hermes | Native Python plugin registration | Root Agent Plugins | Discovery, metadata, resources, and runtime hooks differ between routes. |

The following per-harness sections supply the documentation and implementation
sources for this matrix.

## Claude Code

### Frontmatter and execution

Native skills expose fields beyond the portable baseline: invocation controls
(`disable-model-invocation`, `user-invocable`), arguments, `allowed-tools`,
`disallowed-tools`, `model`, `effort`, `context`, `agent`, `background`, `shell`,
`paths`, and hooks. Preserve only deliberately mapped semantics.
Skill reference[^12]

Native agent definitions separately control model, tools, and execution. Plugin
agents are narrower than standalone agents: `permissionMode`, `mcpServers`, and
hooks can be ignored in the plugin location. A field being documented for an
agent does not establish that it works inside a plugin.
Subagent reference[^13]

The plugin reference also governs default/custom paths. Do not rely on older
authoring guides that describe custom paths differently. Native `.claude-plugin`
behavior is the applicable target, not an assumed Agent Plugins 1.0 route.
Plugin reference[^14]

### Limits and hooks

The native skill-body recommendation is under 500 lines. No general native
whole-file hard cap was established from that guidance. The catalog and context
retention also have budgets; separately hosted skills/API upload rules should not
be attributed to Claude Code local plugins.
Skill reference[^12]

Hooks support several handler types, with event-dependent decisions. Async hooks
cannot retroactively block completed actions. Hook cwd and lifetime depend on
their scope; they cannot be assumed to match Gemini or Codex merely because an
event name is similar. Hook reference[^15]

**Adapter consequence:** use a native schema for the actual plugin location;
separate preapproval from availability and restrictions; verify delegation and
hook lifetime in the selected surface.

## Codex

### Skill metadata and role configuration

Portable and native plugin routes coexist. Client extensions and native fallback
metadata have explicit precedence; overlays should not be assumed to merge
arbitrarily. Plugin packaging[^16]

`agents/openai.yaml` is optional metadata for a skill, including interface data,
dependencies, and `policy.allow_implicit_invocation`. It is not a custom agent
role definition. Native custom roles use separate agent configuration. A malformed
skill sidecar may fail open rather than disabling the skill, so compile-time
validation matters for an invocation guarantee.
Skill documentation[^17],
Subagent configuration[^18]

Codex's shared skill parser is more permissive than the portable authoring
contract. Structured parsing and later presentation have separate limits:

| Field or stage | Codex 0.154.0 behavior | Compilation consequence |
|---|---|---|
| `name` | Missing/empty values fall back to the directory name; normalized values above 64 Unicode scalar characters are rejected. | Still emit an explicit portable name and validate grammar/directory agreement. |
| `description` | Required and nonempty after whitespace normalization; parsing preserves values above 1,024 characters. | Apply the portable source ceiling independently. |
| `metadata.short-description` | Recognized optional normalized string; no parser length cap. | Distinguish it from `agents/openai.yaml` interface metadata. |
| Unknown fields | Structured parsing ignores fields such as `model` and `allowed-tools`; full file text can still reach the model. | Acceptance supplies no model, approval, or restriction semantics. |
| Catalog description | Clipped at 1,024 Unicode scalar characters including an ellipsis. | Presentation truncation, not parser rejection. |
| Aggregate catalog | Default 2% of model context using approximate token accounting; fallback 8,000 characters; explicit token override capped at 10,000. | Descriptions shorten before entries are omitted; separate from the 8,000-byte injection cap. |

Released parser[^19], overlong-description regression test[^20], and catalog
rendering[^21] establish these distinctions. Pass model policy through a supported
role or delegation binding rather than relying on ignored skill fields.
Released skill implementation[^22]

### Injection, file, and catalog limits

The portable host route caps initial injected content at 8,000 UTF-8 bytes. The
whole-file read includes frontmatter. The prefix is UTF-8 safe; a separate warning
is recorded, but no “read the rest” instruction is appended inside the content.
The legacy host branch bypasses this particular cap.
Host route[^23],
Whole-file read[^24],
Prefix implementation[^25]

Do not generalize the native exemption to every execution path. The general
extension injection path also uses the cap. Ordinary filesystem access can read
the full installed file, but whether a model actually does so is a separate
behavioral question.
General injection[^26]

Catalog summaries have separate per-description and aggregate context budgets.
Names distinguish an individual component from its qualified plugin name;
PR73's 64-character combined-name guard is an adapter rule, not a universal Codex
limit. Rendering and limits[^21]

MCP-based skill import documents other submission limits: 256 KiB for `SKILL.md`,
1 MiB per file, 5 MiB per skill, and 100 files per skill. Generated archives for
one scan have a combined 8 MiB ceiling, including ZIP overhead. These submission
limits must not become global local-plugin source limits.
Import contract[^27]

### Hooks and transport

Codex hooks have supported handler types, trust requirements, and failure behavior
that differ from Claude. Parsed prompt/agent handlers are not automatically
executed; some unavailable/unsupported control paths fail open. A successful
plugin installation does not establish hook trust.
Hook documentation[^28]

The inspected portable MCP route does not support legacy SSE. Credential handling
and client-owned authorization are not interchangeable with arbitrary native
header interpolation. MCP documentation[^29]

**Adapter consequence:** pin the loading route; validate skill sidecars, complete
injected bytes, and supported role/hook mechanisms; test full-procedure recovery
where a wrapper relies on reading a file.

## Cursor

Native plugins support skills, agents, rules, commands, hooks, and MCP. Portable
packages supply the common skills/MCP components. Documentation for editor and
Agents window functionality must not be read as proof of CLI/cloud parity.
Plugin reference[^30],
Plugin overview[^31]

Skill fields include name/description and native applicability/invocation data
such as `paths`, `disable-model-invocation`, and presentation metadata. No native
numeric skill-body cap was established in the inspected docs.
Skills[^32]

Subagents have their own `model`, `readonly`, and `is_background` controls. Model
availability and policy can change effective selection. A native readonly mode
is different from asking a skill to avoid writes.
Subagents[^33]

Rules use `alwaysApply`, globs, and descriptions to distinguish always, matched,
model-selected, and manual behavior. The familiar under-500-lines guidance is a
readability recommendation. Rules[^34]

Hooks expose timeout and fail-open/fail-closed behavior. Cloud has a narrower
event/handler surface and some exploration paths do not fire the same hooks.
Do not reuse a local enforcement claim for cloud execution.
Hooks[^35]

**Adapter consequence:** render native `.mdc` rules where needed, retain exact
scope semantics, and record the installation and execution surface. Treat missing
body-size evidence as an unresolved limit, not permission to ignore context cost.

## Gemini CLI

### Extension, skill, and agent contracts

Version 0.59.0 uses `gemini-extension.json`, inline MCP configuration, extension
context, TOML commands, and Markdown skills/agents. No root Agent Plugins loader
was established in the inspected release.
Extension reference[^36]

The skill loader primarily consumes `name`, `description`, and body. Native
operational fields copied from another harness are not implemented by their
presence. The built-in authoring validator checks a single-line description
within 1,024 UTF-16 code units, but that helper's checks are not identical to the
runtime loader. Skill loader[^37],
Authoring validator[^38]

Local agents have a stricter schema including `name`, `description`, `kind`,
`display_name`, `tools`, `mcp_servers`, `model`, `temperature`, `max_turns`, and
`timeout_mins`. The parser's `mcp_servers` spelling differs from documentation
showing `mcpServers`. Defaults and nested-agent restrictions belong to this
schema, not to skill frontmatter.
Released agent loader[^39],
Subagent docs[^40]

### Commands and resource access

Commands use TOML `prompt` and `description`, with Gemini-specific argument and
injection syntax. Name segments above 50 characters are truncated; displayed
descriptions are sanitized to 100 characters. No local command-body cap was
found in the inspected loader.
FileCommandLoader[^41]

`@{...}` uses workspace file access. Commands lack skill activation's automatic
resource-directory access, and the command loader does not hydrate
`${extensionPath}`. Linked extensions can live outside a fixed default install
path. A resolver must support the installation forms it claims.
File injection[^42],
Activation[^43],
Linked extension resolution[^44]

### Rules and hooks

Always-on extension context can use `GEMINI.md` or the configured context file.
Project directory discovery is different from extension packaging. Arbitrary
glob-triggered context cannot be inferred from a conditional sentence placed in
always-loaded extension context.
Context hierarchy[^45]

Hook timeout is in milliseconds, default 60,000. After-tool denial can hide
results; after-agent denial can request a retry. The bundled Claude hook migration
code copies timeout values without unit conversion in the inspected version,
which illustrates why an official migration helper is not proof of equivalence.
Hook contract[^46],
Migration implementation[^47]

**Adapter consequence:** native extension rendering, commands-only adaptations
with tested resource access, parser-aligned agent fields, and explicit hook unit
conversion. Keep activation scope and authorization claims separate.

## GitHub Copilot

### Package and surface distinctions

Agent Plugins supports a portable core plus `com.github.copilot` components.
Local CLI, VS Code, app, SDK, and cloud configuration must be evaluated separately.
Cloud plugin configuration does not establish that all local hooks operate there.
Plugin concept[^48],
CLI plugin reference[^49]

CLI skill metadata includes name/description, arguments, `allowed-tools`,
`user-invocable`, and `disable-model-invocation`. VS Code has its own validation
and settings, including experimental fork behavior. The VS Code Agent Host and
Local host also differ in their handling of prompt files.
VS Code skills[^50],
Prompt files[^51]

Custom-agent definitions include description, tools, model, target, MCP data, and
metadata, with a separate 30,000-character prompt limit. CLI model policies can
distinguish preferred from required selection. Tool omission, wildcard, and empty
lists have materially different meanings.
Custom agents[^52]

### Implementation and hook caveats

Inspected VS Code development code normalizes/truncates discovery metadata using
JavaScript string lengths. That is development evidence only; it should not be
presented as a released limit without a release mapping.
Development prompt service[^53]

Hook names/payloads differ between native and compatibility forms; support for
matching, ask/deny decisions, and prompt handlers varies by surface. VS Code's
hook documentation and Copilot CLI's reference are separate contracts.
Copilot hooks[^54],
VS Code hooks[^55]

**Adapter consequence:** retain exact surface and host in the profile. Avoid a
single Copilot-wide frontmatter, prompt-file, or hook mapping.

## Devin

Native `.devin` markers precede Claude-compatible and root portable routes.
Native plugins can define additional paths and plugin dependencies, but that
installation behavior should not be imported into the Hub's explicit membership
contract. CLI plugins[^56]

Skills support fields such as `model`, `subagent`, `agent`, `allowed-tools`, and
`triggers`. `triggers: [user]` offers a manual-only activation route without a
Gemini-style command conversion. Tool restrictions have native semantics beyond
portable Markdown metadata.
Skill authoring[^57]

Custom-agent default model selection can use the router; the general profile
inherits. Nesting and user-question capabilities also differ: a custom child may
need its parent to relay questions. Do not assume a `model` passed to a spawning
tool overrides a configured profile.
Subagents[^58]

Local rules include always-on, manual, model-selected, agent, and glob activation.
Hooks support command/prompt behavior and documented failure handling. Local
composition and cloud's skill/context behavior are not identical; cloud-hook
documentation has inconsistencies that need runtime qualification.
Rules[^59],
Hooks[^60],
Cloud plugins[^61]

The August 21 release removed invoked-skill truncation. No current general numeric
body cap was established in the inspected docs.
Stable changelog[^4]

**Adapter consequence:** native declarative adaptations are promising, but model
inheritance, child interaction, composition, and cloud support need their own
profile tests.

## OpenClaw

### Native versus imported components

OpenClaw supports native runtime plugins and several imported content formats.
Claude agents can be projected to skills. Claude JSON hooks and Cursor rules,
agents, and hooks can be detected without being executed. OpenClaw's own hook
packs and native runtime code are different mechanisms.
Bundle mapping[^62],
Manifest routing[^63]

The native runtime manifest requires an ID and configuration schema; native JS/TS
can register behavior unavailable to a content-only bundle.
Native manifest[^64]

Skills support invocation and direct-command metadata plus nested
`metadata.openclaw` readiness gates for OS, binaries, environment, and config.
`always` can bypass dependency gates without bypassing the OS check. No portable
`allowed-tools` enforcement was established in the inspected loading path.
Skills[^65],
Frontmatter implementation[^66]

### Limits and identity

Defaults include a 256,000-byte skill-file limit, 300 candidates per root, 200
loaded skills per source, and a 150-skill/18,000-JavaScript-character prompt
budget. Catalog formatting may compact descriptions and omit entries. A manifest
has a separate 256 KiB bounded read.
Discovery[^67],
Prompt limits[^68],
Local loader[^69]

Plugin skills have relatively low precedence and can be shadowed by user/project
skills. Do not assume universal namespacing protects an imported skill's identity.
Native subagents provide real session, model, context, and tool-policy behavior
that an imported agent-as-skill does not automatically recreate.
Loading order[^70],
Subagent tool[^71],
Subagent policy[^72]

**Adapter consequence:** report detection versus execution, projection versus
delegation, name shadowing, and aggregate catalog loss. Native runtime adapters
need a separately maintained execution contract.

## Hermes

### Portable versus native skills

Portable plugins validate required skill name/description, directory agreement,
name up to 64 characters, description up to 1,024, compatibility up to 500, and
string-map metadata. Native authoring can use nested `metadata.hermes`, which is
not a valid portable string-map value. Stringifying it does not preserve the
native semantics.
Portable loader[^73],
Native skill authoring[^74]

Plugin skills are served through qualified names and are excluded from the
ordinary automatic skill index. Portable namespace generation includes a slug and
hash; discover the actual installed name instead of inventing a stable one.
`skill_view` resources must stay inside the skill directory.
Plugin skills[^75],
Serving implementation[^76]

The 60-character ordinary prompt description is presentation truncation, not
portable authoring rejection. The examined plugin-serving path reads full skill
content; no general body-byte limit was established there.
Prompt utility[^77]

### Readiness, hooks, and MCP

Ordinary skill viewing calls readiness/setup logic for credentials and other
requirements. The plugin-serving route does not call that same readiness path.
Copied metadata therefore does not establish equivalent credential prompting.
Ordinary serving[^78],
Plugin serving[^76]

Native Python plugins register tools, hooks, and lifecycle behavior in process.
Hook callbacks have bounded timeouts; pre-tool timeout can fail closed while
observational hooks skip. Portable skills/MCP do not install those Python hooks.
Native plugins[^79],
Hook dispatch[^80],
Subagent lifecycle API[^81]

Portable MCP supports stdio and Streamable HTTP and skips legacy SSE with a
diagnostic. Native dependency hints do not imply automatic package installation.
Portable loader[^73]

**Adapter consequence:** validate native and portable metadata separately;
declare discovery mode, resource boundaries, readiness behavior, and runtime-code
requirements. Do not promise that imported role prose provides native delegation.

## Limits register

Record each limit by stage, field, unit, configurability, failure mode, and version.
The following examples are not a universal source ceiling.

| Profile | Stage / field | Unit and value | Behavior / configurability | Evidence |
|---|---|---|---|---|
| Agent Skills baseline | Name; description; compatibility | 64; 1,024; 500 characters | Portable validation; client counting can vary | Agent Skills specification above |
| Claude Code docs | Body guidance | Under 500 lines | Authoring advice; not a demonstrated byte rejection | Claude section |
| Codex 0.154.0 portable host | Entire injected `SKILL.md` | 8,000 UTF-8 bytes | Prefix truncation; full file stays installed | Codex section |
| Codex legacy host | Same host branch | Particular portable cap bypassed | Not an exemption from every other limit | Codex section |
| Codex MCP skill import | Entry/file/skill/combined archives per scan | 256 KiB / 1 MiB / 5 MiB / 8 MiB | Submission route; archive total includes ZIP overhead | Import documentation above |
| Cursor docs | Native skill body | Unknown | No numeric cap established | Cursor section |
| Copilot CLI docs | Skill description | 1,024 characters | Target metadata ceiling | CLI plugin reference above |
| Copilot agent docs | Agent prompt | 30,000 characters | Different asset type | Custom-agent reference above |
| Gemini 0.59.0 | Command name segment / display description | 50 / 100 JS characters | Truncation/sanitization | Command loader above |
| Devin stable docs | Invoked skill body | Earlier truncation removed August 21 | Current general ceiling unknown | Stable changelog above |
| OpenClaw 2026.9.4 | Skill file | 256,000 bytes by default | Bounded read; oversized skill skipped | Loader above |
| OpenClaw 2026.9.4 | Catalog | 150 skills; 18,000 UTF-16 units by default | Compacts descriptions, then limits included skills | Prompt limits above |
| OpenClaw 2026.9.4 | Discovery | 300 candidates/root; 200 loaded/source by default | Can exclude otherwise valid skills | Discovery above |
| Hermes 2026.9.11 | Ordinary prompt summary | 60 Python characters | Presentation truncation | Prompt utility above |
| Hermes 2026.9.11 portable | Description | 1,024 Python characters | Invalid skill skipped; siblings preserved | Portable loader above |

Byte counts, Unicode scalar counts, UTF-16 string lengths, token estimates, and
line counts must remain labeled. A short description can be optimized for a
particular catalog without shrinking every source description to that display
budget. Supporting-file limits are independent of initial prompt injection.

## PR73 worked record

### Provenance and representation

Toolchain PR73, “Make agent roles discoverable in Codex plugins,” merged at
`36b6b213c93fa184154677c98da640b0b965751e`. Its paired Hub PR1345 supplied the two
role examples at `42927ff8e359f139ca4db40b1c73b692efc89bd2`; that PR was open when
inspected. This is a historical evidence baseline, not a current-status assertion.
Toolchain PR73[^82],
Hub PR1345[^83]

Source role membership remains `agent:<id>`. Existing sidecar configuration selects
`native` for Claude and `agent-skill` for Codex. Claude receives the original
agent; Codex receives `skills/<id>/SKILL.md` through a native `.codex-plugin`
manifest. The source role has not become a different logical asset.

| Concern | Actual adapter behavior | Compatibility interpretation |
|---|---|---|
| Procedure | Complete source body retained | Shared task content preserved |
| Discovery name | Generated skill uses source asset ID | Explicit target name binding |
| Delegation | Preamble instructs one child for this role | Instruction-driven, not native role enforcement |
| Same-role recursion | Assigned child performs role directly | Prompt protocol, not a runtime nesting guard |
| Other roles | Procedure may delegate different roles | One child for this role does not mean one descendant total |
| Context | Parent supplies task, relevant context, and skill location | No promise of complete parent-history transfer |
| Questions | Parent relays questions and waits for required answers | Requires observable behavioral verification |
| Missing subagents | Stop rather than execute the role in parent | Deliberate adapter behavior |
| Model/color | Source settings omitted | Model inheritance choice and presentation omission |
| Tools | Original restrictions represented as advisory prose | No equivalent enforcement or tool-name translation |

Accepted source fields are name, description, tools, disallowedTools, model, and
color. Other metadata is rejected by this narrow adapter. Duplicate/merged YAML
keys, invalid descriptions, empty procedures, unsupported input forms, and name
collisions are checked. These are adapter rules, not universal harness rules.
Agent-skill implementation[^84]

Tool-field warnings appear in build/validation results and CLI stderr, once per
included source role across plugins. They are not recorded in `hub.release.json`
or its hashes; model/color omission does not produce that warning. This motivates
a durable compatibility record, not merely a transient warning.

### Rendered size and injection

The exact PR renderer was applied to both proposed role sources for measurement:

| Role | Description characters | Procedure bytes | Generated whole-file bytes | Added bytes | Lines |
|---|---:|---:|---:|---:|---:|
| Leadgen experiment planner | 401 | 11,332 | 13,753 | 2,421 | 227 |
| Prospect docs analyzer | 300 | 7,133 | 9,441 | 2,308 | 164 |

The current native host path avoids the specific portable 8,000-byte cap. This
does not demonstrate that PR73 is broken. Moving the same output to the capped
route would omit later planner instructions and part of the analyzer's output
contract from initial injection. The wrapper instructs the parent to pass the
full file location, so recovery is possible; it still requires a test showing
the child reads it.

Suitable reviewed adaptations include a bounded delegation entrypoint plus a
complete procedure resource, an authored reference split, or continuing with the
native package route. Measure after rendering, not just before it.

### Dependency behavior and tests

The planner requires human decisions such as success criteria. Missing web access
limits public evidence; missing Monaco prevents overlap checks; missing Notion
means an unpublished result. The analyzer requires web access but can distinguish
browser/container execution from document inspection.
Planner source[^85],
Analyzer source[^86]

Committed conversion tests cover source preservation, registration, frontmatter,
tool-list parsing, metadata rejection, collisions, warnings, and reproducibility.
The ingestion helper checks artifact structure; it does not launch Codex. PR73
reports 333 tests including 41 conversion cases and synthetic behavior checks,
but the reviewed diff does not contain a full harness-runtime qualification.
Conversion tests[^87],
Structural helper[^88]

## Hub inventory and compiler gaps

At Hub revision `6c779ebecc0a7f898ddc51dfe15e8d251feeb646`, the inventory contained
57 `SKILL.md` entrypoints. All had name and description; 33 used `allowed-tools`.
Twenty-six exceeded 8,000 bytes; none exceeded 500 lines.
Pinned inventory[^89]

| Example entrypoint | Bytes | Lines |
|---|---:|---:|
| analyze-trigger-sentiment | 22,504 | 383 |
| remove-ai-tells | 19,773 | 241 |
| build-starport-demo-site | 18,979 | 307 |
| trace-review | 18,811 | 346 |
| manage-monaco-campaigns | 16,402 | 269 |
| authoring-instruction-hub-changes | 15,719 | 199 |
| verify-trigger-replay | 14,828 | 208 |
| customize-sales-deck | 14,672 | 264 |

The sales-deck skill also contains `Sales Deck — Generic Template.html`, measured
at 4,085,073 bytes. Its size is a resource/distribution question, not an instruction
entrypoint question. Measurements count whole-file bytes at the pinned revision;
they do not estimate tokens or prove those files were all injected in full.

The compiler currently defaults skills to delivery across its four targets, copies
skill trees, and preserves much source frontmatter. Generic projection writes
target output but does not establish native discovery or semantic translation.
MCP normalization is broader than a strict per-target runtime schema. The release
manifest records support declarations and output hashes, but not the full
adaptation and runtime-evidence contract proposed here.
[Asset rendering](../../src/promptless_instruction_hub/render/assets.py),
[MCP configuration](../../src/promptless_instruction_hub/mcp_config.py),
[Manifest generation](../../src/promptless_instruction_hub/release/manifests.py)

The source content hash excludes `asset.yaml`; effective metadata must be included
in adaptation freshness even though other release/version calculations already
incorporate metadata. This is an input-identity requirement, not a claim that all
current release hashing ignores sidecars.
[Asset loading](../../src/promptless_instruction_hub/assets.py)

## Unresolved evidence and qualification work

| Question | Current evidence | Required before a support claim |
|---|---|---|
| Full procedure after capped Codex injection | Complete file is accessible; PR73 passes its location | Observe actual read/recovery and role completion |
| Gemini command resource equivalence | Command and skill access paths differ | Test relocation, linked installs, root resolution, and permissions |
| Scoped rules without project projection | Conditional prose is possible | Record broader loading as an accepted difference or implement project integration |
| Hook controls on each surface | Vendor-specific documented semantics | Exercise event, payload, match, timeout, trust, and failure cases |
| Inheritance and unavailable models | Native defaults differ | Observe effective model selection and required/preferred policy |
| Copilot/Devin cloud parity | Some documentation is inconsistent | Qualify cloud separately; do not borrow local guarantees |
| Native metadata through portable readers | Hermes supplies a concrete mismatch | Target-specific schema tests and real loading |
| Catalog budgets with realistic installed sets | Individual/aggregate caps are different | Test a crowded environment, including name shadowing |
| Changes on development branches | Some code is unreleased | Map to an actual release before marking shipped |

Maintain these gaps explicitly. A documentation statement, a successful render,
and a passing runtime fixture are useful evidence at different levels. None
should silently stand in for another.

## Sources

Numbered notes identify the exact source and its evidence baseline. Repository
links within this proposal resolve against the checked-out RFC revision.

[^1]: agentplugins/agent-plugins-spec. [Commit, commit `1fc1b6270e3cc492ec2d24ad7a34277c6d53b9c1`](https://github.com/agentplugins/agent-plugins-spec/commit/1fc1b6270e3cc492ec2d24ad7a34277c6d53b9c1). July 24, 2026; inspected September 13–14, 2026.

[^2]: Vercel. [Introducing Agent Plugins](https://vercel.com/blog/introducing-agent-plugins). August 6, 2026.

[^3]: GitHub. [Agent Plugins 1.0 in VS Code, Copilot CLI, and the Copilot app](https://github.blog/changelog/2026-08-12-agent-plugins-1-0-in-vs-code-copilot-cli-and-the-copilot-app/). August 12, 2026.

[^4]: Cognition, Devin. [Devin changelog](https://docs.devin.ai/cli/changelog/stable). Documentation inspected September 13–14, 2026.

[^5]: agentplugins/agent-plugins-spec. [GOVERNANCE.md](https://github.com/agentplugins/agent-plugins-spec/blob/main/GOVERNANCE.md). revision `main`; inspected September 13–14, 2026.

[^6]: Agent Skills maintainers. [Agent Skills specification](https://agentskills.io/specification). Documentation inspected September 13–14, 2026.

[^7]: Agent Plugins maintainers. [Plugin manifest](https://agent-plugins.org/plugin-authors/manifest). Documentation inspected September 13–14, 2026.

[^8]: Agent Plugins maintainers. [Client extensions](https://agent-plugins.org/plugin-authors/client-extensions). Documentation inspected September 13–14, 2026.

[^9]: Agent Plugins maintainers. [Loading and discovery](https://agent-plugins.org/client-implementers/loading-and-discovery). Documentation inspected September 13–14, 2026.

[^10]: Agent Plugins maintainers. [MCP authoring](https://agent-plugins.org/plugin-authors/mcp-servers). Documentation inspected September 13–14, 2026.

[^11]: Agent Plugins maintainers. [MCP runtime](https://agent-plugins.org/client-implementers/mcp-runtime). Documentation inspected September 13–14, 2026.

[^12]: Anthropic. [Skill reference](https://code.claude.com/docs/en/skills). Documentation inspected September 13–14, 2026.

[^13]: Anthropic. [Subagent reference](https://code.claude.com/docs/en/sub-agents). Documentation inspected September 13–14, 2026.

[^14]: Anthropic. [Plugin reference](https://code.claude.com/docs/en/plugins-reference). Documentation inspected September 13–14, 2026.

[^15]: Anthropic. [Hook reference](https://code.claude.com/docs/en/hooks). Documentation inspected September 13–14, 2026.

[^16]: OpenAI. [Plugin packaging](https://developers.openai.com/plugins/build/plugins). Documentation inspected September 13–14, 2026.

[^17]: OpenAI. [Skill documentation](https://learn.chatgpt.com/docs/build-skills). Documentation inspected September 13–14, 2026.

[^18]: OpenAI. [Subagent configuration](https://learn.chatgpt.com/docs/agent-configuration/subagents). Documentation inspected September 13–14, 2026.

[^19]: openai/codex. [Released frontmatter parser](https://github.com/openai/codex/blob/6b9826e3aa83b1a5947db50f4332cb9c65f1b340/codex-rs/skills/src/parser.rs#L4). Codex 0.154.0, released September 9, 2026; inspected September 13–14, 2026.

[^20]: openai/codex. [Overlong-description regression test](https://github.com/openai/codex/blob/6b9826e3aa83b1a5947db50f4332cb9c65f1b340/codex-rs/skills/src/parser_tests.rs#L100). Codex 0.154.0, released September 9, 2026; inspected September 13–14, 2026.

[^21]: openai/codex. [codex-rs/ext/skills/src/render.rs](https://github.com/openai/codex/blob/6b9826e3aa83b1a5947db50f4332cb9c65f1b340/codex-rs/ext/skills/src/render.rs). Codex 0.154.0, released September 9, 2026; inspected September 13–14, 2026.

[^22]: openai/codex. [codex-rs/ext/skills](https://github.com/openai/codex/tree/6b9826e3aa83b1a5947db50f4332cb9c65f1b340/codex-rs/ext/skills). Codex 0.154.0, released September 9, 2026; inspected September 13–14, 2026.

[^23]: openai/codex. [codex-rs/ext/skills/src/host_prompt.rs](https://github.com/openai/codex/blob/6b9826e3aa83b1a5947db50f4332cb9c65f1b340/codex-rs/ext/skills/src/host_prompt.rs#L69). Codex 0.154.0, released September 9, 2026; inspected September 13–14, 2026.

[^24]: openai/codex. [codex-rs/ext/skills/src/host_outcome.rs](https://github.com/openai/codex/blob/6b9826e3aa83b1a5947db50f4332cb9c65f1b340/codex-rs/ext/skills/src/host_outcome.rs#L115). Codex 0.154.0, released September 9, 2026; inspected September 13–14, 2026.

[^25]: openai/codex. [codex-rs/ext/skills/src/render.rs](https://github.com/openai/codex/blob/6b9826e3aa83b1a5947db50f4332cb9c65f1b340/codex-rs/ext/skills/src/render.rs#L1176). Codex 0.154.0, released September 9, 2026; inspected September 13–14, 2026.

[^26]: openai/codex. [codex-rs/ext/skills/src/extension.rs](https://github.com/openai/codex/blob/6b9826e3aa83b1a5947db50f4332cb9c65f1b340/codex-rs/ext/skills/src/extension.rs#L457). Codex 0.154.0, released September 9, 2026; inspected September 13–14, 2026.

[^27]: OpenAI. [Import contract](https://developers.openai.com/plugins/build/mcp-server#import-skills-from-the-mcp-server). Documentation inspected September 13–14, 2026.

[^28]: OpenAI. [Hook documentation](https://learn.chatgpt.com/docs/hooks). Documentation inspected September 13–14, 2026.

[^29]: OpenAI. [MCP documentation](https://learn.chatgpt.com/docs/extend/mcp). Documentation inspected September 13–14, 2026.

[^30]: Cursor. [Plugin reference](https://prod.cursor.com/docs/reference/plugins). Documentation inspected September 13–14, 2026.

[^31]: Cursor. [Plugin overview](https://prod.cursor.com/docs/plugins). Documentation inspected September 13–14, 2026.

[^32]: Cursor. [Skills](https://prod.cursor.com/docs/skills). Documentation inspected September 13–14, 2026.

[^33]: Cursor. [Subagents](https://prod.cursor.com/docs/subagents). Documentation inspected September 13–14, 2026.

[^34]: Cursor. [Rules](https://prod.cursor.com/docs/rules). Documentation inspected September 13–14, 2026.

[^35]: Cursor. [Hooks](https://prod.cursor.com/docs/hooks). Documentation inspected September 13–14, 2026.

[^36]: Google, Gemini CLI. [Extension reference](https://geminicli.com/docs/extensions/reference/). Documentation inspected September 13–14, 2026.

[^37]: google-gemini/gemini-cli. [packages/core/src/skills/skillLoader.ts](https://github.com/google-gemini/gemini-cli/blob/v0.59.0/packages/core/src/skills/skillLoader.ts). Gemini CLI 0.59.0, released September 8, 2026; inspected September 13–14, 2026.

[^38]: google-gemini/gemini-cli. [packages/core/src/skills/builtin/skill-creator/scripts/validate_skill.cjs](https://github.com/google-gemini/gemini-cli/blob/v0.59.0/packages/core/src/skills/builtin/skill-creator/scripts/validate_skill.cjs). Gemini CLI 0.59.0, released September 8, 2026; inspected September 13–14, 2026.

[^39]: google-gemini/gemini-cli. [packages/core/src/agents/agentLoader.ts](https://github.com/google-gemini/gemini-cli/blob/v0.59.0/packages/core/src/agents/agentLoader.ts#L92). Gemini CLI 0.59.0, released September 8, 2026; inspected September 13–14, 2026.

[^40]: Google, Gemini CLI. [Subagent docs](https://geminicli.com/docs/core/subagents/). Documentation inspected September 13–14, 2026.

[^41]: google-gemini/gemini-cli. [packages/cli/src/services/FileCommandLoader.ts](https://github.com/google-gemini/gemini-cli/blob/v0.59.0/packages/cli/src/services/FileCommandLoader.ts). Gemini CLI 0.59.0, released September 8, 2026; inspected September 13–14, 2026.

[^42]: google-gemini/gemini-cli. [packages/cli/src/services/prompt-processors/atFileProcessor.ts](https://github.com/google-gemini/gemini-cli/blob/v0.59.0/packages/cli/src/services/prompt-processors/atFileProcessor.ts). Gemini CLI 0.59.0, released September 8, 2026; inspected September 13–14, 2026.

[^43]: google-gemini/gemini-cli. [packages/core/src/tools/activate-skill.ts](https://github.com/google-gemini/gemini-cli/blob/v0.59.0/packages/core/src/tools/activate-skill.ts). Gemini CLI 0.59.0, released September 8, 2026; inspected September 13–14, 2026.

[^44]: google-gemini/gemini-cli. [packages/cli/src/config/extension-manager.ts](https://github.com/google-gemini/gemini-cli/blob/v0.59.0/packages/cli/src/config/extension-manager.ts#L758). Gemini CLI 0.59.0, released September 8, 2026; inspected September 13–14, 2026.

[^45]: Google, Gemini CLI. [Context hierarchy](https://geminicli.com/docs/cli/gemini-md/#understand-the-context-hierarchy). Documentation inspected September 13–14, 2026.

[^46]: Google, Gemini CLI. [Hook contract](https://geminicli.com/docs/hooks/reference/). Documentation inspected September 13–14, 2026.

[^47]: google-gemini/gemini-cli. [packages/cli/src/commands/hooks/migrate.ts](https://github.com/google-gemini/gemini-cli/blob/v0.59.0/packages/cli/src/commands/hooks/migrate.ts). Gemini CLI 0.59.0, released September 8, 2026; inspected September 13–14, 2026.

[^48]: GitHub. [Plugin concept](https://docs.github.com/en/copilot/concepts/agents/about-plugins). Documentation inspected September 13–14, 2026.

[^49]: GitHub. [CLI plugin reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-plugin-reference). Documentation inspected September 13–14, 2026.

[^50]: Microsoft, Visual Studio Code. [VS Code skills](https://code.visualstudio.com/docs/agent-customization/agent-skills). Documentation inspected September 13–14, 2026.

[^51]: Microsoft, Visual Studio Code. [Prompt files](https://code.visualstudio.com/docs/agent-customization/prompt-files). Documentation inspected September 13–14, 2026.

[^52]: GitHub. [Custom agents](https://docs.github.com/en/copilot/reference/custom-agents-configuration). Documentation inspected September 13–14, 2026.

[^53]: microsoft/vscode. [src/vs/workbench/contrib/chat/common/promptSyntax/service/promptsServiceImpl.ts](https://github.com/microsoft/vscode/blob/e6a2323c8796874e969e970de97eed3696ed6fef/src/vs/workbench/contrib/chat/common/promptSyntax/service/promptsServiceImpl.ts). development revision, September 14, 2026; not a release baseline; inspected September 13–14, 2026.

[^54]: GitHub. [Copilot hooks](https://docs.github.com/en/copilot/reference/hooks-reference). Documentation inspected September 13–14, 2026.

[^55]: Microsoft, Visual Studio Code. [VS Code hooks](https://code.visualstudio.com/docs/agent-customization/hooks). Documentation inspected September 13–14, 2026.

[^56]: Cognition, Devin. [CLI plugins](https://docs.devin.ai/cli/extensibility/plugins/overview). Documentation inspected September 13–14, 2026.

[^57]: Cognition, Devin. [Skill authoring](https://docs.devin.ai/cli/extensibility/skills/creating-skills). Documentation inspected September 13–14, 2026.

[^58]: Cognition, Devin. [Subagents](https://docs.devin.ai/cli/subagents). Documentation inspected September 13–14, 2026.

[^59]: Cognition, Devin. [Rules](https://docs.devin.ai/cli/extensibility/rules). Documentation inspected September 13–14, 2026.

[^60]: Cognition, Devin. [Hooks](https://docs.devin.ai/cli/extensibility/hooks/overview). Documentation inspected September 13–14, 2026.

[^61]: Cognition, Devin. [Cloud plugins](https://docs.devin.ai/product-guides/plugins). Documentation inspected September 13–14, 2026.

[^62]: OpenClaw. [Bundle mapping](https://docs.openclaw.ai/plugins/bundles#what-openclaw-maps-from-bundles). Documentation inspected September 13–14, 2026.

[^63]: openclaw/openclaw. [src/plugins/bundle-manifest.ts](https://github.com/openclaw/openclaw/blob/v2026.9.4/src/plugins/bundle-manifest.ts). OpenClaw 2026.9.4, released September 11, 2026; inspected September 13–14, 2026.

[^64]: OpenClaw. [Native manifest](https://docs.openclaw.ai/plugins/manifest). Documentation inspected September 13–14, 2026.

[^65]: OpenClaw. [Skills](https://docs.openclaw.ai/tools/skills). Documentation inspected September 13–14, 2026.

[^66]: openclaw/openclaw. [src/skills/loading/frontmatter.ts](https://github.com/openclaw/openclaw/blob/v2026.9.4/src/skills/loading/frontmatter.ts). OpenClaw 2026.9.4, released September 11, 2026; inspected September 13–14, 2026.

[^67]: openclaw/openclaw. [src/skills/loading/skill-root-discovery.ts](https://github.com/openclaw/openclaw/blob/v2026.9.4/src/skills/loading/skill-root-discovery.ts). OpenClaw 2026.9.4, released September 11, 2026; inspected September 13–14, 2026.

[^68]: openclaw/openclaw. [src/skills/loading/skill-prompt-limits.ts](https://github.com/openclaw/openclaw/blob/v2026.9.4/src/skills/loading/skill-prompt-limits.ts). OpenClaw 2026.9.4, released September 11, 2026; inspected September 13–14, 2026.

[^69]: openclaw/openclaw. [src/skills/loading/local-loader.ts](https://github.com/openclaw/openclaw/blob/v2026.9.4/src/skills/loading/local-loader.ts). OpenClaw 2026.9.4, released September 11, 2026; inspected September 13–14, 2026.

[^70]: OpenClaw. [Loading order](https://docs.openclaw.ai/tools/skills#loading-order). Documentation inspected September 13–14, 2026.

[^71]: OpenClaw. [Subagent tool](https://docs.openclaw.ai/tools/subagents/tool-reference). Documentation inspected September 13–14, 2026.

[^72]: OpenClaw. [Subagent policy](https://docs.openclaw.ai/tools/subagents/tool-policy). Documentation inspected September 13–14, 2026.

[^73]: NousResearch/hermes-agent. [hermes_cli/agent_plugins.py](https://github.com/NousResearch/hermes-agent/blob/v2026.9.11/hermes_cli/agent_plugins.py). Hermes 2026.9.11, released September 11, 2026; inspected September 13–14, 2026.

[^74]: Nous Research, Hermes Agent. [Native skill authoring](https://hermes-agent.nousresearch.com/docs/developer-guide/creating-skills#skillmd-format). Documentation inspected September 13–14, 2026.

[^75]: Nous Research, Hermes Agent. [Plugin skills](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins#bundle-skills). Documentation inspected September 13–14, 2026.

[^76]: NousResearch/hermes-agent. [tools/skills_tool_plugin.py](https://github.com/NousResearch/hermes-agent/blob/v2026.9.11/tools/skills_tool_plugin.py). Hermes 2026.9.11, released September 11, 2026; inspected September 13–14, 2026.

[^77]: NousResearch/hermes-agent. [agent/skill_utils.py](https://github.com/NousResearch/hermes-agent/blob/v2026.9.11/agent/skill_utils.py). Hermes 2026.9.11, released September 11, 2026; inspected September 13–14, 2026.

[^78]: NousResearch/hermes-agent. [tools/skills_tool.py](https://github.com/NousResearch/hermes-agent/blob/v2026.9.11/tools/skills_tool.py). Hermes 2026.9.11, released September 11, 2026; inspected September 13–14, 2026.

[^79]: Nous Research, Hermes Agent. [Native plugins](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins). Documentation inspected September 13–14, 2026.

[^80]: NousResearch/hermes-agent. [hermes_cli/plugins_dispatch.py](https://github.com/NousResearch/hermes-agent/blob/v2026.9.11/hermes_cli/plugins_dispatch.py). Hermes 2026.9.11, released September 11, 2026; inspected September 13–14, 2026.

[^81]: Nous Research, Hermes Agent. [Subagent lifecycle API](https://hermes-agent.nousresearch.com/docs/developer-guide/subagent-lifecycle-api). Documentation inspected September 13–14, 2026.

[^82]: Promptless/instruction-hub-toolchain. [Toolchain PR73, pull request #73](https://github.com/Promptless/instruction-hub-toolchain/pull/73). Status and content inspected September 13–14, 2026.

[^83]: Promptless/instruction-hub. [Hub PR1345, pull request #1345](https://github.com/Promptless/instruction-hub/pull/1345). Status and content inspected September 13–14, 2026.

[^84]: Promptless/instruction-hub-toolchain. [src/promptless_instruction_hub/agent_skills.py](https://github.com/Promptless/instruction-hub-toolchain/blob/6d795cd50af79c9710060f215804985b72d8dfd9/src/promptless_instruction_hub/agent_skills.py). revision `6d795cd50af79c9710060f215804985b72d8dfd9`; inspected September 13–14, 2026.

[^85]: Promptless/instruction-hub. [assets/agents/leadgen-experiment-planner.md](https://github.com/Promptless/instruction-hub/blob/42927ff8e359f139ca4db40b1c73b692efc89bd2/assets/agents/leadgen-experiment-planner.md). revision `42927ff8e359f139ca4db40b1c73b692efc89bd2`; inspected September 13–14, 2026.

[^86]: Promptless/instruction-hub. [assets/agents/prospect-docs-analyzer.md](https://github.com/Promptless/instruction-hub/blob/42927ff8e359f139ca4db40b1c73b692efc89bd2/assets/agents/prospect-docs-analyzer.md). revision `42927ff8e359f139ca4db40b1c73b692efc89bd2`; inspected September 13–14, 2026.

[^87]: Promptless/instruction-hub-toolchain. [tests/instruction_hub/test_agent_skills.py](https://github.com/Promptless/instruction-hub-toolchain/blob/6d795cd50af79c9710060f215804985b72d8dfd9/tests/instruction_hub/test_agent_skills.py). revision `6d795cd50af79c9710060f215804985b72d8dfd9`; inspected September 13–14, 2026.

[^88]: Promptless/instruction-hub-toolchain. [tests/instruction_hub/helpers.py](https://github.com/Promptless/instruction-hub-toolchain/blob/6d795cd50af79c9710060f215804985b72d8dfd9/tests/instruction_hub/helpers.py#L45). revision `6d795cd50af79c9710060f215804985b72d8dfd9`; inspected September 13–14, 2026.

[^89]: Promptless/instruction-hub. [Pinned inventory](https://github.com/Promptless/instruction-hub/tree/6c779ebecc0a7f898ddc51dfe15e8d251feeb646). Inspected September 13–14, 2026.
