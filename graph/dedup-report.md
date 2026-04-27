# Dedup Report

- **Model**: `sentence-transformers:sentence-transformers/all-MiniLM-L6-v2`
- **Threshold**: cosine ≥ 0.85
- **Entities indexed**: 13,218
- **Pairs evaluated**: 87,351,153
- **Findings (above threshold, not allowlisted)**: 15,976
- **Allowlisted pairs**: 0
- **Showing**: top 100 by similarity. Full set lives in the JSON sidecar (`dedup-report.json`).
- **By similarity bucket**: ≥0.99 → 12, 0.95-0.99 → 94, 0.90-0.95 → 2,094, 0.85-0.90 → 13,776
- **By type pair**: mcp-server ↔ mcp-server → 15,159, skill ↔ skill → 556, agent ↔ skill → 154, agent ↔ agent → 107

## Findings (review required)

Each pair below has cosine similarity at or above the threshold. **Do not auto-drop.** The intent is human review: either confirm both entries are legitimately distinct (and add to `.dedup-allowlist.txt`), or merge/remove one with a PR that explains why.

### code-refactoring-tech-debt  ↔  codebase-cleanup-tech-debt  (0.999)

- **Types**: skill ↔ skill
- **Shared tags**: (none)
- **code-refactoring-tech-debt unique tags**: (none)
- **codebase-cleanup-tech-debt unique tags**: (none)
- **code-refactoring-tech-debt** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\code-refactoring-tech-debt.md`
  - desc: (none)
- **codebase-cleanup-tech-debt** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\codebase-cleanup-tech-debt.md`
  - desc: (none)

### code-review-ai-ai-review  ↔  performance-testing-review-ai-review  (0.998)

- **Types**: skill ↔ skill
- **Shared tags**: (none)
- **code-review-ai-ai-review unique tags**: (none)
- **performance-testing-review-ai-review unique tags**: (none)
- **code-review-ai-ai-review** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\code-review-ai-ai-review.md`
  - desc: (none)
- **performance-testing-review-ai-review** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\performance-testing-review-ai-review.md`
  - desc: (none)

### chrome-mcp-troubleshooting  ↔  claude-in-chrome-troubleshooting  (0.998)

- **Types**: skill ↔ skill
- **Shared tags**: (none)
- **chrome-mcp-troubleshooting unique tags**: (none)
- **claude-in-chrome-troubleshooting unique tags**: (none)
- **chrome-mcp-troubleshooting** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\chrome-mcp-troubleshooting.md`
  - desc: (none)
- **claude-in-chrome-troubleshooting** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\claude-in-chrome-troubleshooting.md`
  - desc: (none)

### code-refactoring-context-restore  ↔  context-management-context-restore  (0.998)

- **Types**: skill ↔ skill
- **Shared tags**: (none)
- **code-refactoring-context-restore unique tags**: (none)
- **context-management-context-restore unique tags**: (none)
- **code-refactoring-context-restore** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\code-refactoring-context-restore.md`
  - desc: (none)
- **context-management-context-restore** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\context-management-context-restore.md`
  - desc: (none)

### debugging-toolkit-smart-debug  ↔  error-diagnostics-smart-debug  (0.997)

- **Types**: skill ↔ skill
- **Shared tags**: (none)
- **debugging-toolkit-smart-debug unique tags**: (none)
- **error-diagnostics-smart-debug unique tags**: (none)
- **debugging-toolkit-smart-debug** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\debugging-toolkit-smart-debug.md`
  - desc: (none)
- **error-diagnostics-smart-debug** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\error-diagnostics-smart-debug.md`
  - desc: (none)

### agents-v2-py  ↔  hosted-agents-v2-py  (0.997)

- **Types**: skill ↔ skill
- **Shared tags**: (none)
- **agents-v2-py unique tags**: (none)
- **hosted-agents-v2-py unique tags**: (none)
- **agents-v2-py** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\agents-v2-py.md`
  - desc: (none)
- **hosted-agents-v2-py** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\hosted-agents-v2-py.md`
  - desc: (none)

### code-documentation-doc-generate  ↔  documentation-generation-doc-generate  (0.995)

- **Types**: skill ↔ skill
- **Shared tags**: (none)
- **code-documentation-doc-generate unique tags**: (none)
- **documentation-generation-doc-generate unique tags**: (none)
- **code-documentation-doc-generate** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\code-documentation-doc-generate.md`
  - desc: (none)
- **documentation-generation-doc-generate** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\documentation-generation-doc-generate.md`
  - desc: (none)

### claude-code-guide  ↔  code-guide-cc  (0.994)

- **Types**: skill ↔ skill
- **Shared tags**: (none)
- **claude-code-guide unique tags**: (none)
- **code-guide-cc unique tags**: (none)
- **claude-code-guide** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\claude-code-guide.md`
  - desc: (none)
- **code-guide-cc** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\code-guide-cc.md`
  - desc: (none)

### error-debugging-multi-agent-review  ↔  performance-testing-review-multi-agent-review  (0.994)

- **Types**: skill ↔ skill
- **Shared tags**: (none)
- **error-debugging-multi-agent-review unique tags**: (none)
- **performance-testing-review-multi-agent-review unique tags**: (none)
- **error-debugging-multi-agent-review** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\error-debugging-multi-agent-review.md`
  - desc: (none)
- **performance-testing-review-multi-agent-review** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\performance-testing-review-multi-agent-review.md`
  - desc: (none)

### azure-keyvault-keys-ts  ↔  azure-keyvault-secrets-ts  (0.993)

- **Types**: skill ↔ skill
- **Shared tags**: (none)
- **azure-keyvault-keys-ts unique tags**: (none)
- **azure-keyvault-secrets-ts unique tags**: (none)
- **azure-keyvault-keys-ts** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\azure-keyvault-keys-ts.md`
  - desc: (none)
- **azure-keyvault-secrets-ts** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\azure-keyvault-secrets-ts.md`
  - desc: (none)

### error-debugging-error-analysis  ↔  error-diagnostics-error-analysis  (0.993)

