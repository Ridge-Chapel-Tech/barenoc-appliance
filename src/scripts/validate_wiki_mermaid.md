# Validate wiki mermaid diagrams

Checks every ```mermaid block in `src/api/wiki/*.md` parses (syntax only).

```bash
cd /tmp && mkdir mmtest && cd mmtest && npm init -y
npm install mermaid@10 jsdom
node /home/yery/Projects/BareNOC/src/scripts/validate_wiki_mermaid.mjs
```

Requires node + npm. Run before deploying wiki changes.
