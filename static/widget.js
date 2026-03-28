/**
 * ChatSaaS Embeddable Widget
 *
 * Usage (add to any website):
 * <script src="https://cdn.chatsaas.io/widget.js" data-key="sk_live_XXXX"></script>
 *
 * Optional attributes:
 *   data-key          (required)  API key
 *   data-position     "right" | "left"  (default: "right")
 *   data-api-url      Override API URL (for self-hosted)
 */

(function () {
  'use strict';

  // ── Guard: only init once ──────────────────────────────────
  if (window.__ChatSaaSLoaded) return;
  window.__ChatSaaSLoaded = true;

  // ── Config from script tag ─────────────────────────────────
  const scriptTag = document.currentScript ||
    document.querySelector('script[data-key]');

  const API_KEY    = scriptTag?.getAttribute('data-key') || '';
  const POSITION   = scriptTag?.getAttribute('data-position') || 'right';
  const API_URL    = scriptTag?.getAttribute('data-api-url') || '';

  if (!API_KEY) {
    console.warn('[ChatSaaS] No data-key found on widget script tag.');
    return;
  }

  // ── State ──────────────────────────────────────────────────
  let isOpen          = false;
  let conversationId  = null;
  let config          = { botName: 'Assistant', primaryColor: '#6366f1', welcomeMessage: 'Hi! How can I help?' };
  let visitorId       = getOrCreateVisitorId();
  let isTyping        = false;

  // ── Styles ─────────────────────────────────────────────────
  const css = `
    #cs-widget-btn {
      position: fixed; ${POSITION}: 24px; bottom: 24px;
      width: 64px; height: 64px; border-radius: 50%;
      background: var(--cs-color); border: none; cursor: pointer;
      box-shadow: 0 4px 20px rgba(0,0,0,0.25);
      display: flex; align-items: center; justify-content: center;
      z-index: 9998; transition: transform 0.2s, box-shadow 0.2s;
      font-size: 36px !important; line-height: 1 !important;
    }
    #cs-widget-btn:hover { transform: scale(1.08); box-shadow: 0 6px 24px rgba(0,0,0,0.3); }
    #cs-widget-btn svg { width: 28px; height: 28px; fill: white; }
    #cs-widget-btn img { width: 44px !important; height: 44px !important; max-width: 44px !important; min-width: 44px !important; display: block !important; }

    #cs-widget {
      position: fixed; ${POSITION}: 24px; bottom: 24px;
      width: 360px; height: 520px;
      background: #fff; border-radius: 16px;
      box-shadow: 0 8px 40px rgba(0,0,0,0.18);
      display: flex; flex-direction: column;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      font-size: 14px; z-index: 9999;
      transform: scale(0.92) translateY(12px); opacity: 0;
      pointer-events: none;
      transition: transform 0.22s cubic-bezier(0.34,1.56,0.64,1), opacity 0.2s;
    }
    #cs-widget.cs-open {
      transform: scale(1) translateY(0); opacity: 1; pointer-events: all;
    }
    #cs-header {
      padding: 16px 20px; background: var(--cs-color); border-radius: 16px 16px 0 0;
      display: flex; align-items: center; justify-content: space-between; color: white;
    }
    #cs-header .cs-title { font-weight: 600; font-size: 15px; }
    #cs-header .cs-close {
      background: none; border: none; color: rgba(255,255,255,0.8);
      font-size: 22px; cursor: pointer; padding: 0; line-height: 1;
    }
    #cs-header .cs-close:hover { color: white; }
    #cs-messages {
      flex: 1; overflow-y: auto; padding: 16px;
      display: flex; flex-direction: column; gap: 12px;
      scroll-behavior: smooth;
    }
    #cs-messages::-webkit-scrollbar { width: 4px; }
    #cs-messages::-webkit-scrollbar-thumb { background: #ddd; border-radius: 4px; }

    .cs-msg {
      max-width: 82%; padding: 10px 14px; border-radius: 14px;
      line-height: 1.5; word-wrap: break-word;
      animation: cs-fade-in 0.15s ease;
    }
    .cs-msg.cs-user {
      background: var(--cs-color); color: white;
      margin-left: auto; border-bottom-right-radius: 4px;
    }
    .cs-msg.cs-bot {
      background: #f1f3f5; color: #1a1a2e;
      margin-right: auto; border-bottom-left-radius: 4px;
    }
    .cs-msg.cs-typing span {
      display: inline-block; width: 7px; height: 7px; margin: 0 2px;
      background: #9ca3af; border-radius: 50%;
      animation: cs-bounce 1s infinite;
    }
    .cs-msg.cs-typing span:nth-child(2) { animation-delay: 0.2s; }
    .cs-msg.cs-typing span:nth-child(3) { animation-delay: 0.4s; }

    @keyframes cs-bounce {
      0%, 80%, 100% { transform: translateY(0); }
      40% { transform: translateY(-6px); }
    }
    @keyframes cs-fade-in {
      from { opacity: 0; transform: translateY(6px); }
      to   { opacity: 1; transform: translateY(0); }
    }

    .cs-feedback {
      display: flex; gap: 6px; margin-top: 6px;
    }
    .cs-feedback button {
      background: none; border: 1px solid #e5e7eb; border-radius: 6px;
      padding: 2px 8px; cursor: pointer; font-size: 13px; color: #6b7280;
    }
    .cs-feedback button:hover { background: #f9fafb; }

    #cs-footer {
      padding: 12px 16px; border-top: 1px solid #f1f3f5;
      background: white; border-radius: 0 0 16px 16px;
    }
    #cs-input-row {
      display: flex; gap: 8px; align-items: flex-end;
    }
    #cs-input {
      flex: 1; border: 1.5px solid #e5e7eb; border-radius: 10px;
      padding: 9px 12px; resize: none; outline: none;
      font-family: inherit; font-size: 14px; line-height: 1.4;
      max-height: 100px; transition: border-color 0.15s;
    }
    #cs-input:focus { border-color: var(--cs-color); }
    #cs-send {
      width: 38px; height: 38px; border-radius: 10px; border: none;
      background: var(--cs-color); cursor: pointer;
      display: flex; align-items: center; justify-content: center;
      transition: opacity 0.15s; flex-shrink: 0;
    }
    #cs-send:disabled { opacity: 0.5; cursor: not-allowed; }
    #cs-send svg { width: 17px; height: 17px; fill: white; }
    #cs-powered {
      text-align: center; font-size: 11px; color: #9ca3af;
      padding: 6px 0 0; letter-spacing: 0.2px;
    }
    #cs-powered a { color: #9ca3af; text-decoration: none; }

    @media (max-width: 440px) {
      #cs-widget { width: calc(100vw - 32px); ${POSITION}: 16px; bottom: 84px; }
    }
  `;

  // ── Build DOM ──────────────────────────────────────────────
  function buildWidget() {
    // CSS variable for brand color
    document.documentElement.style.setProperty('--cs-color', config.primaryColor);

    // Style tag
    const style = document.createElement('style');
    style.textContent = css;
    document.head.appendChild(style);

    // Launcher button
    const btn = document.createElement('button');
    btn.id = 'cs-widget-btn';
    btn.setAttribute('aria-label', `Open ${config.botName}`);
    btn.innerHTML = chatIcon();
    btn.addEventListener('click', toggleWidget);
    document.body.appendChild(btn);

    // Chat window
    const widget = document.createElement('div');
    widget.id = 'cs-widget';
    widget.setAttribute('role', 'dialog');
    widget.setAttribute('aria-label', config.botName);
    widget.innerHTML = `
      <div id="cs-header">
        <div class="cs-title">${esc(config.botName)}</div>
        <button class="cs-close" aria-label="Close chat">&#x2715;</button>
      </div>
      <div id="cs-messages" role="log" aria-live="polite"></div>
      <div id="cs-footer">
        <div id="cs-input-row">
          <textarea id="cs-input" rows="1" placeholder="Napisz wiadomość…" aria-label="Wiadomość"></textarea>
          <button id="cs-send" aria-label="Send">${sendIcon()}</button>
        </div>
        <div id="cs-powered">Powered by <a href="https://chatsaas.io" target="_blank" rel="noopener">ChatSaaS</a></div>
      </div>
    `;
    document.body.appendChild(widget);

    // Close button
    widget.querySelector('.cs-close').addEventListener('click', closeWidget);

    // Send handlers
    const input = widget.querySelector('#cs-input');
    const sendBtn = widget.querySelector('#cs-send');

    sendBtn.addEventListener('click', sendMessage);
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
    });
    input.addEventListener('input', () => {
      input.style.height = 'auto';
      input.style.height = Math.min(input.scrollHeight, 100) + 'px';
    });

    // Welcome message
    appendBotMessage(config.welcomeMessage);
  }

  // ── Toggle ─────────────────────────────────────────────────
  function toggleWidget() {
    isOpen ? closeWidget() : openWidget();
  }

  function openWidget() {
    isOpen = true;
    document.getElementById('cs-widget').classList.add('cs-open');
    document.getElementById('cs-widget-btn').style.display = 'none';
    setTimeout(() => document.getElementById('cs-input')?.focus(), 250);
  }

  function closeWidget() {
    isOpen = false;
    document.getElementById('cs-widget').classList.remove('cs-open');
    document.getElementById('cs-widget-btn').style.display = 'flex';
  }

  // ── Messaging ──────────────────────────────────────────────
  function sendMessage() {
    const input = document.getElementById('cs-input');
    const text = input.value.trim();
    if (!text || isTyping) return;

    input.value = '';
    input.style.height = 'auto';

    appendUserMessage(text);
    streamBotResponse(text);
  }

  function appendUserMessage(text) {
    const div = document.createElement('div');
    div.className = 'cs-msg cs-user';
    div.textContent = text;
    getMessages().appendChild(div);
    scrollToBottom();
  }

  function appendBotMessage(text, msgId) {
    const wrap = document.createElement('div');
    wrap.style.display = 'flex';
    wrap.style.flexDirection = 'column';
    wrap.style.alignItems = 'flex-start';

    const div = document.createElement('div');
    div.className = 'cs-msg cs-bot';
    div.innerHTML = linkify(esc(text));
    wrap.appendChild(div);

    if (msgId) {
      const fb = buildFeedbackButtons(msgId);
      wrap.appendChild(fb);
    }

    getMessages().appendChild(wrap);
    scrollToBottom();
    return div;
  }

  function showTypingIndicator() {
    const div = document.createElement('div');
    div.className = 'cs-msg cs-bot cs-typing';
    div.id = 'cs-typing';
    div.innerHTML = '<span></span><span></span><span></span>';
    getMessages().appendChild(div);
    scrollToBottom();
  }

  function hideTypingIndicator() {
    document.getElementById('cs-typing')?.remove();
  }

  // ── SSE Streaming ──────────────────────────────────────────
  async function streamBotResponse(userMessage) {
    isTyping = true;
    document.getElementById('cs-send').disabled = true;
    showTypingIndicator();

    try {
      const response = await fetch(`${API_URL}/chatbot/api/message`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'x-api-key': API_KEY,
        },
        body: JSON.stringify({
          message: userMessage,
          conversationId,
          visitorId,
          pageUrl: window.location.href,
        }),
      });

      if (!response.ok) {
        hideTypingIndicator();
        appendBotMessage('Sorry, something went wrong. Please try again.');
        return;
      }

      hideTypingIndicator();

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let fullText = '';
      let finalMsgId = null;
      let botDiv = null;

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop();

        for (const line of lines) {
          if (!line.startsWith('data:')) continue;
          try {
            const payload = JSON.parse(line.slice(5).trim());

            if (payload.conversationId) conversationId = payload.conversationId;
            if (payload.error) {
              if (!botDiv) botDiv = createStreamBubble();
              botDiv.innerHTML = esc(payload.error);
              scrollToBottom();
            }
            if (payload.text) {
              if (!botDiv) botDiv = createStreamBubble();
              fullText += payload.text;
              botDiv.innerHTML = linkify(esc(fullText)) + '<span class="cs-cursor">▍</span>';
              scrollToBottom();
            }
            if (payload.messageId) finalMsgId = payload.messageId;
          } catch {}
        }
      }

      // Clean up cursor, add feedback buttons
      botDiv.innerHTML = linkify(esc(fullText));
      if (finalMsgId) {
        const fb = buildFeedbackButtons(finalMsgId);
        botDiv.parentElement?.appendChild(fb);
      }

    } catch (err) {
      hideTypingIndicator();
      appendBotMessage('Connection error. Please check your internet and try again.');
    } finally {
      isTyping = false;
      document.getElementById('cs-send').disabled = false;
      scrollToBottom();
    }
  }

  function createStreamBubble() {
    const wrap = document.createElement('div');
    wrap.style.display = 'flex';
    wrap.style.flexDirection = 'column';
    wrap.style.alignItems = 'flex-start';

    const div = document.createElement('div');
    div.className = 'cs-msg cs-bot';
    wrap.appendChild(div);
    getMessages().appendChild(wrap);
    return div;
  }

  function buildFeedbackButtons(msgId) {
    const fb = document.createElement('div');
    fb.className = 'cs-feedback';
    fb.innerHTML = `
      <button data-v="thumbs_up" title="Helpful">👍</button>
      <button data-v="thumbs_down" title="Not helpful">👎</button>
    `;
    fb.querySelectorAll('button').forEach((btn) => {
      btn.addEventListener('click', () => {
        sendFeedback(msgId, btn.dataset.v);
        fb.innerHTML = '<span style="font-size:12px;color:#9ca3af">Thanks for your feedback!</span>';
      });
    });
    return fb;
  }

  async function sendFeedback(messageId, feedback) {
    try {
      await fetch(`${API_URL}/chatbot/api/feedback`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'x-api-key': API_KEY },
        body: JSON.stringify({ messageId, feedback }),
      });
    } catch {}
  }

  // ── Utilities ──────────────────────────────────────────────
  function getMessages() { return document.getElementById('cs-messages'); }
  function scrollToBottom() {
    const m = getMessages();
    if (m) m.scrollTop = m.scrollHeight;
  }

  function esc(str) {
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function linkify(text) {
    return text.replace(
      /(https?:\/\/[^\s<>"]+)/g,
      '<a href="$1" target="_blank" rel="noopener" style="color:var(--cs-color)">$1</a>'
    );
  }

  function getOrCreateVisitorId() {
    const KEY = '_cs_vid';
    let id = localStorage.getItem(KEY);
    if (!id) {
      id = 'v_' + Math.random().toString(36).slice(2) + Date.now().toString(36);
      localStorage.setItem(KEY, id);
    }
    return id;
  }

  // ── SVG Icons ──────────────────────────────────────────────
  function chatIcon() {
    const svg = encodeURIComponent(`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
      <rect x="18" y="30" width="64" height="48" rx="12" fill="white"/>
      <rect x="40" y="10" width="20" height="20" rx="10" fill="white"/>
      <circle cx="50" cy="8" r="7" fill="white"/>
      <circle cx="33" cy="50" r="9" fill="rgba(0,0,0,0.25)"/>
      <circle cx="67" cy="50" r="9" fill="rgba(0,0,0,0.25)"/>
      <circle cx="33" cy="50" r="4" fill="white"/>
      <circle cx="67" cy="50" r="4" fill="white"/>
      <rect x="30" y="64" width="40" height="7" rx="3.5" fill="rgba(0,0,0,0.25)"/>
      <rect x="4" y="40" width="14" height="24" rx="7" fill="white"/>
      <rect x="82" y="40" width="14" height="24" rx="7" fill="white"/>
    </svg>`);
    return `<img src="data:image/svg+xml,${svg}" width="44" height="44">`;
  }
  function closeIcon() {
    return `<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
      <path d="M19 6.4L17.6 5 12 10.6 6.4 5 5 6.4 10.6 12 5 17.6 6.4 19 12 13.4 17.6 19 19 17.6 13.4 12z"/>
    </svg>`;
  }
  function sendIcon() {
    return `<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
      <path d="M5 12h14M13 6l6 6-6 6" stroke="white" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" fill="none"/>
    </svg>`;
  }

  // ── Init: fetch config then build ──────────────────────────
  async function init() {
    try {
      const res = await fetch(`${API_URL}/chatbot/api/config`, {
        headers: { 'x-api-key': API_KEY },
      });
      if (res.ok) {
        const data = await res.json();
        config = { ...config, ...data };
        document.documentElement.style.setProperty('--cs-color', config.primaryColor);
      }
    } catch {}

    // Build widget after DOM is ready
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', buildWidget);
    } else {
      buildWidget();
    }
  }

  init();

  // Expose API for programmatic control
  window.ChatSaaS = { open: openWidget, close: closeWidget, toggle: toggleWidget };

})();