- **Types**: skill ↔ skill
- **Shared tags**: (none)
- **error-debugging-error-analysis unique tags**: (none)
- **error-diagnostics-error-analysis unique tags**: (none)
- **error-debugging-error-analysis** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\error-debugging-error-analysis.md`
  - desc: (none)
- **error-diagnostics-error-analysis** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\error-diagnostics-error-analysis.md`
  - desc: (none)

### fp-errors  ↔  fp-ts-errors  (0.991)

- **Types**: skill ↔ skill
- **Shared tags**: (none)
- **fp-errors unique tags**: (none)
- **fp-ts-errors unique tags**: (none)
- **fp-errors** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\fp-errors.md`
  - desc: (none)
- **fp-ts-errors** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\fp-ts-errors.md`
  - desc: (none)

### fp-react  ↔  fp-ts-react  (0.990)

- **Types**: skill ↔ skill
- **Shared tags**: (none)
- **fp-react unique tags**: (none)
- **fp-ts-react unique tags**: (none)
- **fp-react** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\fp-react.md`
  - desc: (none)
- **fp-ts-react** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\fp-ts-react.md`
  - desc: (none)

### frontend-security-coder  ↔  frontend-security-coder  (0.990)

- **Types**: agent ↔ skill
- **Shared tags**: (none)
- **frontend-security-coder unique tags**: (none)
- **frontend-security-coder unique tags**: (none)
- **frontend-security-coder** path: `C:\Users\solun\.claude\skill-wiki\entities\agents\frontend-security-coder.md`
  - desc: (none)
- **frontend-security-coder** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\frontend-security-coder.md`
  - desc: (none)

### kubernetes-architect  ↔  kubernetes-architect  (0.989)

- **Types**: agent ↔ skill
- **Shared tags**: (none)
- **kubernetes-architect unique tags**: (none)
- **kubernetes-architect unique tags**: (none)
- **kubernetes-architect** path: `C:\Users\solun\.claude\skill-wiki\entities\agents\kubernetes-architect.md`
  - desc: (none)
- **kubernetes-architect** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\kubernetes-architect.md`
  - desc: (none)

### claude-d3js-skill  ↔  d3js-skill  (0.989)

- **Types**: skill ↔ skill
- **Shared tags**: (none)
- **claude-d3js-skill unique tags**: (none)
- **d3js-skill unique tags**: (none)
- **claude-d3js-skill** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\claude-d3js-skill.md`
  - desc: (none)
- **d3js-skill** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\d3js-skill.md`
  - desc: (none)

### fp-pragmatic  ↔  fp-ts-pragmatic  (0.989)

- **Types**: skill ↔ skill
- **Shared tags**: (none)
- **fp-pragmatic unique tags**: (none)
- **fp-ts-pragmatic unique tags**: (none)
- **fp-pragmatic** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\fp-pragmatic.md`
  - desc: (none)
- **fp-ts-pragmatic** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\fp-ts-pragmatic.md`
  - desc: (none)

### hybrid-cloud-architect  ↔  hybrid-cloud-architect  (0.988)

- **Types**: agent ↔ skill
- **Shared tags**: (none)
- **hybrid-cloud-architect unique tags**: (none)
- **hybrid-cloud-architect unique tags**: (none)
- **hybrid-cloud-architect** path: `C:\Users\solun\.claude\skill-wiki\entities\agents\hybrid-cloud-architect.md`
  - desc: (none)
- **hybrid-cloud-architect** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\hybrid-cloud-architect.md`
  - desc: (none)

### cc-skill-coding-standards  ↔  coding-standards  (0.988)

- **Types**: skill ↔ skill
- **Shared tags**: (none)
- **cc-skill-coding-standards unique tags**: (none)
- **coding-standards unique tags**: (none)
- **cc-skill-coding-standards** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\cc-skill-coding-standards.md`
  - desc: (none)
- **coding-standards** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\coding-standards.md`
  - desc: (none)

### artifacts-builder  ↔  web-artifacts-builder  (0.988)

- **Types**: skill ↔ skill
- **Shared tags**: (none)
- **artifacts-builder unique tags**: (none)
- **web-artifacts-builder unique tags**: (none)
- **artifacts-builder** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\artifacts-builder.md`
  - desc: (none)
- **web-artifacts-builder** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\web-artifacts-builder.md`
  - desc: (none)

### brand-guidelines-anthropic  ↔  brand-guidelines-community  (0.988)

- **Types**: skill ↔ skill
- **Shared tags**: (none)
- **brand-guidelines-anthropic unique tags**: (none)
- **brand-guidelines-community unique tags**: (none)
- **brand-guidelines-anthropic** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\brand-guidelines-anthropic.md`
  - desc: (none)
- **brand-guidelines-community** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\brand-guidelines-community.md`
  - desc: (none)

### backend-security-coder  ↔  backend-security-coder  (0.987)

- **Types**: agent ↔ skill
- **Shared tags**: (none)
- **backend-security-coder unique tags**: (none)
- **backend-security-coder unique tags**: (none)
- **backend-security-coder** path: `C:\Users\solun\.claude\skill-wiki\entities\agents\backend-security-coder.md`
  - desc: (none)
- **backend-security-coder** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\backend-security-coder.md`
  - desc: (none)

### terraform-specialist  ↔  terraform-specialist  (0.987)

- **Types**: agent ↔ skill
- **Shared tags**: (none)
- **terraform-specialist unique tags**: (none)
- **terraform-specialist unique tags**: (none)
- **terraform-specialist** path: `C:\Users\solun\.claude\skill-wiki\entities\agents\terraform-specialist.md`
  - desc: (none)
- **terraform-specialist** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\terraform-specialist.md`
  - desc: (none)

### claude-code-expert  ↔  code-expert-assistant  (0.987)

- **Types**: skill ↔ skill
- **Shared tags**: (none)
- **claude-code-expert unique tags**: (none)
- **code-expert-assistant unique tags**: (none)
- **claude-code-expert** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\claude-code-expert.md`
  - desc: (none)
- **code-expert-assistant** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\code-expert-assistant.md`
  - desc: (none)

### database-admin  ↔  database-admin  (0.987)

- **Types**: agent ↔ skill
- **Shared tags**: (none)
- **database-admin unique tags**: (none)
- **database-admin unique tags**: (none)
- **database-admin** path: `C:\Users\solun\.claude\skill-wiki\entities\agents\database-admin.md`
  - desc: (none)
- **database-admin** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\database-admin.md`
  - desc: (none)

