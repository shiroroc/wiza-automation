// External file on purpose: MV3 extension pages enforce script-src 'self',
// so an inline <script> in panel.html is silently blocked and never runs.
// Real extensions have the same constraint, which is why Wiza's panel is
// built this way too.

const q = new URLSearchParams(location.search);
const outcome = q.get("outcome") || "both";
const name = q.get("name") || "Asha Patel";
const person = document.getElementById("person");
const body = document.getElementById("body");

function render() {
  if (outcome === "nomatch") {
    person.textContent = "";          // the real no-match screen shows NO name
    body.innerHTML = `
      <div>We couldn't find this contact</div>
      <div>We weren't able to match this profile to a contact, so there's no
           contact information to reveal right now.</div>
      <div>Try opening another profile or check back later.</div>`;
    return;
  }

  person.textContent = name;
  // Masked preview state - the data is on screen but not real yet.
  body.innerHTML = `
    <div class="masked">Work email ***@example.com</div>
    <div class="masked">Phone number +1 (***) *** ****</div>
    <div class="masked">Personal email ***@***.com</div>
    <button id="reveal">Reveal contact info</button>
    <div>Unlimited reveals on your plan</div>`;

  document.getElementById("reveal").addEventListener("click", () => {
    body.innerHTML = `<div>Finding contact data...</div>
                      <div>Hang tight! It's coming in a few seconds</div>
                      <button id="forget">Forget lead</button>`;
    setTimeout(() => {
      if (outcome === "none") {
        body.innerHTML = `<div>Email</div><div>No email found</div>
                          <div>Phone number</div><div>No phone found</div>`;
      } else if (outcome === "email") {
        body.innerHTML = `<div>Email</div>
                          <div><a href="mailto:a.patel@globex.test">a.patel@globex.test</a></div>
                          <div>Phone number</div><div>No phone found</div>`;
      } else {
        body.innerHTML = `<div>Email</div>
                          <div><a href="mailto:a.patel@globex.test">a.patel@globex.test</a></div>
                          <div>Phone number</div>
                          <div><a href="tel:+14155550132">+1 (415) 555-0132</a></div>`;
      }
    }, 900);
  });
}

render();
