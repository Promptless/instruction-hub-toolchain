# RFC: Portable Instruction Hub assets and explicit harness adaptations

**Status:** Draft for team review

**Research baseline:** September 13–14, 2026

**Scope of this PR:** RFC materials; compiler changes and runtime qualification are proposed follow-up work.

**Companion:** [Evidence appendix](portable-assets-evidence.md)

## Summary

Instruction Hub should let teams author reusable procedures and roles, then
compile them into deliberate implementations for their chosen agent harnesses.

The source contract describes the work and its requirements. Target adapters
choose the packaging, frontmatter, invocation mechanism, resource access, and
execution settings that implement those requirements. Each compiled package
discloses meaningful differences and identifies the evidence supporting its
compatibility claims.

An asset can retain its identity while changing representation. An agent role
might compile into a native Claude agent and a Codex skill that delegates to a
child. That conversion can be useful without implying identical execution
behavior. Conversely, successfully copying a file into a package does not show
that the harness discovers it or honors its instructions and configuration.

This RFC recommends a small portable Markdown core, optional compiler metadata,
explicit target adaptations, and compatibility reports evaluated against each
hub's selected targets. It deliberately separates source validity, generated
package validity, and observed runtime behavior.

## Decision record

### Established direction

These choices were established during the design discussion. They define the
RFC's scope; they do not mean the team has already approved its proposed schema
or implementation.

| ID | Topic | Direction and rationale |
|---|---|---|
| D01 | Objective | Propose a fresh source contract and design philosophy for team approval. |
| D02 | Research scope | Cover Claude Code, Codex, Cursor, Gemini CLI, GitHub Copilot, Devin, OpenClaw, and Hermes. |
| D03 | Initial runtime qualification | Start with Claude Code, Codex, Cursor, and Gemini. Qualify concrete surfaces; do not infer CLI, desktop, IDE, or cloud parity. |
| D04 | Deliverable | A Markdown RFC and substantial evidence appendix, with examples and alternatives. |
| D05 | Compatibility scope | Validate each hub's and plugin's declared targets. Different customers and users can select different harness sets. |
| D06 | Installation contract | Compiled packages are the supported installation interface. Sophisticated source assets can rely on compiler-only metadata. |
| D07 | Plugin composition | Keep explicit asset membership. Validate dependencies without silently adding assets. |
| D08 | Adaptation | Decide case by case and disclose differences. PR73's agent-to-skill conversion is a worked example. |
| D09 | Implementation boundary | Generate bundles and thin harness adapters; avoid a universal PIG agent runtime. |
| D10 | Source layout | Compare portable Markdown plus sidecar with richer compiler frontmatter, then recommend. |
| D11 | Model policy | Compare inheritance with exceptions against hub-owned execution profiles, then recommend. |
| D12 | Semantic generation | Keep builds deterministic; examine reviewed LLM-assisted variants that preserve previous versions. |
| D13 | Existing implementation | Use the compiler and real assets as evidence without allowing migration constraints to dictate the ideal contract. |
| D14 | Decision tracking | Retain these choices and distinguish them from recommendations awaiting team approval. |

### Recommendations for team approval

| ID | Recommendation | Principal tradeoff |
|---|---|---|
| R01 | Portable Markdown plus optional compiler metadata is the default authoring format. | More than one file for complex assets, in exchange for clear ownership of runtime policy. |
| R02 | Logical kind and identity remain separate from target representation. | Adapters must describe more than output paths. |
| R03 | Compatibility names harness, surface, package route, and tested version. | More precise support records than a vendor-level boolean. |
| R04 | Adaptations record preserved behavior, differences, acceptance, and evidence. | Review effort for meaningful differences rather than silent degradation. |
| R05 | Size checks measure rendered output and distinguish instruction, catalog, resource, and distribution limits. | More than one budget, because the limits act at different stages. |
| R06 | Extend the existing plugin catalog with compatibility information. | Additional release metadata and presentation work. |
| R07 | Model selection inherits by default; hub-owned profiles and target exceptions are opt-in. | Explicit policy only where workflows justify its maintenance. |
| R08 | Semantic generation is an authoring aid; builds consume reviewed artifacts. | Variants can become stale and require review. |