### cc-skill-project-guidelines-example  ↔  project-guidelines-example  (0.986)

- **Types**: skill ↔ skill
- **Shared tags**: (none)
- **cc-skill-project-guidelines-example unique tags**: (none)
- **project-guidelines-example unique tags**: (none)
- **cc-skill-project-guidelines-example** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\cc-skill-project-guidelines-example.md`
  - desc: (none)
- **project-guidelines-example** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\project-guidelines-example.md`
  - desc: (none)

### cc-skill-security-review  ↔  security-review  (0.986)

- **Types**: skill ↔ skill
- **Shared tags**: (none)
- **cc-skill-security-review unique tags**: (none)
- **security-review unique tags**: (none)
- **cc-skill-security-review** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\cc-skill-security-review.md`
  - desc: (none)
- **security-review** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\security-review.md`
  - desc: (none)

### cc-skill-frontend-patterns  ↔  frontend-patterns  (0.985)

- **Types**: skill ↔ skill
- **Shared tags**: (none)
- **cc-skill-frontend-patterns unique tags**: (none)
- **frontend-patterns unique tags**: (none)
- **cc-skill-frontend-patterns** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\cc-skill-frontend-patterns.md`
  - desc: (none)
- **frontend-patterns** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\frontend-patterns.md`
  - desc: (none)

### customer-support  ↔  customer-support  (0.984)

- **Types**: agent ↔ skill
- **Shared tags**: (none)
- **customer-support unique tags**: (none)
- **customer-support unique tags**: (none)
- **customer-support** path: `C:\Users\solun\.claude\skill-wiki\entities\agents\customer-support.md`
  - desc: (none)
- **customer-support** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\customer-support.md`
  - desc: (none)

### backend-patterns  ↔  cc-skill-backend-patterns  (0.984)

- **Types**: skill ↔ skill
- **Shared tags**: (none)
- **backend-patterns unique tags**: (none)
- **cc-skill-backend-patterns unique tags**: (none)
- **backend-patterns** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\backend-patterns.md`
  - desc: (none)
- **cc-skill-backend-patterns** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\cc-skill-backend-patterns.md`
  - desc: (none)

### devops-troubleshooter  ↔  devops-troubleshooter  (0.983)

- **Types**: agent ↔ skill
- **Shared tags**: (none)
- **devops-troubleshooter unique tags**: (none)
- **devops-troubleshooter unique tags**: (none)
- **devops-troubleshooter** path: `C:\Users\solun\.claude\skill-wiki\entities\agents\devops-troubleshooter.md`
  - desc: (none)
- **devops-troubleshooter** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\devops-troubleshooter.md`
  - desc: (none)

### claude-monitor  ↔  monitor-assistant  (0.983)

- **Types**: skill ↔ skill
- **Shared tags**: (none)
- **claude-monitor unique tags**: (none)
- **monitor-assistant unique tags**: (none)
- **claude-monitor** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\claude-monitor.md`
  - desc: (none)
- **monitor-assistant** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\monitor-assistant.md`
  - desc: (none)

### django-pro  ↔  django-pro  (0.982)

- **Types**: agent ↔ skill
- **Shared tags**: (none)
- **django-pro unique tags**: (none)
- **django-pro unique tags**: (none)
- **django-pro** path: `C:\Users\solun\.claude\skill-wiki\entities\agents\django-pro.md`
  - desc: (none)
- **django-pro** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\django-pro.md`
  - desc: (none)

### caveman-compress  ↔  compress  (0.980)

- **Types**: skill ↔ skill
- **Shared tags**: (none)
- **caveman-compress unique tags**: (none)
- **compress unique tags**: (none)
- **caveman-compress** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\caveman-compress.md`
  - desc: (none)
- **compress** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\compress.md`
  - desc: (none)

### java-pro  ↔  java-pro  (0.980)

- **Types**: agent ↔ skill
- **Shared tags**: (none)
- **java-pro unique tags**: (none)
- **java-pro unique tags**: (none)
- **java-pro** path: `C:\Users\solun\.claude\skill-wiki\entities\agents\java-pro.md`
  - desc: (none)
- **java-pro** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\java-pro.md`
  - desc: (none)

### remotion-best-practices  ↔  remotion-video-creation  (0.977)

- **Types**: skill ↔ skill
- **Shared tags**: (none)
- **remotion-best-practices unique tags**: (none)
- **remotion-video-creation unique tags**: (none)
- **remotion-best-practices** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\remotion-best-practices.md`
  - desc: (none)
- **remotion-video-creation** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\remotion-video-creation.md`
  - desc: (none)

### hr-pro  ↔  hr-pro  (0.977)

- **Types**: agent ↔ skill
- **Shared tags**: (none)
- **hr-pro unique tags**: (none)
- **hr-pro unique tags**: (none)
- **hr-pro** path: `C:\Users\solun\.claude\skill-wiki\entities\agents\hr-pro.md`
  - desc: (none)
- **hr-pro** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\hr-pro.md`
  - desc: (none)

### observability-engineer  ↔  observability-engineer  (0.977)

- **Types**: agent ↔ skill
- **Shared tags**: (none)
- **observability-engineer unique tags**: (none)
- **observability-engineer unique tags**: (none)
- **observability-engineer** path: `C:\Users\solun\.claude\skill-wiki\entities\agents\observability-engineer.md`
  - desc: (none)
- **observability-engineer** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\observability-engineer.md`
  - desc: (none)

### deepseek-r1-reasoner  ↔  deepseek-reasoner  (0.977)

- **Types**: mcp-server ↔ mcp-server
- **Shared tags**: (none)
- **deepseek-r1-reasoner unique tags**: (none)
- **deepseek-reasoner unique tags**: (none)
- **deepseek-r1-reasoner** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\d\deepseek-r1-reasoner.md`
  - desc: Leverages Deepseek r1 for local reasoning and task planning, enabling
- **deepseek-reasoner** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\d\deepseek-reasoner.md`
  - desc: Integrates DeepSeek's R1 reasoning engine to enhance problem-solving,

