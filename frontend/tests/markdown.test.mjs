import assert from 'node:assert/strict'
import test from 'node:test'
import { renderMarkdown } from '../src/utils/markdown.js'

test('reports and interviews render hostile HTML as text', () => {
  for (const payload of [
    '<img src=x onerror="alert(1)">',
    '<svg/onload=alert(1)>',
    '</p><script>alert(1)</script>',
    '**<iframe srcdoc="<script>alert(1)</script>">**',
    '```html\n<img src=x onerror=alert(1)>\n```',
    '&lt;img src=x onerror=alert(1)&gt;',
  ]) {
    const html = renderMarkdown(payload)
    assert.doesNotMatch(html, /<(?:img|svg|script|iframe)\b/i)
    assert.match(html, /&(?:lt|amp);/)
  }
})

test('safe formatting, quotations and literal code survive escaping', () => {
  const html = renderMarkdown('### Findings\n**Avery**\n> Evidence\n`<value>`')
  assert.match(html, /<h4 class="md-h4">Findings<\/h4>/)
  assert.match(html, /<strong>Avery<\/strong>/)
  assert.match(html, /<blockquote class="md-quote">Evidence<\/blockquote>/)
  assert.match(html, /<code class="inline-code">&lt;value&gt;<\/code>/)
})
