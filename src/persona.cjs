"use strict";

async function observePersona(page, onScreen) {
  const personaPageVisible = page.frames().some(frame => {
    try {
      return new URL(frame.url()).hostname === "inquiry.withpersona.com";
    } catch {
      return false;
    }
  });
  const personaPromptVisible = await onScreen(
    page.getByText(/^Verify Through Persona$/i).first(),
  );
  return { personaPageVisible, personaPromptVisible };
}

module.exports = { observePersona };