## Current behavior and the standards boundary

The current installation contract already distributes compiled output. The
toolchain renders `dist/{target}/{plugin.id}/`, publishes generated artifacts to
the release branch, and updates marketplace pointers. Raw assets are authoring
inputs. [Distribution contract](../../README.md#marketplace-and-plugin-identities)

The compiler already has hub-level `stable_plugins` and `targets`, explicit
plugin `includes`, asset support modes, and release manifests containing
composition and hashes. Its support model primarily describes delivery mode and
reason. It does not establish tested runtime fidelity. Extend this inventory
rather than introducing a competing catalog.
[Source models](../../src/promptless_instruction_hub/models.py),
[Release manifests](../../src/promptless_instruction_hub/release/manifests.py)

Agent Plugins 1.0 defines portable packaging and discovery for skills and MCP
servers. Agents, hooks, rules, and commands remain outside its common component
contract, and skill exposure to users and models remains client-specific. Using
the format therefore helps distribution without resolving all of this RFC's
behavioral questions. Agent Plugins specification[^1]

The [appendix](portable-assets-evidence.md#standards-and-history) separates the
July 24 repository publication marker, August 6 public launch, and subsequent
dated client announcements. Plugin support predates that common format in
several harnesses; native plugin support and Agent Plugins conformance are
different claims.

## Three contracts

| Contract | Responsibility | Example |
|---|---|---|
| Authored intent | Purpose, procedure, inputs, outputs, dependencies, and required behavior. | Have an independent specialist review the change and return findings. |
| Compiled representation | Files, frontmatter, manifests, wrappers, names, and resource locations. | A Claude agent file or a Codex delegated skill. |
| Runtime behavior | What the selected harness discovers, loads, permits, and executes. | A child launched, read the procedure, and returned findings. |

A package schema can validate representation. It cannot prove runtime behavior.
A successful workflow test supplies evidence under recorded conditions, not a
guarantee of identical model behavior on arbitrary tasks.

## Source contract

### Asset concepts and identity

Retain distinct source concepts:

| Kind | Source responsibility |
|---|---|
| Skill | Reusable instructions, knowledge, procedure, and supporting resources. |
| Agent | A delegated role, task boundaries, interaction requirements, and result contract. |
| Command | An explicitly invoked workflow and its argument contract. |
| Rule | Instructions with an intended applicability scope. |
| Hook | Event-driven behavior with specified timing and failure semantics. |
| MCP | A tool-server connection or launch definition and runtime prerequisites. |

The source identity, target invocation identity, and output destination are
related but distinct. Preserve `agent:reviewer` even if a target implements it as
a skill. Validate collisions after target naming and normalization, including
collisions between authored and generated skills.

### Common Markdown frontmatter

For skills, use this Agent Skills baseline:

| Field | Proposed source rule |
|---|---|
| `name` | Required; stable identifier matching the skill directory; 1–64 ASCII lowercase letters, digits, and hyphens, with alphanumeric ends and no consecutive hyphens. |
| `description` | Required, nonempty discovery text, at most 1,024 Unicode characters. Target checks additionally apply their actual counting and normalization rules. |
| `license` | Optional string. |
| `compatibility` | Optional nonempty string, 1–500 characters when present, describing the environment. It is not an executable compatibility declaration. |
| `metadata` | Optional string-to-string metadata; no nested compiler policy or runtime guarantees. |

The standard also defines experimental `allowed-tools`. Do not make it the Hub's
neutral permission contract: consumers disagree on its operational meaning.
Agent Skills specification[^2]

Parse and render frontmatter deliberately. Unknown source fields should produce
a useful diagnostic instead of passing unchanged through every target. Reject
ambiguous duplicate keys and misleading types before rendering. Native fields
can be represented through a validated target binding; imported legacy content
needs an explicit migration path rather than silent field removal.

For agent and command Markdown, reuse identity and description conventions where
useful while validating kind-specific schemas. An authored agent need not pretend
to be a skill. The Hub source schema, Agent Plugins format version, adapter
version, harness version, and plugin release version have separate meanings.

### Operational metadata

Use optional `asset.yaml` for directory assets and an adjacent sidecar for file
assets. Keep the following information compiler-owned:

| Concern | Information to represent |
|---|---|
| Invocation | Automatic selection, explicit invocation, and user-facing visibility separately. |
| Execution | Delegation intent, context requirements, model policy, and required user interaction. |
| Dependencies | Required Hub assets, external capabilities, and workflow-specific unavailable behavior. |
| Applicability | Always-on, directory, file-pattern, or explicit-only scope. |
| Target bindings | Selected adapter, target configuration, and accepted behavioral differences. |
| Variants | References to reviewed target content when mechanical compilation is insufficient. |

Avoid duplicating identity and description between Markdown and the sidecar.
Derive structural identity consistently and check that authored names agree.
Use a versioned source schema so future metadata changes are distinguishable from
ordinary content edits.

### Sidecar versus richer frontmatter

| Approach | Advantages | Costs |
|---|---|---|
| **Portable Markdown plus optional sidecar — recommended** | Simple skills stay simple; compiler controls stay out of runtime instructions; works for non-Markdown assets. | Complex assets span files; authoring tools must show them together. |
| Rich compiler frontmatter | Procedure and settings are visible in one file. | Compiler fields require stripping or translation; encourages confusing native fields with portable semantics; awkward for MCP and hooks. |
| Native frontmatter as canonical source | Convenient when authoring for one harness. | That harness's assumptions become the implicit source schema. |

A richer-frontmatter design can work if compiler fields live in a clearly owned
block and output is re-rendered. The issue is semantic ownership, not YAML.

Here is the same proposed manual-workflow intent in both layouts. These snippets
illustrate the schema design under discussion; **the current compiler does not
accept these new fields**. They are not runnable configuration examples.

**Portable Markdown plus sidecar:**

```markdown
---
name: prepare-release
description: Prepare a release checklist when explicitly requested.
---

Read references/release-checklist.md before preparing the checklist.
Return the checklist and unresolved prerequisites to the user.
```

```yaml
# asset.yaml — proposed compiler metadata
invocation:
  automatic: false
  user_visible: true
bindings:
  gemini-cli:
    adapter: manual-command
    resources: resolved-bundle-root
```

**Richer compiler frontmatter:**

```markdown
---
name: prepare-release
description: Prepare a release checklist when explicitly requested.
hub:
  invocation:
    automatic: false
    user_visible: true
  bindings:
    gemini-cli:
      adapter: manual-command
      resources: resolved-bundle-root
---

Read references/release-checklist.md before preparing the checklist.
Return the checklist and unresolved prerequisites to the user.
```

Both require a real implementation of the resource adapter. Merely copying this
metadata cannot make the workflow portable. With the sidecar layout, simple
skills need only their existing Markdown and resources; complex assets acquire
compiler metadata as needed. Compiled packages remain the supported interface.

### Procedure prose

Shared instructions describe the task, evidence, inputs, procedure, expected
output, authorized actions, clarification requirements, stopping conditions, and
behavior when dependencies are unavailable.

Prefer “read the public documentation” over `WebFetch` unless its specific
behavior is essential. Prefer “delegate this review to an independent specialist”
over “launch an Opus subagent.” Put intentional model selection in target policy.

This is not a ban on vendor terminology. A skill about configuring Claude should
discuss Claude. Lint likely execution assumptions and explain the concern; do not
mechanically rewrite product names, examples, or quoted evidence.

### Tools, restrictions, and approval

| Concept | Example |
|---|---|
| Required capability | The workflow must read a public website. |
| Tool availability | The child has filesystem and web tools. |
| Approval policy | Particular tool calls may proceed without another prompt. |
| Instructional restriction | The specialist must not send outreach. |
| Enforced restriction | The runtime prevents the specialist from sending outreach. |

Identical field names do not establish identical controls. Claude skill
`allowed-tools` concerns preapproval, Devin documents a tool restriction, and
some loaders accept a field without implementing either effect.
Claude skills[^3],
Devin skills[^4],
Hermes loader[^5]

An adaptation may preserve a restriction as instructions when that is explicitly
accepted for the workflow. Its report must say that enforcement changed. If the
requirement is runtime enforcement, prose alone cannot satisfy it. Neither a
tool requirement nor a plugin installation should implicitly grant preapproval.

### Model policy

| Approach | Strength | Limitation |
|---|---|---|
| Inherit with explicit exceptions | Respects the user's environment; avoids mappings for ordinary workflows. | Some roles need deliberate capability, cost, or execution policy. |
| Hub-owned named profiles | Customers can maintain consistent policies for selected roles and targets. | Configuration and maintenance; names do not establish equivalent model quality. |
| Compiler-owned “Opus equivalent” mappings | Convenient shorthand. | Hides a product judgment and becomes stale as models and availability change. |

Recommend inheritance by default, with optional hub-owned profiles and explicit
target exceptions. A profile records whether a model choice is preferred or
required and what happens if it is unavailable. Exact model IDs and any reasoning
settings belong to target policy. Do not silently equate providers' model or
effort names.

For example, a shared reviewer role asks for independent review. A hub may use
inheritance everywhere, or bind that role to its own review profile. That profile
may choose a Claude model explicitly and leave Codex inheriting. The report then
describes those choices without claiming equivalent quality.

Implement and test inheritance itself. Omitting a field does not always inherit:
Devin's custom agent can default to a configured router model, while its general
profile inherits. Devin subagents[^6]

### Dependencies and resources

Keep explicit plugin membership. Required internal dependencies must appear in
that plugin's effective contents for the selected target. Report missing or
unavailable dependencies; do not silently expand the plugin. An optional
dependency requires a defined behavior without it. An intentionally skipped asset
cannot satisfy another asset's required dependency.

Distinguish Hub references, external plugins/tools, MCP connections, installed
executables, platform prerequisites, authentication, and configuration. Builds
must not install external plugins, authenticate services, or embed credentials.
Cross-plugin dependency resolution is outside the first composition contract.

Package resources for relocation. Avoid source-checkout and current-directory
assumptions; preserve executable permissions and separate mutable state from the
immutable payload. A target reader may be narrower than ordinary filesystem
access: Hermes's plugin reader disallows paths outside an individual skill root,
so a shared resource may need a packaged copy inside that skill.
Hermes resource serving[^7]

## Targets, compilation, and catalog

### Target profiles

A compatibility profile identifies harness, surface, package/loading route,
tested version or range, relevant platform/configuration prerequisites,
capabilities, limitations, and evidence.

“Codex CLI, native plugin” and “Codex, portable Agent Plugins” can follow different
loading paths. “Copilot” spans multiple products. Do not infer parity between a
local CLI, IDE, desktop app, SDK, and hosted service.

Prefer separate artifacts where routes differ. Shipping all manifests together
can select an unexpected parser: OpenClaw and Hermes prioritize native and
compatibility markers over portable ones in defined orders.
OpenClaw routing[^8],
Hermes discovery[^9]

### Pipeline

1. Parse and validate source.
2. Resolve explicitly included assets and validate dependencies.
3. Resolve the plugin's selected target profiles.
4. Select approved adapters and reviewed variants.
5. Render files, frontmatter, names, wrappers, and resources.
6. Validate the complete generated package against its target.
7. Produce artifacts and a compatibility report with provenance.

Use versioned local schemas and adapters. Ordinary builds should not retrieve
new policy from live documentation or regenerate semantic variants.

### Catalog behavior

Hub configuration declares available targets. Plugins inherit defaults or select
a subset; validation evaluates those combinations. Users install a published
combination appropriate to their environment. Supporting another customer's
harness should not force unrelated hubs onto its restrictions.

For each plugin/target combination publish:

| Information | Purpose |
|---|---|
| Logical assets | Explain what the plugin contains. |
| Generated representations and invocation names | Explain how to access them. |
| Adaptations and omissions | Disclose meaningful differences. |
| Prerequisites | Separate distribution from runtime readiness. |
| Tested profile and evidence | Bound support claims. |
| Source and output provenance | Support reproduction and diagnosis. |

Represent delivery, adaptation, and evidence independently. A native artifact can
still be untested; an adapted artifact can have strong runtime evidence.
Published, installed, enabled, authenticated, and trusted are distinct states.

### Adaptation records

Use named, reviewable adapters for recurring conversions and record asset-specific
exceptions. Do not require a separate approval prompt on every build: review the
policy and concrete adaptations together through normal changes.

Each record states the source identity, target profile, chosen representation,
preserved behavior, changed behavior, accepted differences, dependency behavior,
resource access, evidence, and remaining uncertainty. A format migration should
produce a compatibility diff even when procedure text is unchanged.

## Worked adaptations

### A. Agent role to delegated skill

PR73[^10] preserves
`agent:<id>` while emitting a native Claude agent and a Codex skill. The Codex
adapter retains the procedure and instructs the parent to launch one specialist
child for the role and relay questions and results. It instructs the assigned
child to execute the role without recursively delegating that same role.
It omits the source model choice and makes tool restrictions advisory.
Renderer[^11]

Record context transfer, instructional versus enforced delegation, tool-policy
changes, model choice, required user interaction, and unavailable-subagent
behavior. This is an intentional adapter, not a universal equivalence between
agents and skills. Its [full record](portable-assets-evidence.md#pr73-worked-record)
also distinguishes structural tests from runtime evidence.

Missing capabilities need workflow-specific treatment. The paired Hub changes
allow the planner to return an unpublished plan without Notion; the documentation
analyzer requires web access to verify its claim.
Planner[^12],
Analyzer[^13]

### B. Manual workflow to Gemini command

Gemini can expose `commands/<id>.toml` without placing the workflow in discovered
skill directories. That preserves explicit invocation. However, command file
injection is workspace-based, and commands do not receive skill activation's
directory-access behavior. The loader does not hydrate `${extensionPath}`.
Command loader[^14],
Skill activation[^15]

A resource-bearing command needs a tested resolver or suitable inlining strategy,
including linked installations. Unchanged relative links are insufficient. Manual
invocation is a discovery/activation property, not an authorization boundary.

### C. Scoped rules

Cursor has native rule activation controls. Gemini's project context discovery
is directory-based. A Gemini adapter can project context into the project
hierarchy, load a conditional instruction more broadly from extension context,
or decline an exact file-triggered loading guarantee.
Cursor rules[^16],
Gemini context[^17]

The broader conditional instruction preserves applicability prose but changes
context loading. Accept that difference case by case. A nested context file
inside an extension does not automatically scope itself to the corresponding
project directory. Project projection also requires an installation mechanism
beyond simply shipping a plugin bundle.

### D. Hooks

Map event timing, payloads, matching, cwd, output interpretation, timeout units,
and failure behavior. Gemini uses millisecond timeouts while several other
formats use seconds. A before-tool denial, after-tool output filter, and retry
instruction have different effects.
Gemini hooks[^18],
Claude hooks[^19]

Reuse scripts where feasible with target-specific bindings and thin adapters.
Similar event names do not establish equivalence. Observational hooks can
sometimes tolerate omission; workflows requiring blocking need that behavior.
Do not describe parsed-but-skipped handlers as supported execution.

### E. MCP and credentials

Separate transport, launch configuration, path expansion, and authentication.
Portable MCP has a closed configuration shape. Its selected stdio fields expand
`${PLUGIN_ROOT}` and `${PLUGIN_DATA}`; it does not define generic secret
interpolation or portable OAuth configuration.
Portable MCP[^20]

Copying `${API_KEY}` into a portable header does not reproduce native credential
handling. Keep authentication client-owned and render supported target-specific
references deliberately. Report unsupported transports instead of silently
substituting another transport.

### F. Oversized entrypoints

Do not impose one universal hard skill-length limit. Measure field length,
whole-file bytes, body tokens/lines, aggregate discovery catalogs, supporting
resources, and distribution archives separately. Distinguish readability guidance
from rejection, truncation, and context retention.

At the inspected Hub revision, 57 skill entrypoints included 26 above 8,000 bytes
and none above 500 lines. A bundled HTML template was 4,085,073 bytes. These
measurements show why line counts, entrypoint bytes, and resource sizes answer
different questions. Inventory baseline[^21]

PR73's generated planner and analyzer skills measure 13,753 and 9,441 bytes,
respectively; each gains more than 2 KB from the wrapper and frontmatter.
Source-only checks miss that overhead.

Codex 0.154.0's portable host injection truncates the complete file, including
frontmatter, at 8,000 UTF-8 bytes. The legacy host branch for native-plugin skills
bypasses that particular cap. The installed file remains complete: this
is an injection limit, not a universal file rejection.
Host injection[^22],
Truncation[^23]

Possible remedies are a short entrypoint loading the complete procedure, an
authored reference split, a reviewed compact variant, or a suitable native route.
Do not silently truncate or split arbitrary prose. Verify that execution obtains
the complete required procedure. Put essential loading instructions inside the
part that actually reaches the model.

### G. Reviewed LLM-assisted variants

Keep compilation deterministic. Permit explicit generation of a proposed semantic
adaptation when mechanical translation is inadequate. Builds consume accepted
bytes, not a fresh model response.

Track the source subtree and effective metadata, dependencies, target profile,
adaptation instructions, generator/model details, exact output digest, parent
variant, rationale, review, and validation evidence. Preserve generated bytes even
when only a moving model alias is available; a rerun is not reproducible just
because the alias and settings match.

A source change marks the variant stale and creates a new proposal. It must not
overwrite approved content. If hand edits are allowed, update using the previous
generated base, current reviewed file, and new proposal. Conflicts stop promotion;
a clean merge still requires semantic review. A cache hit is not approval.

Keep the deterministic renderer identity distinct where practical: a packaging
fix need not force a semantic rewrite. This workflow can use files and existing
CI; it does not require an adaptation service.

## Interfaces, validation, and rollout

### Proposed interfaces

| Interface | Change |
|---|---|
| Source schema | Versioned common and kind-specific metadata, dependencies, and bindings. |
| Hub/plugin configuration | Available target profiles and explicit plugin subsets. |
| Adapter interface | Inputs, representation, preserved behavior, differences, and validation requirements. |
| Release metadata | Compatibility records and provenance alongside composition and hashes. |
| CLI diagnostics | Asset, target, affected behavior, and concrete remedies. |
| Authoring tools | Present Markdown, sidecar, dependencies, and generated differences together. |

Adaptation freshness must include effective metadata and relevant resources. The
current asset content hash alone is insufficient because it excludes the sidecar.
[Asset loading](../../src/promptless_instruction_hub/assets.py)

These are proposed interface responsibilities, not a claim that the existing
CLI accepts the illustrative syntax above. Formal schemas and adapters follow
team review of this contract.

### Verification levels

1. **Static:** source and target schemas, dependencies, selected bindings, paths,
   collisions, complete rendered sizes, and deterministic output.
2. **Runtime controls:** actual loading, discovery, resource access, native
   restrictions, hook behavior, and unavailable-model handling.
3. **Model-mediated workflows:** observable delegation, full procedure loading,
   clarification relay, and expected results under recorded harness/model/config.

No actual harness installations or runtime qualification were performed for this
RFC. The evidence is documentation, implementation inspection, repository
measurements, and identified existing tests. Preserve those distinctions in
compatibility reports.

### Initial acceptance fixtures

| Fixture | Required observations |
|---|---|
| Ordinary skill | Expected discovery and complete procedure loading. |
| Manual workflow | Correct explicit route without unintended automatic selection. |
| Resource-bearing workflow | References/scripts work after relocation and supported linked installation. |
| Delegated specialist | Child launches, reads the role, returns results, and avoids same-role recursion. |
| Required clarification | Parent relays the question; dependent work waits for the answer. |
| Scoped rule | Matching/nonmatching paths receive documented behavior. |
| Tool policy | Requirements, availability restrictions, and preapproval behave separately as declared. |
| Hook | Matching, units, blocking/filtering, failure behavior, and payload mappings. |
| Model policy | Inheritance and preferred/required unavailable-model behavior. |
| Boundary sizes | Byte/character boundaries, multibyte text, and wrapper overhead. |
| Composition | Missing dependencies and generated-name collisions produce clear errors. |
| Aggregate catalog | Selection/truncation effects are visible when many skills are installed. |

For Codex's capped route include 7,999-, 8,000-, and 8,001-byte files. For Gemini
commands, test the name-segment boundary rather than accepting silent renaming.
Use harmless local tools for control fixtures. Model-mediated tests record
observed outcomes and repeated failures without demanding identical prose.

### Staged rollout

1. Approve the source contract, vocabulary, and representative layout examples.
2. Add source validation and compatibility reporting while preserving current
   delivery until individual migrations are reviewed.
3. Qualify Claude, Codex, Cursor, and Gemini profiles with exact surface, route,
   version, platform, and relevant configuration recorded.
4. Migrate representative assets: PR73 roles, long and resource-heavy skills,
   manual workflow, rule, hook, and MCP configuration.
5. Qualify Copilot, Devin, OpenClaw, and Hermes through the same process.

The existing compiler's four target names are a starting point for qualification,
not proof that every asset kind and runtime surface already works. Keep researched
targets visibly separate from tested support. Use compatibility diffs when
updating profiles, including changes caused solely by package-format migrations.

## Sources

Numbered notes identify the exact source and its evidence baseline. Repository
links within this proposal resolve against the checked-out RFC revision.

[^1]: Agent Plugins maintainers. [Agent Plugins specification](https://agent-plugins.org/specification). Documentation inspected September 13–14, 2026.

[^2]: Agent Skills maintainers. [Agent Skills specification](https://agentskills.io/specification). Documentation inspected September 13–14, 2026.

[^3]: Anthropic. [Claude skills](https://code.claude.com/docs/en/skills). Documentation inspected September 13–14, 2026.

[^4]: Cognition, Devin. [Devin skills](https://docs.devin.ai/cli/extensibility/skills/creating-skills). Documentation inspected September 13–14, 2026.

[^5]: NousResearch/hermes-agent. [hermes_cli/agent_plugins.py](https://github.com/NousResearch/hermes-agent/blob/v2026.9.11/hermes_cli/agent_plugins.py). Hermes 2026.9.11, released September 11, 2026; inspected September 13–14, 2026.

[^6]: Cognition, Devin. [Devin subagents](https://docs.devin.ai/cli/subagents). Documentation inspected September 13–14, 2026.

[^7]: NousResearch/hermes-agent. [tools/skills_tool_plugin.py](https://github.com/NousResearch/hermes-agent/blob/v2026.9.11/tools/skills_tool_plugin.py). Hermes 2026.9.11, released September 11, 2026; inspected September 13–14, 2026.

[^8]: openclaw/openclaw. [src/plugins/bundle-manifest.ts](https://github.com/openclaw/openclaw/blob/v2026.9.4/src/plugins/bundle-manifest.ts). OpenClaw 2026.9.4, released September 11, 2026; inspected September 13–14, 2026.

[^9]: NousResearch/hermes-agent. [hermes_cli/plugins_discovery.py](https://github.com/NousResearch/hermes-agent/blob/v2026.9.11/hermes_cli/plugins_discovery.py). Hermes 2026.9.11, released September 11, 2026; inspected September 13–14, 2026.

[^10]: Promptless/instruction-hub-toolchain. [PR73, pull request #73](https://github.com/Promptless/instruction-hub-toolchain/pull/73). Status and content inspected September 13–14, 2026.

[^11]: Promptless/instruction-hub-toolchain. [src/promptless_instruction_hub/agent_skills.py](https://github.com/Promptless/instruction-hub-toolchain/blob/6d795cd50af79c9710060f215804985b72d8dfd9/src/promptless_instruction_hub/agent_skills.py#L105). revision `6d795cd50af79c9710060f215804985b72d8dfd9`; inspected September 13–14, 2026.

[^12]: Promptless/instruction-hub. [assets/agents/leadgen-experiment-planner.md](https://github.com/Promptless/instruction-hub/blob/42927ff8e359f139ca4db40b1c73b692efc89bd2/assets/agents/leadgen-experiment-planner.md). revision `42927ff8e359f139ca4db40b1c73b692efc89bd2`; inspected September 13–14, 2026.

[^13]: Promptless/instruction-hub. [assets/agents/prospect-docs-analyzer.md](https://github.com/Promptless/instruction-hub/blob/42927ff8e359f139ca4db40b1c73b692efc89bd2/assets/agents/prospect-docs-analyzer.md). revision `42927ff8e359f139ca4db40b1c73b692efc89bd2`; inspected September 13–14, 2026.

[^14]: google-gemini/gemini-cli. [packages/cli/src/services/FileCommandLoader.ts](https://github.com/google-gemini/gemini-cli/blob/v0.59.0/packages/cli/src/services/FileCommandLoader.ts). Gemini CLI 0.59.0, released September 8, 2026; inspected September 13–14, 2026.

[^15]: google-gemini/gemini-cli. [packages/core/src/tools/activate-skill.ts](https://github.com/google-gemini/gemini-cli/blob/v0.59.0/packages/core/src/tools/activate-skill.ts). Gemini CLI 0.59.0, released September 8, 2026; inspected September 13–14, 2026.

[^16]: Cursor. [Cursor rules](https://prod.cursor.com/docs/rules). Documentation inspected September 13–14, 2026.

[^17]: Google, Gemini CLI. [Gemini context](https://geminicli.com/docs/cli/gemini-md/#understand-the-context-hierarchy). Documentation inspected September 13–14, 2026.

[^18]: Google, Gemini CLI. [Gemini hooks](https://geminicli.com/docs/hooks/reference/). Documentation inspected September 13–14, 2026.

[^19]: Anthropic. [Claude hooks](https://code.claude.com/docs/en/hooks). Documentation inspected September 13–14, 2026.

[^20]: Agent Plugins maintainers. [Portable MCP](https://agent-plugins.org/plugin-authors/mcp-servers). Documentation inspected September 13–14, 2026.

[^21]: Promptless/instruction-hub. [Inventory baseline](https://github.com/Promptless/instruction-hub/tree/6c779ebecc0a7f898ddc51dfe15e8d251feeb646). Inspected September 13–14, 2026.

[^22]: openai/codex. [codex-rs/ext/skills/src/host_prompt.rs](https://github.com/openai/codex/blob/6b9826e3aa83b1a5947db50f4332cb9c65f1b340/codex-rs/ext/skills/src/host_prompt.rs#L69). Codex 0.154.0, released September 9, 2026; inspected September 13–14, 2026.

[^23]: openai/codex. [codex-rs/ext/skills/src/render.rs](https://github.com/openai/codex/blob/6b9826e3aa83b1a5947db50f4332cb9c65f1b340/codex-rs/ext/skills/src/render.rs#L1176). Codex 0.154.0, released September 9, 2026; inspected September 13–14, 2026.
