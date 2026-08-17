const supabaseClient = window.supabase.createClient(
  window.SUPABASE_URL,
  window.SUPABASE_ANON_KEY
);

async function getSession() {
  const { data: { session } } = await supabaseClient.auth.getSession();
  return session;
}

async function signInWithGoogle() {
  await supabaseClient.auth.signInWithOAuth({
    provider: "google",
    options: { redirectTo: window.location.href },
  });
}

async function signOut() {
  await supabaseClient.auth.signOut();
  window.location.reload();
}

async function fetchWithAuth(url, opts = {}) {
  const session = await getSession();
  const headers = new Headers(opts.headers || {});
  if (session) {
    headers.set("Authorization", `Bearer ${session.access_token}`);
  }
  return fetch(url, { ...opts, headers });
}

// --- Remaining-generations badge -------------------------------------------
//
// Lives here rather than in a page script because both pages show it: /generate and
// /me/resume/import draw on the same sliding-window quota, so an import changing the number needs
// to be visible on the profile page too.

let usageCountdownTimer = null;

function formatTimeUntil(isoTimestamp) {
  const msRemaining = new Date(isoTimestamp).getTime() - Date.now();
  if (!Number.isFinite(msRemaining) || msRemaining <= 0) return "any moment";
  const totalMinutes = Math.ceil(msRemaining / 60000);
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  return hours > 0 ? `${hours}h ${minutes}m` : `${minutes}m`;
}

// Always re-reads the server value rather than decrementing locally: a failed generation refunds
// its slot, so the number can go *up*, and slots also age out of the window on their own.
async function refreshUsageBadge() {
  const badge = document.getElementById("usage-badge");
  if (!badge) return;

  if (usageCountdownTimer) {
    clearInterval(usageCountdownTimer);
    usageCountdownTimer = null;
  }

  const session = await getSession();
  if (!session) {
    badge.classList.add("hidden");
    return;
  }

  let usage;
  try {
    const resp = await fetchWithAuth("/me/usage");
    if (!resp.ok) {
      badge.classList.add("hidden");
      return;
    }
    usage = await resp.json();
  } catch {
    // A quota readout is not worth surfacing an error over -- just leave it hidden.
    badge.classList.add("hidden");
    return;
  }

  const render = () => {
    let text = `${usage.remaining} of ${usage.limit} generations left`;
    if (usage.next_reset_at && usage.remaining < usage.limit) {
      text += ` · +1 in ${formatTimeUntil(usage.next_reset_at)}`;
    }
    badge.textContent = text;
  };

  render();
  badge.classList.toggle("exhausted", usage.remaining === 0);
  badge.classList.remove("hidden");

  // Keep the "+1 in ..." text honest while the page sits open. Cheap: no network, just re-rendering
  // the same payload against the current clock.
  if (usage.next_reset_at && usage.remaining < usage.limit) {
    usageCountdownTimer = setInterval(render, 60000);
  }
}

// Renders the sign-in/sign-out control into #auth-bar and calls onReady(session)
// with session === null if signed out. Also re-runs on auth state changes
// (e.g. right after the OAuth redirect completes).
async function initAuth(onReady) {
  const authBar = document.getElementById("auth-bar");

  function render(session) {
    if (session) {
      authBar.innerHTML = "";
      const emailSpan = document.createElement("span");
      emailSpan.className = "auth-email";
      emailSpan.textContent = session.user.email;
      const signOutBtn = document.createElement("button");
      signOutBtn.textContent = "Sign out";
      signOutBtn.className = "auth-btn";
      signOutBtn.addEventListener("click", signOut);
      authBar.appendChild(emailSpan);
      authBar.appendChild(signOutBtn);
    } else {
      authBar.innerHTML = "";
      const signInBtn = document.createElement("button");
      signInBtn.textContent = "Sign in with Google";
      signInBtn.className = "auth-btn";
      signInBtn.addEventListener("click", signInWithGoogle);
      authBar.appendChild(signInBtn);
    }
    // Safe to call on every render: this callback re-runs on each auth state change, and
    // refreshUsageBadge is a fetch plus a textContent write with no accumulating state.
    refreshUsageBadge();
    if (onReady) onReady(session);
  }

  const session = await getSession();
  render(session);

  supabaseClient.auth.onAuthStateChange((_event, newSession) => {
    render(newSession);
  });
}
