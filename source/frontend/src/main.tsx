import ReactDOM from "react-dom/client";
import { Access } from "./Access";
import "./styles.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <Access key="auth-gate-v2" />,
);

// The authentication gate can render before the dashboard component. Remove
// the HTML bootstrap overlay here rather than waiting for App's effect.
document.getElementById("boot-loading")?.remove();
