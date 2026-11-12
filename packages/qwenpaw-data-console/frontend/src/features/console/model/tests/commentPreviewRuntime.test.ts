import assert from 'node:assert/strict';
import test from 'node:test';
import vm from 'node:vm';
import { unified } from 'unified';
import remarkParse from 'remark-parse';
import remarkGfm from 'remark-gfm';
import remarkRehype from 'remark-rehype';
import { parse } from 'parse5';
import { mountCommentPreview } from '../artifacts/commentPreviewRuntime.js';
import { COMMENT_PREVIEW_CHANNEL, createCommentHtml } from '../artifacts/commentPreviewBridge.js';
import type { CommentPreviewResult } from '../artifacts/commentSourceMap.js';
import { mapHtmlBlocks, mapMarkdownBlocks } from '../artifacts/commentSourceMap.js';

// A small deterministic DOM contract, not a browser/visual acceptance substitute.
class Events {
  handlers = new Map<string, Set<(event: any) => void>>();
  addEventListener(name: string, handler: (event: any) => void) {
    if (!this.handlers.has(name)) this.handlers.set(name, new Set());
    this.handlers.get(name)!.add(handler);
  }
  removeEventListener(name: string, handler: (event: any) => void) {
    this.handlers.get(name)?.delete(handler);
  }
  fire(name: string, fields: Record<string, unknown> = {}) {
    const event = {
      defaultPrevented: false,
      stopped: false,
      preventDefault() {
        this.defaultPrevented = true;
      },
      stopImmediatePropagation() {
        this.stopped = true;
      },
      ...fields,
    };
    this.handlers.get(name)?.forEach((handler) => handler(event));
    return event;
  }
}

class ElementStub extends Events {
  attrs = new Map<string, string>();
  children: ElementStub[] = [];
  parentElement: ElementStub | null = null;
  ownerDocument: any;
  scrollTop = 0;
  scrollLeft = 0;
  scrollHeight = 100;
  scrollWidth = 200;
  clientHeight = 100;
  clientWidth = 200;
  clientTop = 0;
  clientLeft = 0;
  top = 0;
  height = 20;
  order = 0;
  display = 'block';
  visibility = 'visible';
  opacity = '1';
  overflowY = 'auto';
  overflowX = 'auto';
  textContent = '';
  isConnected = true;
  control = false;
  tabIndex = -1;
  setAttribute(name: string, value: string) {
    this.attrs.set(name, value);
  }
  getAttribute(name: string) {
    return this.attrs.get(name) ?? null;
  }
  removeAttribute(name: string) {
    this.attrs.delete(name);
  }
  appendChild(node: ElementStub) {
    this.children.push(node);
    node.parentElement = this;
    node.ownerDocument = this.ownerDocument;
    return node;
  }
  contains(node: ElementStub): boolean {
    return node === this || this.children.some((child) => child.contains(node));
  }
  all(): ElementStub[] {
    return this.children.flatMap((child) => [child, ...child.all()]);
  }
  querySelector(selector: string) {
    const match = /^\[([^=]+)="([^"]*)"\]$/.exec(selector);
    return this.all().find((node) => match && node.getAttribute(match[1]) === match[2]) ?? null;
  }
  querySelectorAll(_selector: string) {
    return this.all().filter((node) => node.control);
  }
  getClientRects() {
    return this.display === 'none' ? [] : [this.getBoundingClientRect()];
  }
  getBoundingClientRect() {
    let top = this.top;
    for (let parent = this.parentElement; parent; parent = parent.parentElement)
      top -= parent.scrollTop;
    return { top, bottom: top + this.height, left: 0, right: 100 };
  }
  compareDocumentPosition(other: ElementStub) {
    return this.order < other.order ? 4 : 2;
  }
  remove() {
    this.isConnected = false;
    if (this.parentElement)
      this.parentElement.children = this.parentElement.children.filter((node) => node !== this);
  }
  focus() {}
  blur() {}
}

