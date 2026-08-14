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
    if (onReady) onReady(session);
  }

  const session = await getSession();
  render(session);

  supabaseClient.auth.onAuthStateChange((_event, newSession) => {
    render(newSession);
  });
}
