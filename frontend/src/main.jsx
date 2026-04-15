import { Component, StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App.jsx";

class ErrorBoundary extends Component {
  state = { error: null, info: null };

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    this.setState({ info });
    if (typeof console !== "undefined") {
      console.error("[ARIA-OS ErrorBoundary]", error, info?.componentStack);
    }
  }

  reset = () => this.setState({ error: null, info: null });

  render() {
    if (!this.state.error) return this.props.children;
    const stack = this.state.info?.componentStack || "";
    return (
      <div
        style={{
          minHeight: "100vh",
          background: "#0A0A0F",
          color: "#E6E9EF",
          fontFamily: "'Inter', system-ui, sans-serif",
          padding: "48px 24px",
          display: "flex",
          alignItems: "flex-start",
          justifyContent: "center",
        }}
      >
        <div
          style={{
            maxWidth: "720px",
            width: "100%",
            background: "linear-gradient(180deg, #16161C 0%, #0F0F14 100%)",
            border: "1px solid #2A2A33",
            borderRadius: "14px",
            padding: "32px",
            boxShadow: "0 8px 32px rgba(0,0,0,0.5)",
          }}
        >
          <div
            style={{
              fontSize: "11px",
              letterSpacing: "0.18em",
              color: "#FF7A1A",
              fontWeight: 700,
              marginBottom: "8px",
            }}
          >
            UNHANDLED EXCEPTION
          </div>
          <div style={{ fontSize: "20px", fontWeight: 700, marginBottom: "12px" }}>
            ARIA-OS dashboard hit an error
          </div>
          <div
            style={{
              fontFamily: "'JetBrains Mono', monospace",
              fontSize: "12px",
              color: "#FF6B6B",
              background: "rgba(255,107,107,0.08)",
              border: "1px solid rgba(255,107,107,0.25)",
              borderRadius: "8px",
              padding: "12px 14px",
              marginBottom: "16px",
              wordBreak: "break-word",
            }}
          >
            {String(this.state.error)}
          </div>
          {stack && (
            <details style={{ marginBottom: "16px" }}>
              <summary
                style={{
                  cursor: "pointer",
                  fontSize: "11px",
                  color: "#9CA3B0",
                  marginBottom: "8px",
                }}
              >
                Component stack
              </summary>
              <pre
                style={{
                  fontFamily: "'JetBrains Mono', monospace",
                  fontSize: "10px",
                  color: "#9CA3B0",
                  background: "rgba(0,0,0,0.3)",
                  border: "1px solid #1F1F26",
                  borderRadius: "6px",
                  padding: "10px 12px",
                  overflow: "auto",
                  maxHeight: "240px",
                  whiteSpace: "pre-wrap",
                }}
              >
                {stack}
              </pre>
            </details>
          )}
          <div style={{ display: "flex", gap: "8px" }}>
            <button
              onClick={this.reset}
              style={{
                padding: "10px 18px",
                borderRadius: "8px",
                border: "none",
                background: "linear-gradient(135deg, #00D4FF, #FF7A1A)",
                color: "#fff",
                fontSize: "12px",
                fontWeight: 700,
                cursor: "pointer",
                letterSpacing: "0.04em",
              }}
            >
              TRY AGAIN
            </button>
            <button
              onClick={() => window.location.reload()}
              style={{
                padding: "10px 18px",
                borderRadius: "8px",
                border: "1px solid #2A2A33",
                background: "transparent",
                color: "#9CA3B0",
                fontSize: "12px",
                fontWeight: 600,
                cursor: "pointer",
                letterSpacing: "0.04em",
              }}
            >
              HARD RELOAD
            </button>
          </div>
        </div>
      </div>
    );
  }
}

createRoot(document.getElementById("root")).render(
  <StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </StrictMode>
);
