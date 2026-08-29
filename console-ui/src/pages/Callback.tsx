import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { exchangeCode } from "../auth";
import { useAuth } from "../useAuth";
import Spinner from "../components/Spinner";

const KEYCLOAK_URL = import.meta.env.VITE_KEYCLOAK_URL ?? "";
const KEYCLOAK_REALM = import.meta.env.VITE_KEYCLOAK_REALM ?? "harkeniq";
const CLIENT_ID = import.meta.env.VITE_OIDC_CLIENT_ID ?? "harkeniq-console";

export default function Callback() {
  const navigate = useNavigate();
  const { completeLogin } = useAuth();
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const code = params.get("code");
    const errorParam = params.get("error");

    if (errorParam) {
      setError(`Authentication error: ${errorParam}`);
      return;
    }

    if (!code) {
      setError("No authorization code received.");
      return;
    }

    const verifier = sessionStorage.getItem("hiq_code_verifier");
    if (!verifier) {
      setError("Missing code verifier. Please try signing in again.");
      return;
    }

    const redirectUri = `${window.location.origin}/callback`;

    // D1 fix (P0 2026-08-29): the exchange used to set the raw token and
    // navigate WITHOUT ever telling AuthProvider — RequireAuth still saw
    // an unauthenticated session and parked the user on /login until a
    // hard reload. completeLogin resolves the user first; only then move.
    exchangeCode(KEYCLOAK_URL, KEYCLOAK_REALM, CLIENT_ID, redirectUri, code, verifier)
      .then(async (tokens) => {
        await completeLogin(tokens);
        sessionStorage.removeItem("hiq_code_verifier");
        navigate("/dashboard", { replace: true });
      })
      .catch((err) => {
        setError(err instanceof Error ? err.message : "Token exchange failed.");
      });
  }, [navigate, completeLogin]);

  if (error) {
    return (
      <div style={{ textAlign: "center" }}>
        <div style={{ color: "var(--status-critical)", marginBottom: "1rem", fontSize: "0.875rem" }}>
          {error}
        </div>
        <a href="/login" className="btn btn-primary" style={{ textDecoration: "none" }}>
          Try again
        </a>
      </div>
    );
  }

  return (
    <div style={{ textAlign: "center", padding: "1rem 0" }}>
      <Spinner size="lg" />
      <div style={{ marginTop: "1rem", color: "var(--text-secondary)", fontSize: "0.875rem" }}>
        Completing sign-in...
      </div>
    </div>
  );
}
