import { useState, type FormEvent } from "react";
import { api } from "../lib/api";

export function Login({ onSignedIn }: { onSignedIn: () => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError("");
    setBusy(true);
    try {
      await api.login(username, password);
      onSignedIn();
    } catch (err) {
      // The backend hashes with scrypt whether or not the username matches, so
      // it cannot say which half was wrong - and neither should we.
      setError(err instanceof Error ? err.message : "Sign in failed.");
      setBusy(false);
    }
  }

  return (
    <div className="login">
      <main className="card">
        <img
          className="wordmark"
          src="/title.png"
          alt="The iScale"
          width={216}
          height={44}
        />
        <p className="sub">Sign in to view and take over conversations.</p>

        <form onSubmit={submit} noValidate>
          <div className="field">
            <label htmlFor="u">Username</label>
            <input
              id="u"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoComplete="username"
              placeholder="iScale-user"
              autoFocus
              required
            />
          </div>
          <div className="field">
            <label htmlFor="p">Password</label>
            <input
              id="p"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password"
              placeholder="••••••••••"
              required
            />
          </div>

          {/* Hashing is deliberately slow server-side, so the spinner is doing
              real work rather than decoration. */}
          <button className="primary" type="submit" disabled={busy}>
            {busy && <span className="spin" />}
            {busy ? "Signing in" : "Sign in"}
          </button>
          <div className="error" role="alert">
            {error}
          </div>
        </form>

        <div className="foot">Internal use only · The iScale</div>
      </main>
    </div>
  );
}