function fixture(documentMode = false, loading = false) {
  const observers: { callback: (records: any[]) => void; disconnected: boolean }[] = [];
  const win = Object.assign(new Events(), {
    Element: ElementStub,
    getComputedStyle: (node: ElementStub) => node,
    MutationObserver: class {
      state: (typeof observers)[number];
      constructor(callback: (records: any[]) => void) {
        this.state = { callback, disconnected: false };
        observers.push(this.state);
      }
      observe() {}
      disconnect() {
        this.state.disconnected = true;
      }
    },
    parent: { postMessage: (_message: unknown) => {} },
  });
  const doc: any = Object.assign(new Events(), {
    defaultView: win,
    readyState: loading ? 'loading' : 'complete',
    createElement: () => {
      const node = new ElementStub();
      node.ownerDocument = doc;
      return node;
    },
  });
  const page = new ElementStub();
  page.ownerDocument = doc;
  page.scrollHeight = 3000;
  const root = documentMode ? page : page.appendChild(new ElementStub());
  root.scrollHeight = 1600;
  root.height = 100;
  doc.documentElement = page;
  doc.scrollingElement = page;
  doc.head = doc.createElement();
  const a = root.appendChild(new ElementStub());
  a.top = 400;
  a.order = 1;
  const b = root.appendChild(new ElementStub());
  b.top = 800;
  b.order = 2;
  a.setAttribute('data-fd-comment-test', '0');
  b.setAttribute('data-fd-comment-test', '1');
  const messages: CommentPreviewResult[] = [];
  const blocks = [
    { id: '0', ranges: [{ start: 2, end: 2 }] },
    { id: '1', ranges: [{ start: 4, end: 4 }] },
  ];
  const mount = (invalidate = false) =>
    mountCommentPreview(
      root as unknown as HTMLElement,
      blocks,
      'data-fd-comment-test',
      (result) => messages.push(result),
      invalidate,
    );
  return { win, doc, page, root, a, b, messages, blocks, mount, observers };
}

test('source ranges highlight the matching block, locate only the rendered pane and allow later manual scrolling', () => {
  const f = fixture();
  const runtime = f.mount();
  runtime.update({ range: { start: 4, end: 4 }, sequence: 1 });
  assert.equal(f.b.getAttribute('data-fd-comment-test-hit'), '');
  assert.equal(f.a.getAttribute('data-fd-comment-test-hit'), null);
  assert.equal(f.root.scrollTop, 788);
  assert.equal(f.page.scrollTop, 0);
  f.root.scrollTop = 250;
  f.observers[0].callback([{ type: 'attributes', target: f.b }]);
  assert.equal(f.root.scrollTop, 250);
  runtime.update({ range: { start: 2, end: 2 }, sequence: 0 });
  assert.equal(f.root.scrollTop, 250);
  runtime.update({ range: null, sequence: 2 });
  assert.equal(f.b.getAttribute('data-fd-comment-test-hit'), null);
  assert.equal(f.root.scrollTop, 250);
  runtime.update({ range: { start: 4, end: 4 }, sequence: 3 });
  assert.equal(f.root.scrollTop, 788);
  runtime.dispose();
  assert.equal(f.observers[0].disconnected, true);
  assert.equal(f.b.getAttribute('data-fd-comment-test-hit'), null);
});

test('multiple matches highlight leaves, locate the first and do not move an already visible target', () => {
  const f = fixture();
  const child = f.a.appendChild(new ElementStub());
  child.top = 410;
  child.order = 3;
  child.setAttribute('data-fd-comment-test', '2');
  f.blocks.push({ id: '2', ranges: [{ start: 2, end: 2 }] });
  const runtime = f.mount();
  runtime.update({ range: { start: 2, end: 4 }, sequence: 1 });
  assert.equal(f.a.getAttribute('data-fd-comment-test-hit'), null);
  assert.equal(child.getAttribute('data-fd-comment-test-hit'), '');
  assert.equal(f.b.getAttribute('data-fd-comment-test-hit'), '');
  // Fake document order: b (2) before child (3).
  assert.equal(f.root.scrollTop, 788);
  runtime.update({ range: { start: 4, end: 4 }, sequence: 2 });
  assert.equal(f.root.scrollTop, 788);
  runtime.dispose();
});

