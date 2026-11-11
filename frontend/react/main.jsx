import React, { useEffect } from "react";
import { createRoot } from "react-dom/client";
import parse from "html-react-parser";
import originalPage from "../index.html?raw";
import "../styles.css";
import "../state.css";

// Sprint 2 keeps the existing chat/controller behavior while moving the
// responsive page shell into React. The source HTML is shared with the local
// Python demo, so both ways of running the website show the same controls.
const sourceDocument = new DOMParser().parseFromString(originalPage, "text/html");
sourceDocument.body.querySelectorAll("script").forEach(script => script.remove());
const shell = parse(sourceDocument.body.innerHTML);

function SatiBotWebsite() {
  useEffect(() => {
    // The legacy controllers attach listeners to the React-mounted elements.
    // They own the chat feed until the Sprint 3 state migration is complete.
    void (async () => {
      await import("../auth.js");
      await import("../aws-connection.js");
      await import("../app.js");
    })();
  }, []);

  return <>{shell}</>;
}

createRoot(document.getElementById("root")).render(<SatiBotWebsite />);
