import { readdirSync, readFileSync } from 'fs';
import { JSDOM } from 'jsdom';

const dom = new JSDOM('<!doctype html><html><body></body></html>');
global.window = dom.window;
global.document = dom.window.document;
Object.defineProperty(global, 'navigator', { value: dom.window.navigator, configurable: true });
global.Element = dom.window.Element;

const { default: mermaid } = await import('mermaid');
mermaid.initialize({ startOnLoad: false, securityLevel: 'loose' });

const dir = '/home/yery/Projects/BareNOC/src/api/wiki';
const files = readdirSync(dir).filter(f => f.endsWith('.md'));
let total = 0, ok = 0, failures = [];
for (const f of files) {
  const raw = readFileSync(`${dir}/${f}`, 'utf8');
  const re = /```mermaid\n([\s\S]*?)```/g;
  let m, idx = 0;
  while ((m = re.exec(raw))) {
    total++; idx++;
    try { await mermaid.parse(m[1]); ok++; }
    catch (e) { failures.push(`${f} block#${idx}: ${(e.message || String(e)).split('\n')[0]}`); }
  }
}
console.log(`validated ${total} mermaid blocks: ${ok} OK, ${failures.length} FAILED`);
for (const f of failures) console.log('  ✗', f);
process.exit(failures.length ? 1 : 0);