### backend-architect  ↔  backend-architect  (0.976)

- **Types**: agent ↔ skill
- **Shared tags**: (none)
- **backend-architect unique tags**: (none)
- **backend-architect unique tags**: (none)
- **backend-architect** path: `C:\Users\solun\.claude\skill-wiki\entities\agents\backend-architect.md`
  - desc: (none)
- **backend-architect** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\backend-architect.md`
  - desc: (none)

### flux-schnell  ↔  flux-schnell-replicate  (0.975)

- **Types**: mcp-server ↔ mcp-server
- **Shared tags**: (none)
- **flux-schnell unique tags**: (none)
- **flux-schnell-replicate unique tags**: (none)
- **flux-schnell** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\f\flux-schnell.md`
  - desc: Integrates with the Flux Schnell model on Replicate to generate images
- **flux-schnell-replicate** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\f\flux-schnell-replicate.md`
  - desc: Bridges Claude with Replicate's flux-schnell image generation model,

### claude-settings-audit  ↔  settings-audit  (0.975)

- **Types**: skill ↔ skill
- **Shared tags**: (none)
- **claude-settings-audit unique tags**: (none)
- **settings-audit unique tags**: (none)
- **claude-settings-audit** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\claude-settings-audit.md`
  - desc: (none)
- **settings-audit** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\settings-audit.md`
  - desc: (none)

### rust-pro  ↔  rust-pro  (0.975)

- **Types**: agent ↔ skill
- **Shared tags**: (none)
- **rust-pro unique tags**: (none)
- **rust-pro unique tags**: (none)
- **rust-pro** path: `C:\Users\solun\.claude\skill-wiki\entities\agents\rust-pro.md`
  - desc: (none)
- **rust-pro** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\rust-pro.md`
  - desc: (none)

### docs-architect  ↔  docs-architect  (0.974)

- **Types**: agent ↔ skill
- **Shared tags**: (none)
- **docs-architect unique tags**: (none)
- **docs-architect unique tags**: (none)
- **docs-architect** path: `C:\Users\solun\.claude\skill-wiki\entities\agents\docs-architect.md`
  - desc: (none)
- **docs-architect** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\docs-architect.md`
  - desc: (none)

### architect-review  ↔  architect-review  (0.973)

- **Types**: agent ↔ skill
- **Shared tags**: (none)
- **architect-review unique tags**: (none)
- **architect-review unique tags**: (none)
- **architect-review** path: `C:\Users\solun\.claude\skill-wiki\entities\agents\architect-review.md`
  - desc: (none)
- **architect-review** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\architect-review.md`
  - desc: (none)

### node-code-sandbox  ↔  node-js-code-sandbox  (0.973)

- **Types**: mcp-server ↔ mcp-server
- **Shared tags**: (none)
- **node-code-sandbox unique tags**: (none)
- **node-js-code-sandbox unique tags**: (none)
- **node-code-sandbox** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\n\node-code-sandbox.md`
  - desc: Provides a secure Docker-based sandbox for executing JavaScript code
- **node-js-code-sandbox** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\n\node-js-code-sandbox.md`
  - desc: Provides a secure Docker-based environment for executing Node.js code

### android-ui-verification  ↔  android_ui_verification  (0.973)

- **Types**: skill ↔ skill
- **Shared tags**: (none)
- **android-ui-verification unique tags**: (none)
- **android_ui_verification unique tags**: (none)
- **android-ui-verification** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\android-ui-verification.md`
  - desc: (none)
- **android_ui_verification** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\android_ui_verification.md`
  - desc: (none)

### database-architect  ↔  database-architect  (0.973)

- **Types**: agent ↔ skill
- **Shared tags**: (none)
- **database-architect unique tags**: (none)
- **database-architect unique tags**: (none)
- **database-architect** path: `C:\Users\solun\.claude\skill-wiki\entities\agents\database-architect.md`
  - desc: (none)
- **database-architect** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\database-architect.md`
  - desc: (none)

### minecraft-bukkit-pro  ↔  minecraft-bukkit-pro  (0.972)

- **Types**: agent ↔ skill
- **Shared tags**: (none)
- **minecraft-bukkit-pro unique tags**: (none)
- **minecraft-bukkit-pro unique tags**: (none)
- **minecraft-bukkit-pro** path: `C:\Users\solun\.claude\skill-wiki\entities\agents\minecraft-bukkit-pro.md`
  - desc: (none)
- **minecraft-bukkit-pro** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\minecraft-bukkit-pro.md`
  - desc: (none)

### scala-pro  ↔  scala-pro  (0.969)

- **Types**: agent ↔ skill
- **Shared tags**: (none)
- **scala-pro unique tags**: (none)
- **scala-pro unique tags**: (none)
- **scala-pro** path: `C:\Users\solun\.claude\skill-wiki\entities\agents\scala-pro.md`
  - desc: (none)
- **scala-pro** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\scala-pro.md`
  - desc: (none)

### openweather  ↔  openweathermap  (0.968)

- **Types**: mcp-server ↔ mcp-server
- **Shared tags**: (none)
- **openweather unique tags**: (none)
- **openweathermap unique tags**: (none)
- **openweather** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\o\openweather.md`
  - desc: Fetches current weather and forecasts from the OpenWeatherMap API via
- **openweathermap** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\o\openweathermap.md`
  - desc: Integrates with OpenWeatherMap API to provide current conditions, forecasts,

### seo-authority-builder  ↔  seo-authority-builder  (0.966)

- **Types**: agent ↔ skill
- **Shared tags**: (none)
- **seo-authority-builder unique tags**: (none)
- **seo-authority-builder unique tags**: (none)
- **seo-authority-builder** path: `C:\Users\solun\.claude\skill-wiki\entities\agents\seo-authority-builder.md`
  - desc: (none)
- **seo-authority-builder** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\seo-authority-builder.md`
  - desc: (none)

### browser-automation-playwright  ↔  playmcp-playwright-browser-automation  (0.965)

