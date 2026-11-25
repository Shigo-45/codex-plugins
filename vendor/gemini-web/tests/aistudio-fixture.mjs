import { runInNewContext } from "node:vm";

function matchCompound(element, compound) {
  let rest = compound;
  const attributes = [];
  rest = rest.replace(/\[([^\]]+)\]/g, (_match, body) => {
    attributes.push(body);
    return " ";
  });
  rest = rest.trim();
  if (rest) {
    const [tag, ...classes] = rest.split(".");
    if (tag && tag !== "*" && element.tagName.toLowerCase() !== tag.toLowerCase()) return false;
    if (classes.some((name) => !element.classList.contains(name))) return false;
  }
  for (const body of attributes) {
    const match = body.match(/^([\w-]+)(\*?)=(?:"([^"]*)"|'([^']*)'|([^\]]+))$/);
    if (match == null) {
      if (!element.hasAttribute(body)) return false;
      continue;
    }
    const value = element.getAttribute(match[1]);
    const expected = match[3] ?? match[4] ?? match[5];
    if (match[2] === "*" ? !(value != null && value.includes(expected)) : value !== expected) return false;
  }
  return true;
}

export function matchesSelector(element, selector) {
  return selector.split(",").map((part) => part.trim()).filter(Boolean).some((part) => {
    const tokens = part.split(/\s+/).filter(Boolean);
    let node = element;
    if (!matchCompound(node, tokens.at(-1))) return false;
    node = node.parentElement;
    for (let index = tokens.length - 2; index >= 0; index -= 1) {
      while (node != null && !matchCompound(node, tokens[index])) node = node.parentElement;
      if (node == null) return false;
      node = node.parentElement;
    }
    return true;
  });
}

export class FakeElement {
  constructor(tag, { aria = null, attrs = {}, className = "", text = "", visible = true } = {}) {
    this.attrs = { ...attrs };
    this.aria = aria;
    this.children = [];
    this.className = className;
    this.disabled = false;
    this.events = [];
    this.focused = false;
    this.parentElement = null;
    this.tagName = tag.toUpperCase();
    this.text = text;
    this.value = "";
    this.visible = visible;
    this.clickCount = 0;
    this.onClick = null;
    this.onEvent = null;
  }

  get innerText() { return this.text; }
  set innerText(value) { this.text = value; }
  get textContent() { return this.text; }
  set textContent(value) { this.text = value; }
  get classList() {
    const names = this.className.split(/\s+/).filter(Boolean);
    return { contains: (name) => names.includes(name) };
  }

  getClientRects() { return this.visible ? [{}] : []; }
  getAttribute(name) {
    if (name === "class") return this.className || null;
    if (name === "aria-label") return this.aria ?? this.attrs["aria-label"] ?? null;
    return Object.hasOwn(this.attrs, name) ? this.attrs[name] : null;
  }
  hasAttribute(name) { return this.getAttribute(name) != null; }
  matches(selector) { return matchesSelector(this, selector); }
  closest(selector) {
    let node = this;
    while (node != null) {
      if (matchesSelector(node, selector)) return node;
      node = node.parentElement;
    }
    return null;
  }
  querySelectorAll(selector) {
    return descendants(this).filter((element) => matchesSelector(element, selector));
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] ?? null; }
  append(...children) {
    for (const child of children) {
      child.parentElement = this;
      this.children.push(child);
    }
    return this;
  }
  focus() { this.focused = true; }
  dispatchEvent(event) {
    this.events.push(event);
    this.onEvent?.(event);
    return true;
  }
  click() {
    this.clickCount += 1;
    this.onClick?.(this);
  }
}

export function descendants(element) {
  return element.children.flatMap((child) => [child, ...descendants(child)]);
}

function fakeEvent(type, init = {}) {
  return { type, ...init, defaultPrevented: false };
}

