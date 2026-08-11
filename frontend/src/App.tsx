import { useEffect, useState } from "react";
import { api, token } from "./lib/api";
import { Inbox } from "./components/Inbox";
import { Login } from "./components/Login";

type Status = "checking" | "in" | "out";

export default function App() {
  // A stored token may be expired or signed with a rotated secret, so it is
  // verified against the server before showing the inbox - otherwise the first
  // fetch fails and the console flashes a broken screen on the way to login.
  const [status, setStatus] = useState<Status>(token.get() ? "checking" : "out");

  useEffect(() => {
    if (status !== "checking") return;
    api
      .me()
      .then(() => setStatus("in"))
      .catch(() => {
        token.clear();
        setStatus("out");
      });
  }, [status]);

  if (status === "checking") return <div className="login" />;
  if (status === "out") return <Login onSignedIn={() => setStatus("in")} />;
  return <Inbox onSignedOut={() => setStatus("out")} />;
}
