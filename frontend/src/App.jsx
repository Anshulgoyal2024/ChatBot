import { useEffect, useRef, useState } from "react";

// All browser API traffic stays on this local origin; Vite/Nginx proxy it to FastAPI.

const SUGGESTIONS = [
  "Help me plan a focused workday",
  "Explain a tricky idea in simple terms",
  "Give me a thoughtful first draft",
];

function newSessionId() {
  const bytes = new Uint8Array(8);
  if (window.crypto && window.crypto.getRandomValues) {
    window.crypto.getRandomValues(bytes);
  } else {
    for (let i = 0; i < bytes.length; i += 1) bytes[i] = Math.floor(Math.random() * 256);
  }
  return "s-" + Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function describeValidationDetail(detail) {
  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        const field = Array.isArray(item.loc) ? item.loc.filter((part) => part !== "body").join(".") : "";
        return field ? `${field}: ${item.msg}` : item.msg;
      })
      .join("; ");
  }
  return typeof detail === "string" ? detail : "The request was not valid.";
}

async function readError(response) {
  let detail = null;
  try {
    const body = await response.json();
    detail = body && body.detail;
  } catch {
    detail = null;
  }
  const providerFailures = response.headers.get("X-Provider-Failures");
  const providerSummary = providerFailures
    ? providerFailures.split("; ").map((failure, index) => `API ${index + 1} (${failure})`).join(" · ")
    : null;
  if (response.status === 422) {
    return `Your message could not be sent. ${describeValidationDetail(detail)}`;
  }
  if (response.status === 429) {
    return `No AI answer was returned because the provider rate or usage limit was reached.${providerSummary ? ` ${providerSummary}.` : " Check API usage, billing, and limits before retrying."}`;
  }
  if (response.status === 503) {
    return `No AI answer was returned. Your message is still shown above.${providerSummary ? ` Provider results: ${providerSummary}.` : " Check the backend and retry."}`;
  }
  if (typeof detail === "string" && detail) {
    return `The server returned an error (${response.status}): ${detail}`;
  }
  return `The server returned an unexpected error (${response.status}).`;
}

function StatusDot({ status }) {
  const label = status === "online"
    ? "Backend online"
    : status === "setup"
      ? "API key needed"
      : status === "checking"
        ? "Connecting"
        : "Offline";
  return (
    <span className={`status-pill ${status}`} aria-live="polite">
      <span className="status-dot" />
      {label}
    </span>
  );
}

