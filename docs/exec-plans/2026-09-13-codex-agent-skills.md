# Compile agent definitions into Codex skills

## Goal

Make authored agent roles available through Codex plugin skill discovery while
preserving their native Claude distribution. Enable the leadgen experiment
planner and prospect docs analyzer in Promptless's Instruction Hub.

## Design decisions

- Conversion is opt-in through `support.codex.mode: agent-skill` and supports
  Markdown agent files. Other targets and directory-based agent conversion are
  outside this change.
- Each generated skill contains a delegation preamble and the complete source
  body. The parent launches one specialist, relays clarification questions and
  answers, and returns the result. An assigned specialist runs its role directly.
  Without subagent tools, the workflow stops.
- Use the asset ID and a shared description for discovery. Validate the
  description against the 1,024-character limit without truncating it.
- Preserve source tool allowlists and denylists as advisory instructions and
  report the enforcement limitation through structured warnings and CLI stderr.
  Reject unsupported behavioral metadata. Omit Claude model and color settings;
  launch the Codex child without model or reasoning overrides.
- Preserve `agent:<id>` release provenance and reject generated skill collisions.
  Do not modify users' Codex configuration or install custom agent roles.

## Implementation

1. Add source parsing, generated delegation instructions, validation, collision
   checks, and conversion warnings to the compiler.
2. Document the source and output contracts in the README.
3. In `Promptless/instruction-hub`, enable conversion for both GTM agents, shorten
   their shared descriptions, move useful examples into their bodies, and remove
   claims that depend on Claude-only enforcement. Update the authoring skill.
4. Open coordinated PRs. Land the compiler before the Hub source changes because
   Hub CI consumes toolchain `main`. Use the existing release workflow and
   automatic patch versioning; commit source changes only.

## Verification

- Cover malformed and unsupported metadata, description limits, tool-list
  parsing, file eligibility, output collisions, and structured warnings.
- Verify Codex skill discovery, complete body preservation, native Claude
  output, release provenance, repeatable hashes, and CLI stderr output.
- Run isolated synthetic checks for delegation, recursion prevention,
  clarification relay, and unavailable subagent tools without external writes.
- Run the full toolchain tests, Ruff checks, formatting checks, and `ty`; verify
  the updated Hub with the changed compiler. Complete code, test, type, failure,
  comment, simplification, plain-language, and AI-tells reviews before publishing
  the PRs.
