import React from "react";
import ReactDOM from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import "@fontsource/jetbrains-mono/latin-400.css";
import "@fontsource/jetbrains-mono/latin-600.css";
import "../../styles/global.css";
import Gallery from "./Gallery";

/** A35.11: the dev-only primitive gallery's entry. Served by `vite` at
 *  /gallery.html; `vite build` builds index.html only, so it never ships. */
ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <MemoryRouter>
      <Gallery />
    </MemoryRouter>
  </React.StrictMode>,
);
