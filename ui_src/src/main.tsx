import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./index.css";
import "./eclipse.css";
import "./codemode.css";
import "./cinematic.css";
import "./lib/appearance";
import "./lib/telemetry";
import App from "./App";
import { CodeModeProvider } from './lib/codemode';

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <CodeModeProvider><App /></CodeModeProvider>
  </StrictMode>
);
