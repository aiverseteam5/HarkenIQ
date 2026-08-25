import { useState } from "react";
import { getToken, setToken } from "./api";
import Approvals from "./pages/Approvals";
import Devices from "./pages/Devices";
import Domains from "./pages/Domains";
import Incidents from "./pages/Incidents";
import Overview from "./pages/Overview";

const PAGES = [
  "Overview",
  "Devices",
  "Incidents",
  "Fault Domains",
  "Approvals",
] as const;
type Page = (typeof PAGES)[number];

function TokenGate({ onSubmit }: { onSubmit: (token: string) => void }) {
  const [value, setValue] = useState("");
  return (
    <div className="token-gate">
      <h1>HarkenIQ Site Manager</h1>
      <p className="muted">Enter the site token to continue.</p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          onSubmit(value.trim());
        }}
      >
        <input
          className="text"
          type="password"
          placeholder="site token"
          value={value}
          onChange={(e) => setValue(e.target.value)}
        />
        <button className="action" type="submit">
          Connect
        </button>
      </form>
    </div>
  );
}

export default function App() {
  const [page, setPage] = useState<Page>("Overview");
  const [hasToken, setHasToken] = useState(getToken() !== "");
  // QA-006: no default identity — the operator must type who they are.
  // This is an ASSERTED name (shared-token dashboard); the server records
  // it as sm-local:<name>. Verified approvals go through the Console.
  const [actor, setActor] = useState(
    sessionStorage.getItem("sm_actor") ?? "",
  );

  if (!hasToken) {
    return (
      <TokenGate
        onSubmit={(token) => {
          setToken(token);
          setHasToken(true);
        }}
      />
    );
  }

  const onAuthFail = () => {
    setToken("");
    setHasToken(false);
  };

  return (
    <div className="app">
      <div className="topbar">
        <h1>HarkenIQ Site Manager</h1>
        <nav>
          {PAGES.map((p) => (
            <button
              key={p}
              className={p === page ? "active" : ""}
              onClick={() => setPage(p)}
            >
              {p}
            </button>
          ))}
        </nav>
        <span style={{ marginLeft: "auto" }} className="muted">
          operator:{" "}
          <input
            className="text"
            style={{ width: "9rem" }}
            value={actor}
            onChange={(e) => {
              setActor(e.target.value);
              sessionStorage.setItem("sm_actor", e.target.value);
            }}
          />
        </span>
      </div>
      {page === "Overview" && <Overview onAuthFail={onAuthFail} />}
      {page === "Devices" && <Devices onAuthFail={onAuthFail} />}
      {page === "Incidents" && <Incidents onAuthFail={onAuthFail} />}
      {page === "Fault Domains" && (
        <Domains actor={actor} onAuthFail={onAuthFail} />
      )}
      {page === "Approvals" && (
        <Approvals actor={actor} onAuthFail={onAuthFail} />
      )}
    </div>
  );
}