- **Types**: mcp-server ↔ mcp-server
- **Shared tags**: (none)
- **browser-automation-playwright unique tags**: (none)
- **playmcp-playwright-browser-automation unique tags**: (none)
- **browser-automation-playwright** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\b\browser-automation-playwright.md`
  - desc: Universal browser automation server using Playwright that provides web
- **playmcp-playwright-browser-automation** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\p\playmcp-playwright-browser-automation.md`
  - desc: Provides browser automation capabilities using Playwright, enabling web

### rag-documentation-search  ↔  ragdocs-vector-documentation-search  (0.964)

- **Types**: mcp-server ↔ mcp-server
- **Shared tags**: (none)
- **rag-documentation-search unique tags**: (none)
- **ragdocs-vector-documentation-search unique tags**: (none)
- **rag-documentation-search** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\r\rag-documentation-search.md`
  - desc: Provides semantic document search and retrieval through vector embeddings,
- **ragdocs-vector-documentation-search** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\r\ragdocs-vector-documentation-search.md`
  - desc: Enables semantic documentation search and retrieval using vector databases,

### adweave-meta-ads  ↔  meta-ads-complete  (0.963)

- **Types**: mcp-server ↔ mcp-server
- **Shared tags**: (none)
- **adweave-meta-ads unique tags**: (none)
- **meta-ads-complete unique tags**: (none)
- **adweave-meta-ads** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\a\adweave-meta-ads.md`
  - desc: Meta Ads management platform with 47 tools for campaigns, creatives,
- **meta-ads-complete** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\m\meta-ads-complete.md`
  - desc: Production-grade Meta Ads Manager integration with 20+ tools for campaign,

### c4-container  ↔  c4-container  (0.962)

- **Types**: agent ↔ skill
- **Shared tags**: (none)
- **c4-container unique tags**: (none)
- **c4-container unique tags**: (none)
- **c4-container** path: `C:\Users\solun\.claude\skill-wiki\entities\agents\c4-container.md`
  - desc: (none)
- **c4-container** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\c4-container.md`
  - desc: (none)

### gitlab  ↔  gitlab-ci  (0.962)

- **Types**: mcp-server ↔ mcp-server
- **Shared tags**: (none)
- **gitlab unique tags**: (none)
- **gitlab-ci unique tags**: (none)
- **gitlab** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\g\gitlab.md`
  - desc: Integrates with GitLab's API to enable repository management, issue tracking,
- **gitlab-ci** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\g\gitlab-ci.md`
  - desc: Manage GitLab CI/CD pipelines, schedules, merge requests, and repository

### youtube-transcript-extractor  ↔  youtube-ultimate-toolkit  (0.961)

- **Types**: mcp-server ↔ mcp-server
- **Shared tags**: (none)
- **youtube-transcript-extractor unique tags**: (none)
- **youtube-ultimate-toolkit unique tags**: (none)
- **youtube-transcript-extractor** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\y\youtube-transcript-extractor.md`
  - desc: Extracts YouTube video transcripts from various URL formats using a command-line
- **youtube-ultimate-toolkit** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\y\youtube-ultimate-toolkit.md`
  - desc: Extracts YouTube video transcripts with timestamps, metadata, comments,

### graphql-architect  ↔  graphql-architect  (0.960)

- **Types**: agent ↔ skill
- **Shared tags**: (none)
- **graphql-architect unique tags**: (none)
- **graphql-architect unique tags**: (none)
- **graphql-architect** path: `C:\Users\solun\.claude\skill-wiki\entities\agents\graphql-architect.md`
  - desc: (none)
- **graphql-architect** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\graphql-architect.md`
  - desc: (none)

### leiloeiro-edital  ↔  leiloeiro-risco  (0.960)

- **Types**: skill ↔ skill
- **Shared tags**: (none)
- **leiloeiro-edital unique tags**: (none)
- **leiloeiro-risco unique tags**: (none)
- **leiloeiro-edital** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\leiloeiro-edital.md`
  - desc: (none)
- **leiloeiro-risco** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\leiloeiro-risco.md`
  - desc: (none)

### elixir-pro  ↔  elixir-pro  (0.960)

- **Types**: agent ↔ skill
- **Shared tags**: (none)
- **elixir-pro unique tags**: (none)
- **elixir-pro unique tags**: (none)
- **elixir-pro** path: `C:\Users\solun\.claude\skill-wiki\entities\agents\elixir-pro.md`
  - desc: (none)
- **elixir-pro** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\elixir-pro.md`
  - desc: (none)

### markdown-to-pdf-converter  ↔  pdf-to-markdown-converter  (0.959)

- **Types**: mcp-server ↔ mcp-server
- **Shared tags**: (none)
- **markdown-to-pdf-converter unique tags**: (none)
- **pdf-to-markdown-converter unique tags**: (none)
- **markdown-to-pdf-converter** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\m\markdown-to-pdf-converter.md`
  - desc: Converts Markdown to styled PDFs, enabling creation of visually appealing
- **pdf-to-markdown-converter** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\p\pdf-to-markdown-converter.md`
  - desc: Converts PDF documents to Markdown format while preserving document structure,

### things  ↔  things3  (0.959)

- **Types**: mcp-server ↔ mcp-server
- **Shared tags**: (none)
- **things unique tags**: (none)
- **things3 unique tags**: (none)
- **things** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\t\things.md`
  - desc: Integrates with Things.app task management for macOS, enabling task and
- **things3** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\t\things3.md`
  - desc: Integrates with Things3 task management app on macOS, enabling creation

### edinet-financial-disclosures  ↔  edinet-financial-disclosures-morinosei  (0.959)

- **Types**: mcp-server ↔ mcp-server
- **Shared tags**: (none)
- **edinet-financial-disclosures unique tags**: (none)
- **edinet-financial-disclosures-morinosei unique tags**: (none)
- **edinet-financial-disclosures** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\e\edinet-financial-disclosures.md`
  - desc: Access Japanese financial disclosures from EDINET with company search
- **edinet-financial-disclosures-morinosei** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\e\edinet-financial-disclosures-morinosei.md`
  - desc: Retrieves Japanese corporate financial disclosure documents from the

### azure-data-explorer-kusto  ↔  kusto-azure-data-explorer  (0.959)

- **Types**: mcp-server ↔ mcp-server
- **Shared tags**: (none)
- **azure-data-explorer-kusto unique tags**: (none)
- **kusto-azure-data-explorer unique tags**: (none)
- **azure-data-explorer-kusto** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\a\azure-data-explorer-kusto.md`
  - desc: Integrates with Azure Data Explorer to enable listing databases, retrieving
