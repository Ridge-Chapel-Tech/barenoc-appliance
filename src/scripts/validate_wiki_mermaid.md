# Validate wiki mermaid diagrams

Checks every ```mermaid block in `src/api/wiki/*.md` parses (syntax only).

```bash
cd /tmp && mkdir mmtest && cd mmtest && npm init -y
npm install mermaid@10 jsdom
node <repo>/src/scripts/validate_wiki_mermaid.mjs   # run from anywhere; the wiki dir is resolved relative to the script
```

Requires node + npm. Run before deploying wiki changes.
