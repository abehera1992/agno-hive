---
name: external-framework-verification
description: How to cite external docs safely (verify against real project file:line, label unverified claims) and how to check a difference from an external framework's default is intentional design before flagging it as a misconfiguration.
---
VERIFY-EXTERNAL rule: For any external tool, Docker image, library, or
third-party service you reference that is NOT found in the project files —
call web_search() to verify the current name, image tag, version, and
config before including it in your findings. Training data may be stale.
Example: if recommending a Docker image name, web_search it first and use
the result from the official docs or GitHub README. Label any unverified
external claim as "unverified — from training data".

EVIDENCE rule: Any recommendation based on external documentation MUST cite
(a) the specific doc URL and section, AND (b) the specific project file:line
that was compared. If you cannot cite a project file, label the claim as
"inference from docs — not verified in codebase" rather than presenting it
as a confirmed requirement.

DESIGN-INTENT rule: Before flagging a difference between this project and an
external framework as a misconfiguration, read CLAUDE.md and docs.md (via
get_file_content) to check if the difference is intentional design. Many
patterns in this project deliberately differ from framework defaults.
