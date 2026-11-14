import type { CommentBlock, CommentPreviewResult, CommentSelection } from './commentSourceMap.js';

/** Self-contained: also serialized into an opaque-origin HTML iframe. No closure dependencies. */
export function mountCommentPreview(
  root: HTMLElement,
  blocks: CommentBlock[],
  attribute: string,
  report: (result: CommentPreviewResult) => void,
  invalidateContent = false,
) {
  const doc = root.ownerDocument;
  const win = doc.defaultView!;
  const documentRoot = root === doc.documentElement;
  const eventRoot: EventTarget = documentRoot ? win : root;
  const hitAttribute = `${attribute}-hit`;
  const nodes = new Map<string, Element>();
  const invalid = new Set<string>();
  const highlighted = new Set<Element>();
  let selection: CommentSelection = { range: null, sequence: -1 };
  let disposed = false;
  let ready = false;
  let pendingLocate = false;
  let lastResult: CommentPreviewResult | undefined;
  const originalTabIndex = root.getAttribute('tabindex');
  root.tabIndex = 0;
  const style = doc.createElement('style');
  // A unique root attribute scopes the rules to the rendered pane in the host document.
  root.setAttribute(`${attribute}-root`, '');
  style.textContent = `[${attribute}-root], [${attribute}-root] * { user-select:none!important; -webkit-user-select:none!important; }
    [${hitAttribute}] { outline:3px solid #1677ff!important; outline-offset:-3px!important; background-color:rgba(22,119,255,.14)!important; }
    [${attribute}-root] iframe { pointer-events:none!important; }`;
  doc.head.appendChild(style);
  const savedTabs = new Map<Element, string | null>();
  function disableControls() {
    root
      .querySelectorAll(
        'a,button,input,textarea,select,summary,[tabindex],[contenteditable],audio,video,iframe',
      )
      .forEach((node) => {
        if (!savedTabs.has(node)) savedTabs.set(node, node.getAttribute('tabindex'));
        if (node.getAttribute('tabindex') !== '-1') node.setAttribute('tabindex', '-1');
      });
  }
  disableControls();
  function stop(event: Event) {
    event.preventDefault();
    event.stopImmediatePropagation();
  }
  function isolate(event: Event) {
    if (event.type === 'wheel' || event.type === 'touchmove') pendingLocate = false;
    event.stopImmediatePropagation();
  }
  const blocked = [
    'selectstart',
    'dragstart',
    'click',
    'dblclick',
    'auxclick',
    'contextmenu',
    'beforeinput',
    'submit',
    'copy',
    'cut',
    'paste',
    'keyup',
    'keypress',
  ];
  const isolated = [
    'pointerdown',
    'pointerup',
    'mousedown',
    'mouseup',
    'pointermove',
    'mousemove',
    'mouseover',
    'pointerover',
    'mouseenter',
    'pointerenter',
    'touchstart',
    'touchmove',
    'touchend',
    'wheel',
  ];
  blocked.forEach((name) => eventRoot.addEventListener(name, stop, true));
  isolated.forEach((name) =>
    eventRoot.addEventListener(name, isolate, { capture: true, passive: true }),
  );
  function scrollingElement(element: Element) {
    return documentRoot && element === root
      ? ((doc.scrollingElement ?? root) as HTMLElement)
      : (element as HTMLElement);
  }
  function scrollable(element: Element) {
    const target = scrollingElement(element);
    const css = win.getComputedStyle(element);
    return (
      (element === root || /auto|scroll|overlay/.test(css.overflowY + css.overflowX)) &&
      (target.scrollHeight > target.clientHeight || target.scrollWidth > target.clientWidth)
    );
  }
  function focus(event: Event) {
    if (event.target !== root && event.target instanceof win.Element) {
      (event.target as HTMLElement).blur?.();
      root.focus({ preventScroll: true });
    }
  }
  function key(event: Event) {
    const e = event as KeyboardEvent;
    if (e.key === 'Tab' || e.key === 'Escape') return;
    pendingLocate = false;
    stop(e);
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    let container = e.target instanceof win.Element ? e.target : root;
    while (container !== root && !scrollable(container))
      container = container.parentElement ?? root;
    const target = scrollingElement(container);
    const page = Math.max(40, target.clientHeight * 0.8);
    const amounts: Record<string, number> = {
      ArrowDown: 40,
      ArrowUp: -40,
      PageDown: page,
      PageUp: -page,
      ' ': e.shiftKey ? -page : page,
    };
    if (e.key in amounts) target.scrollTop += amounts[e.key];
    else if (e.key === 'Home') target.scrollTop = 0;
    else if (e.key === 'End') target.scrollTop = target.scrollHeight;
    else if (e.key === 'ArrowLeft') target.scrollLeft -= 40;
    else if (e.key === 'ArrowRight') target.scrollLeft += 40;
  }
  eventRoot.addEventListener('focusin', focus, true);
  eventRoot.addEventListener('keydown', key, true);
  function userScroll() {
    pendingLocate = false;
  }
  eventRoot.addEventListener('scroll', userScroll, { capture: true, passive: true });
  function visible(node: Element) {
    if (!node.isConnected || !root.contains(node) || !node.getClientRects().length) return false;
    for (let current: Element | null = node; current; current = current.parentElement) {
      const css = win.getComputedStyle(current);
      if (
        css.display === 'none' ||
        css.visibility === 'hidden' ||
        css.visibility === 'collapse' ||
        css.opacity === '0'
      )
        return false;
      if (current === root) break;
    }
    return true;
  }
  function locate(node: Element) {
    // Adjust only this pane's scrollable ancestors; scrollIntoView could move the modal/page.
    for (let ancestor = node.parentElement; ancestor; ancestor = ancestor.parentElement) {
      if (scrollable(ancestor)) {
        const target = scrollingElement(ancestor);
        const box = node.getBoundingClientRect();
        const frame =
          ancestor === root && documentRoot
            ? { top: 0, left: 0 }
            : ancestor.getBoundingClientRect();
        const top = frame.top + (ancestor === root && documentRoot ? 0 : target.clientTop);
        const left = frame.left + (ancestor === root && documentRoot ? 0 : target.clientLeft);
        if (box.top < top || box.bottom > top + target.clientHeight)
          target.scrollTop += box.top - top - 12;
        if (box.left < left || box.right > left + target.clientWidth)
          target.scrollLeft += box.left - left - 12;
      }
      if (ancestor === root) break;
    }
  }
  function apply(scroll: boolean) {
    highlighted.forEach((node) => node.removeAttribute(hitAttribute));
    highlighted.clear();
    const { range, sequence } = selection;
    function result(count: number) {
      if (lastResult?.sequence === sequence && lastResult.count === count) return;
      lastResult = { sequence, count };
      report(lastResult);
    }
    if (!range) {
      pendingLocate = false;
      result(0);
      return;
    }
    const matches = blocks.flatMap((block) => {
      const node = nodes.get(block.id);
      return node &&
        !invalid.has(block.id) &&
        visible(node) &&
        block.ranges.some((part) => part.start <= range.end && part.end >= range.start)
        ? [node]
        : [];
    });
    const matching = new Set(matches);
    const ancestors = new Set<Element>();
    for (const node of matches) {
      for (
        let parent = node.parentElement;
        parent && root.contains(parent);
        parent = parent.parentElement
      ) {
        if (matching.has(parent)) ancestors.add(parent);
        if (parent === root) break;
      }
    }
    const hits = matches.filter((node) => !ancestors.has(node));
    hits.sort((a, b) => (a.compareDocumentPosition(b) & 2 ? 1 : -1));
    hits.forEach((node) => {
      node.setAttribute(hitAttribute, '');
      highlighted.add(node);
    });
    pendingLocate =
      scroll &&
      !hits.length &&
      blocks.some(
        (block) =>
          !invalid.has(block.id) &&
          nodes.get(block.id)?.isConnected &&
          block.ranges.some((part) => part.start <= range.end && part.end >= range.start),
      );
    if (scroll && hits[0]) locate(hits[0]);
    result(hits.length);
  }
  const observer = new win.MutationObserver((records) => {
    if (disposed) return;
    disableControls();
    if (invalidateContent) {
      for (const [id, node] of nodes) {
        if (
          !node.isConnected ||
          records.some((record) => record.type !== 'attributes' && node.contains(record.target))
        )
          invalid.add(id);
      }
    }
    // Invalidation may clear marks, but never repeatedly scroll after the user moves the pane.
    if (selection.range) apply(pendingLocate);
  });
  function contentLoaded() {
    if (ready && pendingLocate && !disposed) apply(true);
  }
  root.addEventListener('load', contentLoaded, true);
  function initialize() {
    if (disposed || ready) return;
    for (const block of blocks) {
      const node = root.querySelector(`[${attribute}="${block.id}"]`);
      if (node) nodes.set(block.id, node);
    }
    disableControls();
    ready = true;
    observer.observe(root, {
      subtree: true,
      childList: true,
      characterData: true,
      attributes: true,
      attributeFilter: ['hidden', 'style', 'class'],
    });
    if (selection.sequence >= 0) apply(true);
  }
  if (documentRoot && doc.readyState === 'loading')
    doc.addEventListener('DOMContentLoaded', initialize, { once: true });
  else initialize();
  return {
    update(next: CommentSelection) {
      if (disposed || !Number.isSafeInteger(next.sequence) || next.sequence <= selection.sequence)
        return;
      const r = next.range;
      if (
        r !== null &&
        (!r ||
          !Number.isSafeInteger(r.start) ||
          !Number.isSafeInteger(r.end) ||
          r.start < 1 ||
          r.end < r.start)
      )
        return;
      selection = next;
      if (ready) apply(true);
    },
    dispose() {
      disposed = true;
      doc.removeEventListener('DOMContentLoaded', initialize);
      observer.disconnect();
      blocked.forEach((name) => eventRoot.removeEventListener(name, stop, true));
      isolated.forEach((name) => eventRoot.removeEventListener(name, isolate, true));
      eventRoot.removeEventListener('focusin', focus, true);
      eventRoot.removeEventListener('keydown', key, true);
      eventRoot.removeEventListener('scroll', userScroll, true);
      root.removeEventListener('load', contentLoaded, true);
      highlighted.forEach((node) => node.removeAttribute(hitAttribute));
      savedTabs.forEach((value, node) =>
        value === null ? node.removeAttribute('tabindex') : node.setAttribute('tabindex', value),
      );
      originalTabIndex === null
        ? root.removeAttribute('tabindex')
        : root.setAttribute('tabindex', originalTabIndex);
      root.removeAttribute(`${attribute}-root`);
      style.remove();
    },
  };
}