test('nested Markdown list labels remain highlighted alongside children, with independent source ranges', async () => {
  for (const marker of ['-', '1.']) {
    for (const newline of ['\n', '\r\n']) {
      const source = [
        `${marker} first`,
        `${marker} parent **bold** and [link](https://example.com)`,
        `   ${marker} child`,
        `      ${marker} grandchild`,
        `${marker} last`,
      ].join(newline);
      const processor = unified().use(remarkParse).use(remarkGfm).use(remarkRehype);
      const tree = await processor.run(processor.parse(source));
      const attribute = 'data-fd-comment-test';
      const f = fixture();
      f.a.remove();
      f.b.remove();
      f.blocks.splice(0, f.blocks.length, ...mapMarkdownBlocks(tree, attribute, source));
      let order = 0;
      function render(node: any, parent: ElementStub) {
        if (node.type === 'text') {
          parent.textContent += node.value;
          return;
        }
        const element = parent.appendChild(new ElementStub());
        element.order = order++;
        if (node.properties?.[attribute] !== undefined)
          element.setAttribute(attribute, node.properties[attribute]);
        node.children?.forEach((child: any) => render(child, element));
        parent.textContent += element.textContent;
      }
      render(tree, f.root);
      const runtime = f.mount();
      const hits = () =>
        f.root
          .all()
          .filter((node) => node.getAttribute(`${attribute}-hit`) !== null)
          .map((node) => node.textContent.trim());
      runtime.update({ range: { start: 1, end: 5 }, sequence: 1 });
      assert.deepEqual(hits(), ['first', 'parent bold and link', 'child', 'grandchild', 'last']);
      runtime.update({ range: { start: 2, end: 2 }, sequence: 2 });
      assert.deepEqual(hits(), ['parent bold and link']);
      runtime.update({ range: { start: 3, end: 3 }, sequence: 3 });
      assert.deepEqual(hits(), ['child']);
      runtime.update({ range: { start: 4, end: 4 }, sequence: 4 });
      assert.deepEqual(hits(), ['grandchild']);
      runtime.update({ range: null, sequence: 5 });
      assert.deepEqual(hits(), []);
      runtime.dispose();
    }
  }
});

test('HTML nested list labels and inline text highlight independently of their descendants', () => {
  for (const tag of ['ul', 'ol']) {
    for (const newline of ['\n', '\r\n']) {
      const source = [
        `<${tag}>`,
        '<li>first</li>',
        '<li>parent',
        `<${tag}>`,
        '<li>child',
        `<${tag}><li>grandchild</li></${tag}>`,
        '</li>',
        `</${tag}>`,
        '</li>',
        '<li>last</li>',
        `</${tag}>`,
      ].join(newline);
      const attribute = 'data-fd-comment-test';
      const mapped = mapHtmlBlocks(source, attribute);
      const f = fixture();
      f.a.remove();
      f.b.remove();
      f.blocks.splice(0, f.blocks.length, ...mapped.blocks);
      let order = 0;
      function render(node: any, parent: ElementStub) {
        if (node.nodeName === '#text') {
          parent.textContent += node.value;
          return;
        }
        const element = parent.appendChild(new ElementStub());
        element.order = order++;
        for (const attr of node.attrs ?? []) element.setAttribute(attr.name, attr.value);
        node.childNodes?.forEach((child: any) => render(child, element));
        parent.textContent += element.textContent;
      }
      render(parse(mapped.html), f.root);
      const runtime = f.mount(true);
      const hits = () =>
        f.root
          .all()
          .filter((node) => node.getAttribute(`${attribute}-hit`) !== null)
          .map((node) => node.textContent.trim());
      runtime.update({ range: { start: 2, end: 10 }, sequence: 1 });
      assert.deepEqual(hits(), ['first', 'parent', 'child', 'grandchild', 'last']);
      runtime.update({ range: { start: 3, end: 3 }, sequence: 2 });
      assert.deepEqual(hits(), ['parent']);
      runtime.update({ range: { start: 5, end: 5 }, sequence: 3 });
      assert.deepEqual(hits(), ['child']);
      runtime.update({ range: { start: 6, end: 6 }, sequence: 4 });
      assert.deepEqual(hits(), ['grandchild']);
      runtime.update({ range: null, sequence: 5 });
      assert.deepEqual(hits(), []);
      runtime.dispose();
    }
  }
});

test('invalid, absent, hidden and dynamically invalidated blocks never jump to unrelated content', () => {
  const f = fixture();
  const runtime = f.mount(true);
  runtime.update({ range: { start: 4, end: 3 }, sequence: 1 });
  assert.equal(f.messages.length, 0);
  runtime.update({ range: { start: 3, end: 3 }, sequence: 2 });
  assert.equal(f.messages[f.messages.length - 1]?.count, 0);
  assert.equal(f.root.scrollTop, 0);
  f.b.visibility = 'hidden';
  runtime.update({ range: { start: 4, end: 4 }, sequence: 3 });
  assert.equal(f.messages[f.messages.length - 1]?.count, 0);
  f.b.visibility = 'visible';
  runtime.update({ range: { start: 4, end: 4 }, sequence: 4 });
  f.observers[0].callback([{ type: 'characterData', target: f.b }]);
  assert.equal(f.b.getAttribute('data-fd-comment-test-hit'), null);
  assert.equal(f.messages[f.messages.length - 1]?.count, 0);
  f.root.scrollTop = 0;
  runtime.update({ range: { start: 4, end: 4 }, sequence: 5 });
  assert.equal(f.root.scrollTop, 0);
  runtime.dispose();
});