export function createAistudioPage({
  modelId = "gemini-4-argon",
  noApiKey = true,
  paidKey = false,
  unknownApiKey = false,
  pro = true,
  responseText = "",
  runEnabled = true,
  stopVisible = false,
  modalVisible = false,
  url = "https://aistudio.google.com/prompts/new_chat?model=gemini-4-argon",
} = {}) {
  const elements = {};
  const state = {
    now: 0,
    paidKey,
    noApiKey,
    unknownApiKey,
    pro,
    responseText,
    runEnabled,
    stopVisible,
    modalVisible,
    submitClicks: 0,
  };
  const time = {
    advance(ms) { state.now += ms; },
    now() { return state.now; },
  };
  const body = new FakeElement("body");
  const turnsRoot = new FakeElement("div", { className: "chat-turns" });

  elements.composer = new FakeElement("textarea", {
    aria: "Enter a prompt",
    attrs: { formcontrolname: "promptText", placeholder: "Enter a prompt" },
  });
  elements.modelCard = new FakeElement("button", { className: "model-selector-card" });
  elements.modelName = new FakeElement("span", {
    attrs: { "data-test-id": "model-name" },
    text: modelId,
  });
  elements.modelCard.append(elements.modelName);
  elements.account = new FakeElement("button", {
    aria: "Google Account",
    className: "account-switcher-button",
    text: "user@example.com",
  });
  elements.api = new FakeElement("button", {
    aria: "No API key selected",
    className: "paid-api-key-button",
    text: "key_off",
  });
  elements.runLabel = new FakeElement("span", { className: "run-button-label", text: "Run" });
  elements.run = new FakeElement("button", { className: "run-button", text: "keyboard_command_key Run" });
  elements.run.append(elements.runLabel);
  elements.modal = new FakeElement("mat-dialog-container", { visible: false });
  elements.turnsRoot = turnsRoot;

  body.append(
    elements.composer,
    elements.modelCard,
    elements.account,
    elements.api,
    elements.run,
    elements.modal,
    turnsRoot,
  );

  const document = {
    title: "AI Studio",
    body,
    querySelectorAll(selector) {
      return descendants(body).filter((element) => matchesSelector(element, selector));
    },
    querySelector(selector) {
      return this.querySelectorAll(selector)[0] ?? null;
    },
  };

  const page = {
    Date: { now: time.now },
    Event: class Event { constructor(type, init = {}) { return fakeEvent(type, init); } },
    InputEvent: class InputEvent { constructor(type, init = {}) { return fakeEvent(type, init); } },
    URL,
    document,
    elements,
    getComputedStyle: (element) => ({ visibility: element.visible ? "visible" : "hidden" }),
    location: { href: url },
    queueMicrotask,
    setTimeout(callback, ms = 0) {
      time.advance(ms);
      queueMicrotask(callback);
      return 1;
    },
    state,
    time,
    addTurn({ error = null, footer = true, id = null, text = "", thoughts = null } = {}) {
      const turn = new FakeElement("ms-chat-turn", {
        attrs: id == null ? {} : { "data-turn-id": id },
      });
      const container = new FakeElement("div", {
        attrs: { "data-turn-role": "Model" },
        className: "chat-turn-container model",
      });
      if (thoughts != null) {
        const thoughtWrap = new FakeElement("div", { attrs: { "data-turn-role": "Thoughts" } });
        const thoughtChunk = new FakeElement("ms-text-chunk");
        thoughtChunk.append(new FakeElement("ms-cmark-node", { className: "cmark-node", text: thoughts }));
        thoughtWrap.append(thoughtChunk);
        container.append(thoughtWrap);
      }
      if (text) {
        const chunk = new FakeElement("ms-text-chunk");
        const cmark = new FakeElement("ms-cmark-node", { className: "cmark-node", text });
        cmark.append(new FakeElement("ms-cmark-node", { className: "cmark-node", text: "nested duplicate" }));
        chunk.append(cmark);
        container.append(chunk);
      }
      if (footer) container.append(new FakeElement("div", { className: "turn-footer", text: "footer" }));
      if (error != null) container.append(new FakeElement("div", { className: "model-error", text: error }));
      turn.append(container);
      turnsRoot.append(turn);
      state.responseText = text;
      return turn;
    },
    setApiKey(kind) {
      state.noApiKey = kind === "no_api_key";
      state.paidKey = kind === "paid_key";
      state.unknownApiKey = kind === "unknown";
      if (kind === "missing") {
        elements.api.visible = false;
        return;
      }
      elements.api.visible = true;
      if (kind === "no_api_key") {
        elements.api.aria = "No API key selected";
        elements.api.text = "key_off";
      } else if (kind === "paid_key") {
        elements.api.aria = "API key selected";
        elements.api.text = "key_on";
      } else {
        elements.api.aria = "API key status";
        elements.api.text = "key_unknown";
      }
    },
    setModal(value) {
      state.modalVisible = value;
      elements.modal.visible = value;
    },
    setModelId(value) { elements.modelName.text = value; },
    setPro(value) {
      state.pro = value;
      elements.account.text = value ? "PRO\nuser@example.com" : "user@example.com";
    },
    setResponse(value) {
      state.responseText = value;
      const turn = turnsRoot.children.at(-1);
      const chunk = turn?.querySelector("ms-text-chunk");
      const cmark = chunk?.querySelector("ms-cmark-node.cmark-node");
      if (cmark) cmark.text = value;
    },
    setRunEnabled(value) {
      state.runEnabled = value;
      elements.run.disabled = !value;
    },
    setStop(value) {
      state.stopVisible = value;
      elements.runLabel.text = value ? "Stop" : "Run";
    },
  };

  page.run = (fn) => runInNewContext(`(${fn.toString()})`, {
    Date: page.Date,
    Event: page.Event,
    InputEvent: page.InputEvent,
    URL: page.URL,
    document: page.document,
    getComputedStyle: page.getComputedStyle,
    location: page.location,
    queueMicrotask: page.queueMicrotask,
    setTimeout: page.setTimeout,
  });
  page.setApiKey(noApiKey ? "no_api_key" : paidKey ? "paid_key" : unknownApiKey ? "unknown" : "missing");
  page.setPro(pro);
  page.setModal(modalVisible);
  page.setRunEnabled(runEnabled);
  page.setStop(stopVisible);
  return page;
}