- **kusto-azure-data-explorer** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\k\kusto-azure-data-explorer.md`
  - desc: Integrates with Azure Data Explorer to enable read-only querying, table

### youtube-transcript  ↔  youtube-translate  (0.959)

- **Types**: mcp-server ↔ mcp-server
- **Shared tags**: (none)
- **youtube-transcript unique tags**: (none)
- **youtube-translate unique tags**: (none)
- **youtube-transcript** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\y\youtube-transcript.md`
  - desc: Extracts and formats YouTube video transcripts with language selection,
- **youtube-translate** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\y\youtube-translate.md`
  - desc: Enables access to YouTube video transcripts, translations, summaries,

### youtube-summarize  ↔  youtube-transcript  (0.959)

- **Types**: mcp-server ↔ mcp-server
- **Shared tags**: (none)
- **youtube-summarize unique tags**: (none)
- **youtube-transcript unique tags**: (none)
- **youtube-summarize** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\y\youtube-summarize.md`
  - desc: Fetch YouTube video transcripts and optionally summarize them.
- **youtube-transcript** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\y\youtube-transcript.md`
  - desc: Extracts and formats YouTube video transcripts with language selection,

### acedatacloud-seedance  ↔  acedatacloud-seedream  (0.958)

- **Types**: mcp-server ↔ mcp-server
- **Shared tags**: (none)
- **acedatacloud-seedance unique tags**: (none)
- **acedatacloud-seedream unique tags**: (none)
- **acedatacloud-seedance** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\a\acedatacloud-seedance.md`
  - desc: ByteDance Seedance AI video generation through the AceDataCloud API platform.
- **acedatacloud-seedream** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\a\acedatacloud-seedream.md`
  - desc: ByteDance Seedream AI image generation through the AceDataCloud API platform.

### seo-cannibalization-detector  ↔  seo-cannibalization-detector  (0.958)

- **Types**: agent ↔ skill
- **Shared tags**: (none)
- **seo-cannibalization-detector unique tags**: (none)
- **seo-cannibalization-detector unique tags**: (none)
- **seo-cannibalization-detector** path: `C:\Users\solun\.claude\skill-wiki\entities\agents\seo-cannibalization-detector.md`
  - desc: (none)
- **seo-cannibalization-detector** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\seo-cannibalization-detector.md`
  - desc: (none)

### internal-comms-anthropic  ↔  internal-comms-community  (0.958)

- **Types**: skill ↔ skill
- **Shared tags**: (none)
- **internal-comms-anthropic unique tags**: (none)
- **internal-comms-community unique tags**: (none)
- **internal-comms-anthropic** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\internal-comms-anthropic.md`
  - desc: (none)
- **internal-comms-community** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\internal-comms-community.md`
  - desc: (none)

### aws-bedrock-knowledge-base  ↔  aws-bedrock-knowledge-base-retrieval  (0.957)

- **Types**: mcp-server ↔ mcp-server
- **Shared tags**: (none)
- **aws-bedrock-knowledge-base unique tags**: (none)
- **aws-bedrock-knowledge-base-retrieval unique tags**: (none)
- **aws-bedrock-knowledge-base** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\a\aws-bedrock-knowledge-base.md`
  - desc: Query and retrieve information from AWS knowledge bases using the Bedrock
- **aws-bedrock-knowledge-base-retrieval** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\a\aws-bedrock-knowledge-base-retrieval.md`
  - desc: Bridge to access Amazon Bedrock Knowledge Bases.

### national-park-service  ↔  national-parks-service  (0.957)

- **Types**: mcp-server ↔ mcp-server
- **Shared tags**: (none)
- **national-park-service unique tags**: (none)
- **national-parks-service unique tags**: (none)
- **national-park-service** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\n\national-park-service.md`
  - desc: Integrates with the National Park Service API to provide structured park
- **national-parks-service** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\n\national-parks-service.md`
  - desc: Provides real-time National Park Service data for searching parks by

### codebase-cleanup-deps-audit  ↔  dependency-management-deps-audit  (0.957)

- **Types**: skill ↔ skill
- **Shared tags**: (none)
- **codebase-cleanup-deps-audit unique tags**: (none)
- **dependency-management-deps-audit unique tags**: (none)
- **codebase-cleanup-deps-audit** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\codebase-cleanup-deps-audit.md`
  - desc: (none)
- **dependency-management-deps-audit** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\dependency-management-deps-audit.md`
  - desc: (none)

### vscode-commands  ↔  vscode-internal-commands  (0.957)

- **Types**: mcp-server ↔ mcp-server
- **Shared tags**: (none)
- **vscode-commands unique tags**: (none)
- **vscode-internal-commands unique tags**: (none)
- **vscode-commands** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\v\vscode-commands.md`
  - desc: Bridges VSCode extensions with external tools by exposing VSCode commands
- **vscode-internal-commands** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\v\vscode-internal-commands.md`
  - desc: VSCode extension that exposes VSCode's internal commands and functionality,

### security-audit  ↔  web-security-testing  (0.957)

- **Types**: skill ↔ skill
- **Shared tags**: (none)
- **security-audit unique tags**: (none)
- **web-security-testing unique tags**: (none)
- **security-audit** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\security-audit.md`
  - desc: (none)
- **web-security-testing** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\web-security-testing.md`
  - desc: (none)

### headless-ida-pro  ↔  ida-pro-headless  (0.956)

- **Types**: mcp-server ↔ mcp-server
- **Shared tags**: (none)
- **headless-ida-pro unique tags**: (none)
- **ida-pro-headless unique tags**: (none)
- **headless-ida-pro** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\h\headless-ida-pro.md`
  - desc: Enables reverse engineering of binary files through IDA Pro's headless
- **ida-pro-headless** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\i\ida-pro-headless.md`
  - desc: Provides headless access to IDA Pro's reverse engineering capabilities

### solana-agent  ↔  solana-agent-kit  (0.956)

