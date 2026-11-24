# Enforce built-in read-only paths before file-tool acquisition

## Context

Skills and ACP workspaces are read-only agent inputs. The local file-tool
validator enforces that policy, but remote file-tool calls can reach a provider
without that check. This is a core tool-boundary bug; an optional extension
cannot make the built-in tools enforce the same policy for every provider.

## Decision

Reject `write_file` (including append) and `str_replace` targeting the configured
skills container path or `/mnt/acp-workspace` before acquiring a sandbox. Compare
normalized absolute POSIX virtual paths with complete path-component boundaries.
Keep provider path mapping unchanged. Run config-dependent validation off the
event loop in async entry points, before the common async acquisition wrapper.
The default read-before-write middleware skips content inspection for these
mutations and delegates to the tool's denial, so its version gate does not
acquire a sandbox first. Authorization and normal tool result handling remain
in place.

## Consequences

Remote and local file tools consistently reject these mutations without
initializing a sandbox. Writable user-data paths and similarly named sibling
paths keep their existing behavior. Provider filesystem enforcement is still
required for shell commands, symlinks and other aliases; this change does not
claim those boundaries are hardened. Arbitrary custom read-only mounts remain
subject to their existing provider and local-tool policy.
