import { useEffect, useLayoutEffect } from "react";
import { createRoot } from "react-dom/client";
import { useFunnel } from "@use-funnel/browser";

const FUNNEL_ID = "flint-workspace";
const DEFAULT_SCREEN = "setup";
const SCREENS = ["overview", "setup", "tuning", "clients", "settings", "activity"];
const isScreen = value => SCREENS.includes(value);
const scrollOf = context => Number.isFinite(context?.scroll) && context.scroll >= 0 ? context.scroll : 0;
// Contexts come from history.state, so keep only the restorable scroll position.
const steps = Object.fromEntries(SCREENS.map(name => [name, { parse: context => ({ scroll: scrollOf(context) }) }]));
const requested = new URLSearchParams(window.location.search).get(`${FUNNEL_ID}.step`);
const initial = { step: isScreen(requested) ? requested : DEFAULT_SCREEN, context: { scroll: 0 } };

function Screen({ name, context }) {
  const scroll = scrollOf(context);
  useLayoutEffect(() => {
    window.dispatchEvent(new CustomEvent("flint:screen", { detail: { name, scroll } }));
  }, [name, scroll]);
  return null;
}

function WorkspaceNavigation() {
  const funnel = useFunnel({ id: FUNNEL_ID, initial, steps });
  const known = isScreen(funnel.step);

  useEffect(() => {
    document.documentElement.dataset.navigation = "funnel";
    return () => { delete document.documentElement.dataset.navigation; };
  }, []);

  useEffect(() => {
    // Unknown steps (from a stale or edited URL) are shown as the default screen and the URL is corrected.
    const current = new URLSearchParams(window.location.search).get(`${FUNNEL_ID}.step`);
    if (!known || (current !== null && !isScreen(current))) funnel.history.replace(DEFAULT_SCREEN, { scroll: 0 });
  }, [known, funnel.history]);

  useEffect(() => {
    function navigate(event) {
      const next = event.detail;
      if (!known || !isScreen(next) || next === funnel.step) return;
      const content = document.getElementById("workspace-content");
      // Save where the user was so browser Back returns to the same place.
      funnel.history.replace(funnel.step, { scroll: content ? content.scrollTop : 0 });
      funnel.history.push(next, { scroll: 0 });
    }
    window.addEventListener("flint:navigate", navigate);
    return () => window.removeEventListener("flint:navigate", navigate);
  }, [known, funnel.step, funnel.history]);

  if (!known) return null;
  return (
    <funnel.Render
      overview={({ context }) => <Screen name="overview" context={context} />}
      setup={({ context }) => <Screen name="setup" context={context} />}
      tuning={({ context }) => <Screen name="tuning" context={context} />}
      clients={({ context }) => <Screen name="clients" context={context} />}
      settings={({ context }) => <Screen name="settings" context={context} />}
      activity={({ context }) => <Screen name="activity" context={context} />}
    />
  );
}

const container = document.getElementById("navigation-root");
if (container) {
  // If React navigation fails, app.js falls back to direct tab switching.
  const fallback = () => { delete document.documentElement.dataset.navigation; };
  createRoot(container, { onUncaughtError: fallback }).render(<WorkspaceNavigation />);
}