- **Types**: mcp-server ↔ mcp-server
- **Shared tags**: (none)
- **solana-agent unique tags**: (none)
- **solana-agent-kit unique tags**: (none)
- **solana-agent** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\s\solana-agent.md`
  - desc: Enables blockchain interactions on Solana by providing a comprehensive
- **solana-agent-kit** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\s\solana-agent-kit.md`
  - desc: Integrates with Solana blockchain to enable token deployment, NFT creation,

### startup-business-analyst-financial-projections  ↔  startup-financial-modeling  (0.956)

- **Types**: skill ↔ skill
- **Shared tags**: (none)
- **startup-business-analyst-financial-projections unique tags**: (none)
- **startup-financial-modeling unique tags**: (none)
- **startup-business-analyst-financial-projections** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\startup-business-analyst-financial-projections.md`
  - desc: (none)
- **startup-financial-modeling** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\startup-financial-modeling.md`
  - desc: (none)

### seo-snippet-hunter  ↔  seo-snippet-hunter  (0.955)

- **Types**: agent ↔ skill
- **Shared tags**: (none)
- **seo-snippet-hunter unique tags**: (none)
- **seo-snippet-hunter unique tags**: (none)
- **seo-snippet-hunter** path: `C:\Users\solun\.claude\skill-wiki\entities\agents\seo-snippet-hunter.md`
  - desc: (none)
- **seo-snippet-hunter** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\seo-snippet-hunter.md`
  - desc: (none)

### porkbun  ↔  porkbun-dns  (0.955)

- **Types**: mcp-server ↔ mcp-server
- **Shared tags**: (none)
- **porkbun unique tags**: (none)
- **porkbun-dns unique tags**: (none)
- **porkbun** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\p\porkbun.md`
  - desc: Manage Porkbun domains, DNS records, DNSSEC, SSL certificates, and URL
- **porkbun-dns** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\p\porkbun-dns.md`
  - desc: Manage DNS records, domains, DNSSEC, and SSL certificates through Porkbun's

### knowledge-graph  ↔  memory-knowledge-graph  (0.955)

- **Types**: mcp-server ↔ mcp-server
- **Shared tags**: (none)
- **knowledge-graph unique tags**: (none)
- **memory-knowledge-graph unique tags**: (none)
- **knowledge-graph** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\k\knowledge-graph.md`
  - desc: Provides persistent memory for Claude through a local knowledge graph
- **memory-knowledge-graph** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\m\memory-knowledge-graph.md`
  - desc: Provides a persistent knowledge graph system for maintaining structured

### weather  ↔  weather-alerts-forecasts  (0.955)

- **Types**: mcp-server ↔ mcp-server
- **Shared tags**: (none)
- **weather unique tags**: (none)
- **weather-alerts-forecasts unique tags**: (none)
- **weather** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\w\weather.md`
  - desc: Integrates with weather data APIs to provide current conditions and forecasts
- **weather-alerts-forecasts** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\w\weather-alerts-forecasts.md`
  - desc: Integrates with OpenWeather to retrieve and provide weather data.

### rednote-xiaohongshu  ↔  xiaohongshu-rednote  (0.954)

- **Types**: mcp-server ↔ mcp-server
- **Shared tags**: (none)
- **rednote-xiaohongshu unique tags**: (none)
- **xiaohongshu-rednote unique tags**: (none)
- **rednote-xiaohongshu** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\r\rednote-xiaohongshu.md`
  - desc: Provides a bridge to Xiaohongshu (Red Note) social media platform for
- **xiaohongshu-rednote** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\x\xiaohongshu-rednote.md`
  - desc: Automates Xiaohongshu (RedNote) interactions through browser automation

### aws-bedrock-knowledge-base  ↔  aws-knowledge-base  (0.954)

- **Types**: mcp-server ↔ mcp-server
- **Shared tags**: (none)
- **aws-bedrock-knowledge-base unique tags**: (none)
- **aws-knowledge-base unique tags**: (none)
- **aws-bedrock-knowledge-base** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\a\aws-bedrock-knowledge-base.md`
  - desc: Query and retrieve information from AWS knowledge bases using the Bedrock
- **aws-knowledge-base** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\a\aws-knowledge-base.md`
  - desc: Integrates with AWS Knowledge Bases to retrieve information using Bedrock

### html-to-markdown  ↔  markdown-to-html  (0.954)

- **Types**: mcp-server ↔ mcp-server
- **Shared tags**: (none)
- **html-to-markdown unique tags**: (none)
- **markdown-to-html unique tags**: (none)
- **html-to-markdown** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\h\html-to-markdown.md`
  - desc: Convert HTML to clean Markdown, stripping scripts and styles.
- **markdown-to-html** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\m\markdown-to-html.md`
  - desc: Convert Markdown to clean HTML with headings, lists, and code blocks.

### seo-content-auditor  ↔  seo-content-auditor  (0.953)

- **Types**: agent ↔ skill
- **Shared tags**: (none)
- **seo-content-auditor unique tags**: (none)
- **seo-content-auditor unique tags**: (none)
- **seo-content-auditor** path: `C:\Users\solun\.claude\skill-wiki\entities\agents\seo-content-auditor.md`
  - desc: (none)
- **seo-content-auditor** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\seo-content-auditor.md`
  - desc: (none)

### aws-bedrock-knowledge-base-retrieval  ↔  aws-knowledge-base  (0.953)

- **Types**: mcp-server ↔ mcp-server
- **Shared tags**: (none)
- **aws-bedrock-knowledge-base-retrieval unique tags**: (none)
- **aws-knowledge-base unique tags**: (none)
- **aws-bedrock-knowledge-base-retrieval** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\a\aws-bedrock-knowledge-base-retrieval.md`
  - desc: Bridge to access Amazon Bedrock Knowledge Bases.
- **aws-knowledge-base** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\a\aws-knowledge-base.md`
  - desc: Integrates with AWS Knowledge Bases to retrieve information using Bedrock

### haskell-pro  ↔  haskell-pro  (0.953)

- **Types**: agent ↔ skill
- **Shared tags**: (none)
- **haskell-pro unique tags**: (none)
- **haskell-pro unique tags**: (none)
- **haskell-pro** path: `C:\Users\solun\.claude\skill-wiki\entities\agents\haskell-pro.md`
  - desc: (none)