export default function App() {
  const [sessionId, setSessionId] = useState(newSessionId);
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [systemPrompt, setSystemPrompt] = useState("");
  const [simulateFailure, setSimulateFailure] = useState("");
  const [pending, setPending] = useState(false);
  const [apiStatus, setApiStatus] = useState("checking");
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const counter = useRef(0);
  const bottomRef = useRef(null);
  const inputRef = useRef(null);
  const activeSessionRef = useRef(sessionId);

  useEffect(() => {
    const controller = new AbortController();
    fetch("/health", { signal: controller.signal })
      .then((response) => {
        if (!response.ok) throw new Error("Backend health check failed");
        return response.json();
      })
      .then((health) => {
        if (health.status !== "ok") setApiStatus("offline");
        else setApiStatus(health.keys_configured > 0 ? "online" : "setup");
      })
      .catch(() => {
        if (!controller.signal.aborted) setApiStatus("offline");
      });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    bottomRef.current?.scrollIntoView?.({ behavior: "smooth", block: "end" });
  }, [messages, pending]);

  const nextId = () => {
    counter.current += 1;
    return counter.current;
  };

  function startNewSession() {
    const nextSession = newSessionId();
    activeSessionRef.current = nextSession;
    setSessionId(nextSession);
    setMessages([]);
    setInput("");
    setSystemPrompt("");
    setSimulateFailure("");
    setPending(false);
    setSidebarOpen(false);
    inputRef.current?.focus();
  }

  async function send(textOverride = null, retryId = null) {
    const text = (typeof textOverride === "string" ? textOverride : input).trim();
    if (!text || pending) return;

    const requestSessionId = sessionId;
    const isCurrentSession = () => activeSessionRef.current === requestSessionId;
    const userId = retryId ?? nextId();
    const failureHeader = simulateFailure.trim();
    setInput("");
    setSimulateFailure(""); // The simulation applies to this request only.
    if (retryId) {
      setMessages((previous) =>
        previous
          .filter((message) => !(message.role === "error" && message.retryFor === retryId))
          .map((message) => (message.id === retryId ? { ...message, failed: false } : message)),
      );
    } else {
      setMessages((previous) => [...previous, { id: userId, role: "user", content: text }]);
    }
    setPending(true);

    const headers = { "Content-Type": "application/json" };
    if (failureHeader) headers["X-Simulate-Failure"] = failureHeader;
    const body = { session_id: requestSessionId, message: text };
    if (systemPrompt.trim()) body.system_prompt = systemPrompt.trim();

    try {
      const response = await fetch("/chat", {
        method: "POST",
        headers,
        body: JSON.stringify(body),
      });
      if (!response.ok) {
        const errorText = await readError(response);
        if (!isCurrentSession()) return;
        setMessages((previous) => [
          ...previous.map((message) => (message.id === userId ? { ...message, failed: true } : message)),
          { id: nextId(), role: "error", content: errorText, retryFor: userId },
        ]);
      } else {
        const data = await response.json();
        if (!isCurrentSession()) return;
        setApiStatus("online");
        setMessages((previous) => [
          ...previous,
          { id: nextId(), role: "assistant", content: data.reply, metadata: data.metadata },
        ]);
      }
    } catch {
      if (!isCurrentSession()) return;
      setApiStatus("offline");
      setMessages((previous) => [
        ...previous.map((message) => (message.id === userId ? { ...message, failed: true } : message)),
        {
          id: nextId(),
          role: "error",
          content: "Could not reach the local backend at 127.0.0.1:8000. Start the backend in VS Code, then retry.",
          retryFor: userId,
        },
      ]);
    } finally {
      if (isCurrentSession()) setPending(false);
    }
  }

  function onKeyDown(event) {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      send();
    }
  }

  function retryMessage(messageId) {
    const failedMessage = messages.find((message) => message.id === messageId);
    if (failedMessage?.role === "user") send(failedMessage.content, messageId);
  }

  return (
    <div className="app-shell">
      {sidebarOpen && <button className="sidebar-scrim" aria-label="Close menu" onClick={() => setSidebarOpen(false)} />}

      <aside className={`sidebar${sidebarOpen ? " sidebar-open" : ""}`}>
        <div className="brand-lockup">
          <span className="brand-symbol" aria-hidden="true"><span /><span /><span /></span>
          <span className="brand-name">lyftr<span className="brand-period">.</span></span>
        </div>
        <p className="brand-caption">A calmer space to think.</p>

        <button className="new-chat-button" type="button" onClick={startNewSession}>
          <span className="plus-mark" aria-hidden="true">+</span>
          New conversation
        </button>

        <div className="sidebar-divider" />
        <div className="sidebar-section-label">WORKSPACE</div>
        <div className="workspace-entry">
          <span className="workspace-icon" aria-hidden="true">✦</span>
          <span>AI conversation</span>
          <span className="active-indicator" />
        </div>

        <div className="sidebar-settings">
          <details className="settings-disclosure">
            <summary><span className="setting-icon">✳</span><span>Assistant style</span><span className="summary-chevron">⌄</span></summary>
            <div className="disclosure-content">
              <label htmlFor="system-prompt-input">Custom instructions <span>OPTIONAL</span></label>
              <textarea
                id="system-prompt-input"
                data-testid="system-prompt-input"
                value={systemPrompt}
                onChange={(event) => setSystemPrompt(event.target.value)}
                placeholder="Tell the assistant how you'd like it to respond…"
                rows={4}
                maxLength={8000}
              />
              <p className="field-footnote">Applies to this conversation and stays private.</p>
            </div>
          </details>

          <details className="settings-disclosure">
            <summary><span className="setting-icon">⇄</span><span>Failover demo</span><span className="summary-chevron">⌄</span></summary>
            <div className="disclosure-content">
              <label htmlFor="simulate-failure-input">Simulate a key failure <span>DEMO</span></label>
              <input
                id="simulate-failure-input"
                data-testid="simulate-failure-input"
                type="text"
                value={simulateFailure}
                onChange={(event) => setSimulateFailure(event.target.value)}
                placeholder="key_1=429 or *=429"
                autoComplete="off"
                spellCheck={false}
              />
              <p className="field-footnote">Applies once to your next message. Leave blank for a normal reply.</p>
            </div>
          </details>
        </div>

        <div className="sidebar-bottom">
          <div className="connection-row"><span>Service status</span><StatusDot status={apiStatus} /></div>
          <div className="session-card">
            <span className="session-label">CURRENT SESSION</span>
            <code title={sessionId}>{sessionId}</code>
            <span className="session-note">A fresh session starts each time you open the app.</span>
          </div>
          <div className="sidebar-footnote"><span className="privacy-mark">◈</span> Built for thoughtful conversations</div>
        </div>
      </aside>

      <main className="main-panel">
        <header className="topbar">
          <button className="mobile-menu-button" type="button" onClick={() => setSidebarOpen(true)} aria-label="Open menu">☰</button>
          <div className="topbar-context">
            <span className="context-kicker">YOUR WORKSPACE</span>
            <span className="context-title">AI conversation</span>
          </div>
          <div className="topbar-actions">
            <StatusDot status={apiStatus} />
            <span className="topbar-divider" />
            <button className="topbar-new-chat" type="button" onClick={startNewSession}>
              <span aria-hidden="true">＋</span> New chat
            </button>
          </div>
        </header>

        <section className="conversation" role="log" aria-live="polite" aria-label="Conversation">
          {messages.length === 0 && !pending && (
            <div className="welcome-state">
              <div className="welcome-orbit" aria-hidden="true"><span>✦</span></div>
              <p className="welcome-eyebrow">A LITTLE SPACE TO THINK</p>
              <h1>What’s on your mind?</h1>
              <p className="welcome-copy">Ask a question, untangle an idea, or start with one of these.</p>
              <div className="suggestion-grid">
                {SUGGESTIONS.map((suggestion, index) => (
                  <button key={suggestion} className="suggestion-card" type="button" onClick={() => { setInput(suggestion); inputRef.current?.focus(); }}>
                    <span className={`suggestion-icon suggestion-${index + 1}`} aria-hidden="true">{["◷", "✧", "↗"][index]}</span>
                    <span>{suggestion}</span>
                    <span className="suggestion-arrow" aria-hidden="true">↗</span>
                  </button>
                ))}
              </div>
              <div className="welcome-hint"><span>↵</span> Enter to send <i /> <span>⇧ ↵</span> for a new line</div>
            </div>
          )}

          {messages.map((message) => {
            if (message.role === "user") {
              return (
                <div key={message.id} className="message-row user-row">
                  <div className={`user-message${message.failed ? " user-message-failed" : ""}`}>
                    <p>{message.content}</p>
                    {message.failed && <button type="button" className="retry-inline" onClick={() => retryMessage(message.id)} disabled={pending}>↻ Retry</button>}
                  </div>
                </div>
              );
            }
            if (message.role === "error") {
              return (
                <div key={message.id} className="message-row assistant-row">
                  <div className="assistant-avatar error-avatar" aria-hidden="true">!</div>
                  <div className="error-message" role="alert" data-testid="error-message"><p>{message.content}</p></div>
                </div>
              );
            }

            const metadata = message.metadata || {};
            const cached = metadata.resolution_source === "LLM_CACHE";
            return (
              <div key={message.id} className="message-row assistant-row">
                <div className="assistant-avatar" aria-hidden="true"><span>✦</span></div>
                <article className="assistant-message" data-testid="assistant-message">
                  <div className="assistant-card-topline"><span>LYFTR ASSISTANT</span><span className="reply-status">● Responded</span></div>
                  <p className="reply-text">{message.content}</p>
                  <div className="metadata-strip" aria-label="Reply details">
                    <div className={`metadata-source ${cached ? "cache" : "live"}`}>
                      <span className="metadata-label">SOURCE</span>
                      <span data-testid="meta-source">{metadata.resolution_source}</span>
                    </div>
                    <div className="metadata-item model-item"><span className="metadata-label">MODEL</span><span data-testid="meta-model">{metadata.model}</span></div>
                    <div className="metadata-item"><span className="metadata-label">TIME</span><span data-testid="meta-latency">{metadata.latency_ms} ms</span></div>
                    <div className="metadata-item"><span className="metadata-label">TOKENS IN</span><span data-testid="meta-input-tokens">{metadata.input_tokens}</span></div>
                    <div className="metadata-item"><span className="metadata-label">TOKENS OUT</span><span data-testid="meta-output-tokens">{metadata.output_tokens}</span></div>
                    <div className="metadata-item"><span className="metadata-label">KEY</span><span data-testid="meta-key">{metadata.key_id || "none"}</span></div>
                    <div className="metadata-item"><span className="metadata-label">FALLBACKS</span><span data-testid="meta-fallbacks">{metadata.fallback_attempts}</span></div>
                  </div>
                </article>
              </div>
            );
          })}

          {pending && (
            <div className="message-row assistant-row" aria-label="Waiting for the assistant">
              <div className="assistant-avatar" aria-hidden="true"><span>✦</span></div>
              <div className="typing-card"><span /><span /><span /><em>Thinking through it…</em></div>
            </div>
          )}
          <div ref={bottomRef} />
        </section>

        <form className="composer-area" onSubmit={(event) => { event.preventDefault(); send(); }}>
          <div className="composer-box">
            <textarea
              ref={inputRef}
              data-testid="chat-input"
              value={input}
              onChange={(event) => setInput(event.target.value)}
              onKeyDown={onKeyDown}
              placeholder="Write your message…"
              rows={1}
              maxLength={4000}
              aria-label="Message"
            />
            <div className="composer-toolbar">
              <span className="composer-note"><span className="composer-sparkle">✦</span> Responses can make mistakes. Check important details.</span>
              <div className="composer-actions">
                <span className="character-count">{input.length}/4000</span>
                <button className="send-button" type="submit" data-testid="send-button" disabled={pending || !input.trim()} aria-label="Send message">
                  {pending ? <span className="send-spinner" /> : <span aria-hidden="true">↑</span>}
                  <span className="send-label">{pending ? "Sending" : "Send"}</span>
                </button>
              </div>
            </div>
          </div>
          <p className="composer-shortcut">Press <kbd>Enter</kbd> to send <span>·</span> <kbd>Shift + Enter</kbd> for a new line</p>
        </form>
      </main>
    </div>
  );
}
