# Marketplace Registry

> Known skill/plugin marketplaces and how to interact with them.
> The router queries these when it detects a stack gap (needed skill not installed).

## Registered Marketplaces

### 1. Local Skills Directory

```yaml
name: local
type: filesystem
path: /mnt/skills/
scan_method: directory listing
refresh: always current
priority: 1  # check first
```

List all installed skills:
```bash
find /mnt/skills/ -name "SKILL.md" -maxdepth 3
```

### 2. User Skills Directory

```yaml
name: user-local
type: filesystem
path: /mnt/skills/user/
scan_method: directory listing
refresh: always current
priority: 2
```

User-uploaded or custom skills. Same scan as local but separate namespace.

### 3. Example Skills

```yaml
name: examples
type: filesystem
path: /mnt/skills/examples/
scan_method: directory listing
refresh: always current
priority: 3
```

Bundled example skills that may not be active but can be copied to user skills.

### 4. GitHub Skill Repos

```yaml
name: github
type: git
base_url: https://github.com
search_method: topic search "claude-skill" OR "hermes-skill"
install_method: git clone + copy SKILL.md to /mnt/skills/user/
refresh: on-demand
priority: 5
```

Search pattern:
```bash
# Via GitHub API (if available)
curl -s "https://api.github.com/search/repositories?q=topic:claude-skill&sort=stars"

# Via web search fallback
web_search "claude skill github site:github.com"
```

### 5. npm Registry (for JS/TS skills packaged as npm)

```yaml
name: npm
type: package-registry
base_url: https://registry.npmjs.org
search_method: keyword search "claude-skill"
install_method: npm install -g <package>
refresh: on-demand
priority: 6
```

### 6. PyPI (for Python skills packaged as pip)

```yaml
name: pypi
type: package-registry
base_url: https://pypi.org
search_method: keyword search "claude-skill" OR "hermes-skill"
install_method: pip install <package>
refresh: on-demand
priority: 6
```

---

## Marketplace Query Protocol

When the resolver identifies a gap (stack detected, no skill available):

```
1. Check local -> user-local -> examples (instant, filesystem)
2. If not found, check wiki for cached marketplace data
3. If cache is stale or empty:
   a. Query GitHub topics
   b. Query npm/pypi if relevant language
   c. Cache results in raw/marketplace-dumps/
   d. Create/update entity pages for discovered skills
4. Present findings to user with install commands
```

## Caching

Marketplace results are cached in the wiki at:
```
raw/marketplace-dumps/<marketplace-name>-YYYY-MM.md
```

Each dump is a markdown table:

```markdown
# GitHub Marketplace Dump -- 2026-04

| Name | URL | Stars | Description | Stacks | Last Updated |
|------|-----|-------|-------------|--------|--------------|
| ... | ... | ... | ... | ... | ... |
```

Refresh policy: controlled by `refresh_interval_days` on each marketplace's
entity page. Default 7 days. User can override per marketplace.

## Installing from Marketplace

The install flow:
1. Router suggests: "The `terraform` skill is available on GitHub. Install it?"
2. User confirms
3. Router executes:
   ```bash
   # GitHub example
   git clone https://github.com/user/terraform-skill.git /tmp/terraform-skill
   cp -r /tmp/terraform-skill /mnt/skills/user/terraform/
   rm -rf /tmp/terraform-skill
   ```
4. Create entity page in wiki
5. Add to current manifest and load
6. Log the install

## Security Notes

- Never auto-install without user confirmation
- Always show the source URL before installing
- For git repos: check for SKILL.md at root, reject if missing
- Never execute arbitrary scripts from marketplace skills without user review
- Warn if a skill requires network access or system-level permissions