- **haskell-pro** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\haskell-pro.md`
  - desc: (none)

### shipsaving  ↔  shipswift  (0.952)

- **Types**: mcp-server ↔ mcp-server
- **Shared tags**: (none)
- **shipsaving unique tags**: (none)
- **shipswift unique tags**: (none)
- **shipsaving** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\s\shipsaving.md`
  - desc: Access ShipSaving APIs for shipping rates, label generation, order management,
- **shipswift** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\s\shipswift.md`
  - desc: Shipping and logistics management with rate comparison, label generation,

### golang-pro  ↔  golang-pro  (0.952)

- **Types**: agent ↔ skill
- **Shared tags**: (none)
- **golang-pro unique tags**: (none)
- **golang-pro unique tags**: (none)
- **golang-pro** path: `C:\Users\solun\.claude\skill-wiki\entities\agents\golang-pro.md`
  - desc: (none)
- **golang-pro** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\golang-pro.md`
  - desc: (none)

### eve-online  ↔  eve-online-est  (0.952)

- **Types**: mcp-server ↔ mcp-server
- **Shared tags**: (none)
- **eve-online unique tags**: (none)
- **eve-online-est unique tags**: (none)
- **eve-online** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\e\eve-online.md`
  - desc: Integrates with EVE Online's API to provide real-time market data, item
- **eve-online-est** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\e\eve-online-est.md`
  - desc: Provides EVE Online server time and daily maintenance downtime calculations

### us-weather-nws  ↔  weather-alerts-forecasts  (0.951)

- **Types**: mcp-server ↔ mcp-server
- **Shared tags**: (none)
- **us-weather-nws unique tags**: (none)
- **weather-alerts-forecasts unique tags**: (none)
- **us-weather-nws** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\u\us-weather-nws.md`
  - desc: US National Weather Service alerts by state and short-term forecasts
- **weather-alerts-forecasts** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\w\weather-alerts-forecasts.md`
  - desc: Integrates with OpenWeather to retrieve and provide weather data.

### google-programmable-search-engine  ↔  google-search-via-chrome  (0.951)

- **Types**: mcp-server ↔ mcp-server
- **Shared tags**: (none)
- **google-programmable-search-engine unique tags**: (none)
- **google-search-via-chrome unique tags**: (none)
- **google-programmable-search-engine** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\g\google-programmable-search-engine.md`
  - desc: Integrates with Google Programmable Search Engine to enable web search
- **google-search-via-chrome** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\g\google-search-via-chrome.md`
  - desc: Integrates Google search and webpage content extraction via Chrome browser

### akshare-chinese-financial-data  ↔  akshare-financial-data  (0.951)

- **Types**: mcp-server ↔ mcp-server
- **Shared tags**: (none)
- **akshare-chinese-financial-data unique tags**: (none)
- **akshare-financial-data unique tags**: (none)
- **akshare-chinese-financial-data** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\a\akshare-chinese-financial-data.md`
  - desc: Integrates with AKShare to provide real-time financial data and analysis
- **akshare-financial-data** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\a\akshare-financial-data.md`
  - desc: Provides direct access to AKShare's financial data capabilities for retrieving

### claude-context  ↔  claude-context-local  (0.951)

- **Types**: mcp-server ↔ mcp-server
- **Shared tags**: (none)
- **claude-context unique tags**: (none)
- **claude-context-local unique tags**: (none)
- **claude-context** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\c\claude-context.md`
  - desc: Provides semantic code search and indexing using vector embeddings and
- **claude-context-local** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\c\claude-context-local.md`
  - desc: Provides local semantic code search using EmbeddingGemma embeddings and

### trilium-notes  ↔  triliumnext-notes  (0.951)

- **Types**: mcp-server ↔ mcp-server
- **Shared tags**: (none)
- **trilium-notes unique tags**: (none)
- **triliumnext-notes unique tags**: (none)
- **trilium-notes** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\t\trilium-notes.md`
  - desc: Connects to Trilium Notes through its ETAPI for creating, editing, and
- **triliumnext-notes** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\t\triliumnext-notes.md`
  - desc: Integrates with TriliumNext Notes for creating, retrieving, updating,

### newsapi  ↔  search1api  (0.951)

- **Types**: mcp-server ↔ mcp-server
- **Shared tags**: (none)
- **newsapi unique tags**: (none)
- **search1api unique tags**: (none)
- **newsapi** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\n\newsapi.md`
  - desc: Searches news articles, retrieves top headlines, and lists sources from
- **search1api** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\s\search1api.md`
  - desc: Execute web searches, news queries, and content extraction.

### ruby-pro  ↔  ruby-pro  (0.951)

- **Types**: agent ↔ skill
- **Shared tags**: (none)
- **ruby-pro unique tags**: (none)
- **ruby-pro unique tags**: (none)
- **ruby-pro** path: `C:\Users\solun\.claude\skill-wiki\entities\agents\ruby-pro.md`
  - desc: (none)
- **ruby-pro** path: `C:\Users\solun\.claude\skill-wiki\entities\skills\ruby-pro.md`
  - desc: (none)

### couchbase  ↔  couchdb  (0.951)

- **Types**: mcp-server ↔ mcp-server
- **Shared tags**: (none)
- **couchbase unique tags**: (none)
- **couchdb unique tags**: (none)
- **couchbase** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\c\couchbase.md`
  - desc: Connect to Couchbase clusters for document management, SQL++ queries,
- **couchdb** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\c\couchdb.md`
  - desc: Integrates with CouchDB to enable database management, document operations,

### exa  ↔  exa-ai  (0.951)

- **Types**: mcp-server ↔ mcp-server
- **Shared tags**: (none)
- **exa unique tags**: (none)
- **exa-ai unique tags**: (none)
- **exa** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\e\exa.md`
  - desc: Integrates with Exa.ai to enable web searches with customizable parameters
- **exa-ai** path: `C:\Users\solun\.claude\skill-wiki\entities\mcp-servers\e\exa-ai.md`
  - desc: Integrates with Exa AI's search engine to provide real-time web searching,