test('scroll-only lock cancels selection/activation/editing but preserves wheel defaults and scroll keys', () => {
  const f = fixture();
  f.a.control = true;
  f.a.setAttribute('tabindex', '0');
  const runtime = f.mount();
  for (const name of ['selectstart', 'click', 'dragstart', 'beforeinput', 'submit', 'copy'])
    assert.equal(f.root.fire(name).defaultPrevented, true);
  for (const name of ['wheel', 'touchmove', 'pointerdown'])
    assert.equal(f.root.fire(name).defaultPrevented, false);
  assert.equal(f.a.getAttribute('tabindex'), '-1');
  f.root.fire('keydown', { key: 'PageDown', target: f.root });
  assert.equal(f.root.scrollTop, 80);
  assert.equal(f.root.fire('keydown', { key: 'Enter', target: f.a }).defaultPrevented, true);
  assert.equal(f.root.fire('keydown', { key: 'Tab', target: f.root }).defaultPrevented, false);
  runtime.dispose();
  assert.equal(f.a.getAttribute('tabindex'), '0');
  assert.equal(f.root.fire('click').defaultPrevented, false);
});

test('a delayed visible block locates once, but user scroll cancels a pending locate', () => {
  const f = fixture();
  f.b.display = 'none';
  const runtime = f.mount();
  runtime.update({ range: { start: 4, end: 4 }, sequence: 1 });
  assert.equal(f.root.scrollTop, 0);
  f.b.display = 'block';
  f.observers[0].callback([{ type: 'attributes', target: f.b }]);
  assert.equal(f.root.scrollTop, 788);
  f.b.display = 'none';
  f.root.scrollTop = 100;
  runtime.update({ range: { start: 4, end: 4 }, sequence: 2 });
  f.root.fire('wheel', { type: 'wheel' });
  f.b.display = 'block';
  f.observers[0].callback([{ type: 'attributes', target: f.b }]);
  assert.equal(f.root.scrollTop, 100);
  assert.equal(f.b.getAttribute('data-fd-comment-test-hit'), '');
  runtime.dispose();
});

test('nested report scrolling moves the inner viewport without moving the host page', () => {
  const f = fixture();
  const nested = f.root.appendChild(new ElementStub());
  nested.scrollHeight = 1000;
  nested.top = 20;
  f.b.parentElement = nested;
  f.root.children = f.root.children.filter((node) => node !== f.b);
  nested.children.push(f.b);
  const runtime = f.mount();
  runtime.update({ range: { start: 4, end: 4 }, sequence: 1 });
  assert.equal(nested.scrollTop, 768);
  assert.equal(f.root.scrollTop, 0);
  assert.equal(f.page.scrollTop, 0);
  runtime.dispose();
});

test('serialized iframe bridge validates parent/identity, queues only latest selection and survives ready replay', () => {
  const f = fixture(true, true);
  const messages: any[] = [];
  f.win.parent.postMessage = (message) => messages.push(message);
  const html = createCommentHtml(
    '<html><head></head><body>\n<p>one</p>\n\n<p>two</p></body></html>',
    'test',
  );
  const script = html.match(/<script>([\s\S]*?)<\/script>/)?.[1];
  assert.ok(script);
  vm.runInNewContext(script, { window: f.win, document: f.doc });
  const data = {
    channel: COMMENT_PREVIEW_CHANNEL,
    identity: 'test',
    kind: 'select',
    range: { start: 2, end: 2 },
    sequence: 1,
  };
  f.win.fire('message', { source: {}, data });
  f.win.fire('message', { source: f.win.parent, data: { ...data, identity: 'old' } });
  f.win.fire('message', { source: f.win.parent, data });
  f.win.fire('message', {
    source: f.win.parent,
    data: { ...data, range: { start: 4, end: 4 }, sequence: 2 },
  });
  assert.equal(f.root.scrollTop, 0);
  f.doc.readyState = 'complete';
  f.doc.fire('DOMContentLoaded');
  assert.equal(f.root.scrollTop, 788);
  assert.equal(f.a.getAttribute('data-fd-comment-test-hit'), null);
  assert.equal(f.b.getAttribute('data-fd-comment-test-hit'), '');
  f.root.scrollTop = 100;
  f.win.fire('message', { source: f.win.parent, data: { ...data, sequence: 2 } });
  assert.equal(f.root.scrollTop, 100);
  assert.equal(messages[messages.length - 1]?.sequence, 2);
  assert.equal(messages[messages.length - 1]?.count, 1);
});
