/* PrepSage AI browser client.
 * The API and this small client are intentionally served by the same FastAPI
 * process. There is no build step, client-side framework, or second server.
 */
(function () {
  "use strict";

  const modes = {
    interview: {
      label: "Interview preparation",
      short: "Interview",
      icon: "◌",
      blurb:
        "A timed, conversational interview with a named AI interviewer, focused follow-ups and a saved transcript.",
    },
    group_discussion: {
      label: "Group discussion",
      short: "Group discussion",
      icon: "◎",
      blurb:
        "A simulated panel reports what your peers argued; you respond and are assessed on depth, clarity, initiative and collaboration.",
    },
    debate: {
      label: "Structured debate",
      short: "Debate",
      icon: "⚖",
      blurb:
        "Speech-by-speech rounds on your assigned side, adjudicated on matter, manner, method and role fulfilment.",
    },
    coaching: {
      label: "Personal coaching",
      short: "Coaching",
      icon: "✦",
      blurb:
        "Reflective one-to-one questions with warm, specific and actionable coaching notes.",
    },
  };
  const defaultQuestionCount = 7;
  const navHighlightStorageKey = "prepsage-nav-highlight-origin";
  const interviewers = {
    lisa: {
      name: "Explorer Lisa",
      initials: "L",
      description:
        "Warm, curious and precise. Lisa explores the story behind your answer.",
    },
    bob: {
      name: "Empathetic Bob",
      initials: "B",
      description:
        "Patient and encouraging. Bob listens for evidence, clarity and reflection.",
    },
  };
  const statuses = {
    created: "Setting up",
    in_progress: "In progress",
    paused: "Paused",
    completed: "Completed",
    abandoned: "Abandoned",
    failed: "Needs another try",
  };
  const state = {
    user: null,
    docs: [],
    home: null,
    sessions: null,
    session: null,
    live: null,
    sharedRoom: null,
    historyFilters: {},
    admin: { tab: "overview" },
  };
  let sharedRoomPollTimer = null;
  let sharedRoomHeartbeatTimer = null;
  let sharedRoomPollBusy = false;
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) =>
    Array.from(root.querySelectorAll(selector));
  const esc = (value) =>
    String(value ?? "").replace(
      /[&<>"']/g,
      (char) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[char],
    );
  const listItems = (payload, key) => {
    if (Array.isArray(payload)) return payload;
    const items = payload?.[key];
    return Array.isArray(items) ? items : [];
  };
  const modeLabel = (mode) => modes[mode]?.label || mode || "Practice";
  const fmt = (value, digits = 0) => Number(value || 0).toFixed(digits);
  const providerNotice = (session) =>
    session?.provider_degraded
      ? `<div class="notice amber" style="margin-top:14px" role="status">◌ ${esc(session.provider_notice || "The AI provider is temporarily unavailable. Your session is continuing with PrepSage's built-in coach.")}</div>`
      : "";
  const initials = (user) => {
    const words = String(user?.full_name || user?.email || "PS")
      .trim()
      .split(/\s+/);
    return (
      words.length > 1 ? words[0][0] + words[1][0] : words[0].slice(0, 2)
    ).toUpperCase();
  };
  const toast = (message, error = false) => {
    const old = $(".toast");
    if (old) old.remove();
    const node = document.createElement("div");
    node.className = "toast";
    // F4: toast announced to screen readers
    node.setAttribute("role", "status");
    node.setAttribute("aria-live", "polite");
    node.setAttribute("aria-atomic", "true");
    node.textContent = message;
    if (error) {
      node.style.color = "#9f2f2f";
      node.style.background = "#fff5f5";
      node.style.borderColor = "#efc7c7";
    }
    document.body.appendChild(node);
    setTimeout(() => node.remove(), 4500);
  };
  const errorText = (err) => {
    const detail = err?.detail;
    if (detail && typeof detail === "object")
      return (
        detail.message ||
        Object.values(detail.fields || {})[0] ||
        "Please correct the highlighted fields."
      );
    return detail || err?.message || "Something went wrong. Please try again.";
  };
  function clearFormErrors(form) {
    $$(".field", form).forEach((wrapper) => {
      wrapper.classList.remove("has-error");
      const input = $("[name]", wrapper);
      if (input) {
        input.removeAttribute("aria-invalid");
        input.removeAttribute("aria-describedby");
      }
      const error = $("[data-field-error]", wrapper);
      if (error) error.textContent = "";
    });
  }
  function fieldErrorSummary(form, fields = {}) {
    const items = Object.entries(fields)
      .filter(([, message]) => message)
      .map(([name, message]) => {
        const input = form.elements?.namedItem?.(name);
        const wrapper = input?.closest?.(".field");
        const label =
          $(".label", wrapper)?.textContent || name.replaceAll("_", " ");
        return `<li><strong>${esc(label)}:</strong> ${esc(message)}</li>`;
      });
    return items.length
      ? `<div class="field-error-summary" role="alert"><strong>Check these fields:</strong><ul>${items.join("")}</ul></div>`
      : "";
  }
  function applyFormErrors(form, fields = {}) {
    clearFormErrors(form);
    const first = Object.entries(fields).find(([, message]) => message);
    Object.entries(fields).forEach(([name, message]) => {
      const input = form.elements?.namedItem?.(name),
        wrapper = input?.closest?.(".field");
      if (!wrapper || !message) return;
      wrapper.classList.add("has-error");
      input.setAttribute("aria-invalid", "true");
      input.setAttribute("aria-describedby", `${name}-error`);
      const error = $("[data-field-error]", wrapper);
      if (error) error.textContent = message;
    });
    if (first && form.elements?.[first[0]]) form.elements[first[0]].focus();
  }
  async function api(url, options = {}) {
    // H2: attach CSRF token from cookie on all state-changing requests
    const csrfCookie = document.cookie
      .split(";")
      .map((c) => c.trim())
      .find((c) => c.startsWith("prepsage_csrf="));
    const csrfToken = csrfCookie ? csrfCookie.split("=")[1] : "";
    const isFormData = options.body instanceof FormData;
    const config = {
      credentials: "same-origin",
      ...options,
      headers: {
        ...(isFormData ? {} : { "Content-Type": "application/json" }),
        ...(csrfToken ? { "x-prepsage-csrf": csrfToken } : {}),
        ...(options.headers || {}),
      },
    };
    if (config.body && typeof config.body !== "string" && !isFormData) {
      config.body = JSON.stringify(config.body);
    }
    const response = await fetch(url, config);
    const text = await response.text();
    let data = {};
    try {
      data = text ? JSON.parse(text) : {};
    } catch (_) {
      data = text;
    }
    if (!response.ok)
      throw { status: response.status, detail: data?.detail || data };
    return data;
  }
  const nav = (href, label, active = false) =>
    `<a class="nav-link${active ? " active" : ""}" href="${href}"${active ? ' aria-current="page"' : ""}>${esc(label)}</a>`;
  function shell(content, active = "") {
    const user = state.user;
    const menuId = "mobile-nav-menu";
    const actions = user
      ? `<button class="nav-toggle" aria-expanded="false" aria-controls="${menuId}" aria-label="Toggle navigation menu">☰</button>
         <div class="nav-links" id="${menuId}" role="navigation" aria-label="Main navigation"><span class="nav-highlight" aria-hidden="true"></span>
           ${nav("/home", "Home", active === "home")}
           ${nav("/practice", "Practice", active === "practice")}
           ${nav("/arena", "Live rooms", active === "arena")}
           ${nav("/coach", "AI coach", active === "coach")}
           ${nav("/history", "History", active === "history")}
           ${nav("/account", "Account", active === "account")}
           ${user.role === "admin" ? nav("/admin", "Admin", active === "admin") : ""}
         </div>
         ${user.role === "admin" ? '<span class="role-chip admin">♢ Administrator</span>' : ""}
         <a class="user-pill" href="/account" aria-label="Your account: ${esc(user.full_name || user.email)}"><span class="avatar" aria-hidden="true">${esc(initials(user))}</span><span>${esc(user.full_name || user.email)}</span></a>
         `
      : `<a class="btn small" href="/register">Start practicing&nbsp; →</a>`;
    return `<a href="#main-content" class="skip-link">Skip to main content</a>
<div class="page-shell">
  <header class="topbar" role="banner">
    <div class="topbar-inner">
      <a class="brand" href="/" aria-label="PrepSage home">
        <span class="brand-name"><strong>PrepSage</strong><small>Academic coaching studio</small></span>
      </a>
      <nav aria-label="Main navigation">${actions}</nav>
    </div>
  </header>
  <main class="main" id="main-content" tabindex="-1">${content}</main>
  <footer class="footer" role="contentinfo">
    <div class="footer-inner">
       <span>PrepSage AI — an editorial coaching studio for exams, interviews and debate.</span>
    </div>
  </footer>
</div>`;
  }
  function render(html, active = "") {
    releaseInterviewCamera();
    document.getElementById("app").innerHTML = shell(html, active);
    // F1: mobile nav toggle
    const toggle = $(".nav-toggle");
    const menu = toggle ? document.getElementById("mobile-nav-menu") : null;
    setupNavHighlight(toggle, menu);
    if (toggle && menu) {
      toggle.onclick = () => {
        const expanded = toggle.getAttribute("aria-expanded") === "true";
        toggle.setAttribute("aria-expanded", String(!expanded));
        menu.classList.toggle("open", !expanded);
      };
    }
    bindLogoutButton($('[data-action="logout"]'));
    if (window.PrepSageMotion && window.PrepSageMotion.afterRender) {
      window.PrepSageMotion.afterRender(document.getElementById("app"));
    }
  }
  function setupNavHighlight(toggle, menu) {
    if (!menu) return;
    const highlight = $(".nav-highlight", menu);
    const activeLink = $(".nav-link.active", menu);
    if (!highlight || !activeLink) return;

    let originPath = null;
    try {
      originPath = sessionStorage.getItem(navHighlightStorageKey);
      sessionStorage.removeItem(navHighlightStorageKey);
    } catch (_) {}

    const links = [...menu.querySelectorAll(".nav-link")];
    const originLink = links.find(
      (link) => link.getAttribute("href") === originPath,
    );
    const reducedMotion = window.matchMedia(
      "(prefers-reduced-motion: reduce)",
    ).matches;
    const placeAt = (link) => {
      highlight.style.width = `${link.offsetWidth}px`;
      highlight.style.height = `${link.offsetHeight}px`;
      highlight.style.transform = `translate3d(${link.offsetLeft}px, ${link.offsetTop}px, 0)`;
    };
    const sync = () => {
      if (getComputedStyle(menu).display === "none") return;
      menu.classList.remove("nav-highlight-ready");
      if (originLink && originLink !== activeLink && !reducedMotion) {
        placeAt(originLink);
        void highlight.offsetWidth;
        menu.classList.add("nav-highlight-ready");
        requestAnimationFrame(() =>
          requestAnimationFrame(() => placeAt(activeLink)),
        );
      } else {
        placeAt(activeLink);
        requestAnimationFrame(() => menu.classList.add("nav-highlight-ready"));
      }
    };

    sync();
    toggle?.addEventListener("click", () => requestAnimationFrame(sync));
    window.addEventListener("resize", () => {
      if (getComputedStyle(menu).display !== "none") placeAt(activeLink);
    });
  }
  function bindLogoutButton(button) {
    if (!button) return;
    button.onclick = async () => {
      try {
        await api("/api/auth/logout", { method: "POST" });
        state.user = null;
        location.href = "/login";
      } catch (err) {
        toast(errorText(err), true);
      }
    };
  }
  function alertBox(message, success = false) {
    return message
      ? `<div class="alert ${success ? "success" : ""}" role="alert">${success ? "✓" : "!"}&nbsp; ${esc(message)}</div>`
      : "";
  }
  function stat(label, value, note = "") {
    const empty = [
      "Not assessed",
      "No completed sessions yet",
      "Not measured",
      "No sessions yet",
      "No open tasks",
    ].includes(String(value));
    return `<div class="stat${empty ? " is-empty" : ""}"><small>${esc(label)}</small><strong>${esc(value)}</strong><span>${esc(note)}</span></div>`;
  }
  function progress(value) {
    return `<div class="progress"><span style="width:${Math.max(0, Math.min(100, Number(value || 0)))}%"></span></div>`;
  }
  function field(label, name, options = {}) {
    const tag = options.textarea
      ? `<textarea class="textarea" id="${name}" name="${name}" rows="${options.rows || 4}" placeholder="${esc(options.placeholder || "")}" ${options.required === false ? "" : "required"}>${esc(options.value || "")}</textarea>`
      : options.select
        ? `<select class="select" id="${name}" name="${name}" ${options.required === false ? "" : "required"}>${options.options.map(([value, text]) => `<option value="${esc(value)}" ${String(value) === String(options.value || "") ? "selected" : ""}>${esc(text)}</option>`).join("")}</select>`
        : `<input class="input" id="${name}" name="${name}" type="${options.type || "text"}" value="${esc(options.value || "")}" placeholder="${esc(options.placeholder || "")}" ${options.required === false ? "" : "required"} autocomplete="${options.autocomplete || "off"}">`;
    return `<label class="field" data-field="${esc(name)}"><span class="label">${esc(label)}</span>${tag}${options.hint ? `<span class="field-hint">${esc(options.hint)}</span>` : ""}<span class="field-error" id="${esc(name)}-error" data-field-error="${esc(name)}"></span></label>`;
  }
  function passwordField(label, name, options = {}, strength = false) {
    const input = `<div class="password-control"><input class="input" id="${name}" name="${name}" type="password" placeholder="${esc(options.placeholder || "")}" required autocomplete="${options.autocomplete || "current-password"}"><button class="password-toggle" type="button" data-toggle-password="${name}" aria-controls="${name}" aria-pressed="false">Show</button></div>`;
    const meter = strength
      ? `<div class="password-strength" id="password-strength" aria-live="polite">
      <div class="strength-heading"><strong data-strength-label>Enter a password</strong><span data-strength-count>0 / 5</span></div>
      <div class="strength-meter" role="meter" aria-label="Password strength" aria-valuemin="0" aria-valuemax="5" aria-valuenow="0"><span></span></div>
      <ul class="password-rules">
        <li data-rule="length">10+ characters</li>
        <li data-rule="case">Upper &amp; lowercase</li>
        <li data-rule="number">Number</li>
        <li data-rule="special">Special character</li>
        <li data-rule="personal">Doesn't contain your name or email</li>
      </ul>
    </div>`
      : "";
    return `<label class="field password-field" data-field="${esc(name)}"><span class="label">${esc(label)}</span>${input}${meter}<span class="field-error" id="${esc(name)}-error" data-field-error="${esc(name)}"></span></label>`;
  }

  function welcome() {
    const cta = state.user
      ? `<a class="ps-btn ps-btn-primary ps-glow ps-magnetic" href="/home">Start Your Preparation <span>→</span></a><a class="ps-btn ps-btn-ghost" href="/history">Track Results</a>`
      : `<a class="ps-btn ps-btn-primary ps-glow ps-magnetic" href="/register">Start Your Preparation <span>→</span></a><a class="ps-btn ps-btn-ghost" href="/login">Sign in ↗</a>`;
    render(`<div class="ps-home">
      <section class="ps-hero ps-wrap">
        <div class="ps-hero-bg" aria-hidden="true">
          <video class="ps-hero-bg-video" autoplay muted loop playsinline preload="auto" disablepictureinpicture poster="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='16' height='9'%3E%3Crect width='16' height='9' fill='%2304132f'/%3E%3C/svg%3E">
            <source src="/static/student-practice.mp4?v=2" type="video/mp4" />
          </video>
          <div class="ps-hero-bg-shade"></div>
        </div>
        <div class="ps-hero-copy">
          <h1 class="ps-h1"><span class="ps-line"><span class="ps-line-in">Prepare Today.</span></span><span class="ps-line ps-line-grad"><span class="ps-line-in">Perform Tomorrow.</span></span></h1>
          <p>Master interviews, group discussions and debates with realistic AI practice, personal feedback and progress that helps you move forward.</p>
          <div class="ps-typing-line"><span class="ps-typing-label">Now practising</span><span class="ps-typing" data-typing="Interviews|Group discussions|Structured debates|Personal coaching"></span></div>
          <div class="ps-hero-actions">${cta}</div>
          <div class="ps-proof"><span>✦</span> Practice with purpose. Grow with confidence.</div>
        </div>
        <div class="ps-hero-visual" aria-label="Student practicing with a laptop" data-parallax="0.035">
          <div class="ps-orbit ps-orbit-one"></div><div class="ps-orbit ps-orbit-two"></div>
          <span class="ps-spark ps-s1">✧</span><span class="ps-spark ps-s2">✦</span><span class="ps-spark ps-s3">✳</span>
          <div class="ps-float ps-float-top"><span class="ps-mini-icon blue">✦</span><span><b>Group Discussion</b><small>Build your voice</small></span><span class="ps-arrow">↗</span></div>
          <div class="ps-float ps-float-mid"><span class="ps-mini-icon violet">♙</span><span><b>Debate</b><small>Sharpen your thinking</small></span><span class="ps-arrow">↗</span></div>
          <div class="ps-float ps-float-bottom"><span class="ps-mini-icon teal">◉</span><span><b>Interview</b><small>Practice with AI</small></span><span class="ps-arrow">↗</span></div>
        </div>
        <button type="button" class="ps-scroll-cue" data-scroll-to="ps-practice" aria-label="Scroll to practice rooms"><span></span>Scroll to explore</button>
      </section>
      <section class="ps-wrap ps-practice" id="ps-practice">
        <div class="ps-section-head"><div><span class="ps-kicker">FIND YOUR PRACTICE ROOM</span><h2>Make every practice count.</h2></div><span class="ps-hand-note">Your next step starts here ↗</span></div>
        <div class="ps-room-grid">
          <a class="ps-room ps-room-blue" href="/practice"><span class="ps-room-icon">♧</span><h3>Group Discussion</h3><p>Practice speaking up, sharing ideas and collaborating with an AI-powered panel.</p><span class="ps-card-link">Start GD practice <b>↗</b></span></a>
          <a class="ps-room ps-room-purple" href="/practice"><span class="ps-room-icon">♆</span><h3>Debate</h3><p>Build stronger arguments, handle rebuttals and learn to think on your feet.</p><span class="ps-card-link">Open Debate Arena <b>↗</b></span></a>
          <a class="ps-room ps-room-green" href="/practice"><span class="ps-room-icon">♙</span><h3>Interview</h3><p>Rehearse real interview questions and get thoughtful, structured feedback.</p><span class="ps-card-link">Start mock interview <b>↗</b></span></a>
          <a class="ps-room ps-room-orange" href="/resources"><span class="ps-room-icon">▤</span><h3>Resources</h3><p>Explore helpful preparation materials, guides and learning resources.</p><span class="ps-card-link">Explore resources <b>↗</b></span></a>
        </div>
      </section>
      <section class="ps-wrap ps-different">
        <div class="ps-section-head"><div><span class="ps-kicker">A SMARTER WAY TO PREPARE</span><h2>What Makes PrepSage Different?</h2><p>More than just practice. It's your growth toolkit.</p></div><span class="ps-hand-note">Learn • Improve • Repeat</span></div>
        <div class="ps-feature-grid">
          <article class="ps-feature"><span class="ps-feature-icon violet">✺</span><h3>AI-Powered Feedback</h3><p>Understand your strengths and what to improve next.</p><small>Review your progress ↗</small></article>
          <article class="ps-feature"><span class="ps-feature-icon aqua">▦</span><h3>Real-World Scenarios</h3><p>Practice with realistic prompts and conversations.</p><small>Practice for real ↗</small></article>
          <article class="ps-feature"><span class="ps-feature-icon blue">♙</span><h3>Expert Guidelines</h3><p>Build strong habits with structured evaluation criteria.</p><small>Build good habits ↗</small></article>
          <article class="ps-feature"><span class="ps-feature-icon orange">♧</span><h3>Personalized Learning</h3><p>Focus your preparation around your own goals.</p><small>Find your focus ↗</small></article>
          <article class="ps-feature"><span class="ps-feature-icon pink">▣</span><h3>Learn Anywhere</h3><p>Return to your practice journey whenever you're ready.</p><small>Keep learning ↗</small></article>
          <article class="ps-feature"><span class="ps-feature-icon pink ps-track-icon">◎</span><h3>Progress Tracking</h3><p>Track your growth with detailed insights and practice history.</p><small><a href="/history">View Progress ↗</a></small></article>
        </div>
      </section>
      <section class="ps-wrap ps-skills">
        <div class="ps-section-head"><div><span class="ps-kicker">SMALL STEPS. STRONGER SKILLS.</span><h2>Skills You'll Build</h2><p>Build the confidence that carries into your future.</p></div><span class="ps-trophy">♜</span></div>
        <div class="ps-skill-grid">
          <article><span class="ps-hex blue">▤</span><b>Communication</b><small>Express ideas clearly</small></article>
          <article><span class="ps-hex violet">♧</span><b>Critical Thinking</b><small>Analyze and respond</small></article>
          <article><span class="ps-hex green">◇</span><b>Confidence</b><small>Speak with assurance</small></article>
          <article><span class="ps-hex orange">♟</span><b>Teamwork</b><small>Collaborate naturally</small></article>
          <article><span class="ps-hex pink">◎</span><b>Interview Skills</b><small>Present your best self</small></article>
        </div>
      </section>
      <section class="ps-wrap ps-bottom-cta"><div><span class="ps-kicker">YOUR POTENTIAL IS WAITING</span><h2>Ready to Build a Stronger You?</h2><p>Start practicing today and take the first step toward your dream career.</p></div><a class="ps-btn ps-btn-yellow ps-magnetic" href="${state.user ? "/home" : "/register"}">Get Started <span>→</span></a><span class="ps-cta-doodle">✳</span></section>
    </div>`);
    initHeroIntro();
    initScrollFx();
  }
  function initScrollFx() {
    const home = document.querySelector(".ps-home");
    if (!home) return;
    const groups = [
      ".ps-section-head",
      ".ps-room",
      ".ps-feature",
      ".ps-skill-grid article",
      ".ps-bottom-cta > *",
    ];
    const els = [];
    groups.forEach((sel) => {
      home.querySelectorAll(sel).forEach((el, i) => {
        el.classList.add("ps-reveal");
        el.style.setProperty("--d", (i % 6) * 0.08 + "s");
        els.push(el);
      });
    });
    if (!("IntersectionObserver" in window)) {
      els.forEach((el) => el.classList.add("ps-in"));
      return;
    }
    const io = new IntersectionObserver(
      (entries) => {
        entries.forEach((e) => {
          if (e.isIntersecting) {
            e.target.classList.add("ps-in");
            io.unobserve(e.target);
          }
        });
      },
      { threshold: 0.12, rootMargin: "0px 0px -6% 0px" },
    );
    els.forEach((el) => io.observe(el));
    home.querySelectorAll(".ps-room, .ps-feature").forEach((card) => {
      card.addEventListener("pointermove", (ev) => {
        const r = card.getBoundingClientRect();
        card.style.setProperty("--mx", ev.clientX - r.left + "px");
        card.style.setProperty("--my", ev.clientY - r.top + "px");
      });
    });
  }
  function initHeroIntro() {
    const home = document.querySelector(".ps-home");
    const video = home && home.querySelector(".ps-hero-bg-video");
    if (!home) return;
    let textDone = false,
      videoDone = false;
    const showText = () => {
      if (textDone) return;
      textDone = true;
      requestAnimationFrame(() =>
        requestAnimationFrame(() => home.classList.add("ps-loaded")),
      );
    };
    // Reveal the video only once real frames are playing, so the browser's
    // paused first frame never shows as a still image.
    const showVideo = () => {
      if (videoDone) return;
      videoDone = true;
      home.classList.add("ps-video-on");
    };
    if (video) {
      const onPlaying = () => {
        if (video.requestVideoFrameCallback)
          video.requestVideoFrameCallback(showVideo);
        else showVideo();
      };
      video.addEventListener("playing", onPlaying, { once: true });
      if (!video.paused && video.currentTime > 0) onPlaying();
      const p = video.play();
      if (p && p.catch) p.catch(() => {});
    }
    setTimeout(showText, 500);
  }
  function authPage(register = false, message = "", success = false) {
    const body = `<div class="form-layout"><div class="ps-auth-aura" aria-hidden="true"></div><section class="card form-card"><div class="eyebrow">${register ? "Open your notebook" : "Welcome back"}</div><div class="form-title"><h1 style="font-size:34px;margin-top:8px">${register ? "Create account" : "Sign in"}</h1><p>${register ? "One account holds your goals, rehearsals and readiness history." : "Pick up your preparation path exactly where you left it."}</p></div><div id="form-message" style="margin-top:18px">${alertBox(message, success)}</div><form id="${register ? "register" : "login"}-form" class="form" novalidate>${register ? field("Full name", "full_name", { placeholder: "Ada Lovelace", autocomplete: "name" }) : ""}${field("Email address", "email", { type: "email", placeholder: "you@school.edu", autocomplete: "email", hint: register ? "We normalise and lowercase your email before saving it." : "" })}${passwordField("Password", "password", { placeholder: register ? "Choose a strong password" : "Your password", autocomplete: register ? "new-password" : "current-password" }, register)}${register ? passwordField("Confirm password", "confirm_password", { placeholder: "Repeat your password", autocomplete: "new-password" }) : ""}<button class="btn ps-glow ps-magnetic" type="submit">${register ? "Create account →" : "Sign in ↪"}</button></form><p class="muted" style="margin-top:20px;padding-top:16px;border-top:1px dashed rgba(18,35,63,.2);font-size:13px">${register ? "Already registered? " : "New to PrepSage? "}<a class="link" href="${register ? "/login" : "/register"}">${register ? "Sign in" : "Start practicing"}</a></p></section></div>`;
    render(body, register ? "register" : "login");
    const form = $(`#${register ? "register" : "login"}-form`);
    $$("[data-toggle-password]", form).forEach((button) => {
      button.addEventListener("click", () => {
        const input = form.elements.namedItem(button.dataset.togglePassword);
        const showing = input.type === "text";
        input.type = showing ? "password" : "text";
        button.textContent = showing ? "Show" : "Hide";
        button.setAttribute("aria-pressed", String(!showing));
      });
    });
    if (register) {
      const password = form.elements.namedItem("password");
      const email = form.elements.namedItem("email");
      const fullName = form.elements.namedItem("full_name");
      const strength = $("#password-strength", form);
      const updateStrength = () => {
        const value = password.value;
        const emailText = email.value.trim().toLowerCase();
        const emailLocal = emailText.split("@")[0];
        const nameParts = fullName.value
          .trim()
          .toLowerCase()
          .split(/\s+/)
          .filter((part) => part.length >= 4);
        const personalMatches =
          Boolean(value) &&
          ((emailLocal.length >= 4 &&
            value.toLowerCase().includes(emailLocal)) ||
            (emailText.length >= 4 &&
              value.toLowerCase().includes(emailText)) ||
            nameParts.some((part) => value.toLowerCase().includes(part)));
        const rules = {
          length: value.length >= 10,
          case: /[a-z]/.test(value) && /[A-Z]/.test(value),
          number: /\d/.test(value),
          special: /[^\p{L}\p{N}]/u.test(value),
          personal: Boolean(value) && !personalMatches,
        };
        const score = Object.values(rules).filter(Boolean).length;
        const labels = [
          "Enter a password",
          "Very weak",
          "Weak",
          "Fair",
          "Good",
          "Strong",
        ];
        strength.classList.toggle("has-password", Boolean(value));
        strength.classList.toggle("is-strong", score === 5);
        strength.querySelector("[data-strength-label]").textContent =
          labels[score];
        strength.querySelector("[data-strength-count]").textContent =
          `${score} / 5`;
        strength
          .querySelector(".strength-meter")
          .setAttribute("aria-valuenow", String(score));
        strength.querySelector(".strength-meter > span").style.width =
          `${score * 20}%`;
        Object.entries(rules).forEach(([rule, passed]) => {
          const item = strength.querySelector(`[data-rule="${rule}"]`);
          item.classList.toggle("passed", passed);
          item.setAttribute("aria-checked", String(passed));
        });
      };
      [password, email, fullName].forEach((input) =>
        input.addEventListener("input", updateStrength),
      );
      updateStrength();
    }
    form.onsubmit = async (event) => {
      event.preventDefault();
      const data = Object.fromEntries(new FormData(form).entries());
      clearFormErrors(form);
      const message = $("#form-message");
      message.innerHTML = `<div class="loading">Checking your details…</div>`;
      try {
        const result = await api(
          `/api/auth/${register ? "register" : "login"}`,
          { method: "POST", body: data },
        );
        if (register) {
          authPage(
            false,
            result.message || "Welcome aboard — please sign in to begin.",
            true,
          );
        } else {
          state.user = result.user;
          const destination = consumePostAuthDestination();
          if (window.PrepSageMotion && window.PrepSageMotion.queueOpening) {
            window.PrepSageMotion.queueOpening(
              result.user.full_name || result.user.email,
            );
          }
          location.href = destination;
        }
      } catch (err) {
        const fields =
          err?.detail && typeof err.detail === "object"
            ? err.detail.fields
            : null;
        message.innerHTML =
          alertBox(errorText(err)) +
          (fields ? fieldErrorSummary(form, fields) : "");
        if (fields) applyFormErrors(form, fields);
      }
    };
    form.addEventListener("input", (event) => {
      const wrapper = event.target.closest(".field");
      if (!wrapper) return;
      wrapper.classList.remove("has-error");
      event.target.removeAttribute("aria-invalid");
      event.target.removeAttribute("aria-describedby");
      const error = $("[data-field-error]", wrapper);
      if (error) error.textContent = "";
    });
  }

  function homePage() {
    render(
      `<div class="container page-pad"><div id="home-content" class="loading">Opening your preparation briefing…</div></div>`,
      "home",
    );
    loadHome();
  }
  async function loadHome() {
    try {
      state.home = await api("/api/home");
      drawHome();
    } catch (err) {
      $("#home-content").innerHTML = alertBox(errorText(err));
    }
  }
  function drawHome() {
    const h = state.home || {},
      onboarding = h.onboarding || {},
      summary = h.summary || {},
      goals = h.goals || [],
      tasks = h.tasks || [],
      activity = h.activity || [],
      analytics = h.analytics || {};
    const completed = Boolean(onboarding.completed),
      completedSessions = Number(summary.completed_sessions || 0),
      hasCompletedSession = completedSessions > 0,
      readiness = Number(summary.readiness || 0),
      activeGoals = Number(
        summary.active_goals ||
          goals.filter((g) => g.status === "active").length,
      );
    const weeklyTarget = Number(
        summary.weekly_target || onboarding.weekly_target || 3,
      ),
      weekSessions = Number(summary.week_sessions || 0),
      weeklyPercent = Number(summary.weekly_percent || 0);
    const brief = `<section class="card"><div class="briefing-head"><div><div class="eyebrow">Today's briefing</div><h1 style="font-size:clamp(30px,4vw,42px);margin-top:8px">Good to see you, ${esc(state.user.full_name || state.user.email)}.</h1><p class="body-copy" style="margin-top:8px">${onboarding.target_role ? `Preparing for ${esc(onboarding.target_role)}.` : "Set your preparation brief and the path below fills in."}</p></div><div class="readiness-box${hasCompletedSession ? "" : " is-empty"}"><small>Readiness</small><strong>${hasCompletedSession ? fmt(readiness) : "Not assessed"}</strong><span>${hasCompletedSession ? (readiness >= 80 ? "Interview ready" : readiness >= 60 ? "Building confidence" : "Foundations forming") : "Not assessed"}</span></div></div><div class="briefing-body"><div class="path-list"><small>The path from here</small><ol><li class="${completed ? "done" : ""}"><b>${completed ? "✓" : "I"}</b><strong>Brief</strong><span>${completed ? "Target set and ready" : "Preparation brief incomplete"}</span></li><li class="${activeGoals ? "done" : ""}"><b>${activeGoals ? "✓" : "II"}</b><strong>Goals</strong><span>${activeGoals ? `${activeGoals} active goals` : "No active goals yet"}</span></li><li class="${hasCompletedSession ? "done" : ""}"><b>${hasCompletedSession ? "✓" : "III"}</b><strong>Rehearse</strong><span>${hasCompletedSession ? `${completedSessions} completed sessions` : "No completed sessions yet"}</span></li><li class="${summary.answers_recorded ? "done" : ""}"><b>${summary.answers_recorded ? "✓" : "IV"}</b><strong>Review</strong><span>${summary.answers_recorded ? `${summary.answers_recorded} answers on record` : "No feedback yet"}</span></li></ol></div><div class="commitment"><div class="commitment-head"><span>This week's commitment</span><strong>${weekSessions ? `${fmt(weeklyPercent)}%` : "Not started"}</strong></div>${progress(weeklyPercent)}<p>${weekSessions ? `${weekSessions} of ${weeklyTarget} rehearsals logged in the last 7 days` : `No rehearsals logged in the last 7 days · target ${weeklyTarget}`}</p><div class="row" style="margin-top:18px"><a class="btn small" href="/practice">▶ Start a session</a><button class="btn secondary small" data-action="toggle-brief">${completed ? "Edit brief" : "Complete brief"}</button></div></div></div><p class="muted serif" style="margin-top:20px;padding-top:12px;border-top:1px solid rgba(18,35,63,.1);font-size:13px;font-style:italic">Every stage below is recorded against your account, so you can pause and resume without losing the thread.</p></section>`;
    const briefForm = `<section id="brief-form" class="card soft-card hidden"><div class="row between"><h2>Your preparation brief</h2><button class="btn secondary small" data-action="toggle-brief">Close</button></div><p class="muted" style="margin-top:6px;font-size:13px">These answers shape your readiness maths, your weekly pace and every recommendation below.</p><form id="brief-save" class="form"><div class="form-grid">${field("Target role, exam or motion", "target_role", { value: onboarding.target_role, placeholder: "Product analyst graduate scheme" })}${field(
      "Experience so far",
      "experience_level",
      {
        value: onboarding.experience_level,
        select: true,
        options: [
          ["", "Choose your starting point"],
          ["first_time", "First time preparing"],
          ["some_practice", "Some practice already"],
          ["experienced", "Experienced, sharpening"],
        ],
      },
    )}${field("Preparation target date", "target_date", { type: "date", value: onboarding.target_date || "" })}${field("Sessions per week", "weekly_target", { type: "number", value: onboarding.weekly_target || 3, hint: "Between 1 and 14." })}${field("Minutes per week", "weekly_minutes", { type: "number", value: onboarding.weekly_minutes || 60 })}${field("Focus note", "focus_note", { value: onboarding.focus_note || "", required: false, placeholder: "Structure my answers under pressure" })}</div><fieldset style="border:0;border-top:1px dashed rgba(18,35,63,.2);padding:16px 0 0;margin:0"><legend class="label">Preferred practice modes</legend><div class="row" style="margin-top:10px">${Object.entries(
      modes,
    )
      .map(
        ([key, value]) =>
          `<label class="tag" style="cursor:pointer"><input type="checkbox" name="preferred_modes" value="${key}" ${(onboarding.preferred_modes || []).includes(key) ? "checked" : ""}> ${value.short}</label>`,
      )
      .join(
        "",
      )}</div></fieldset><button class="btn" type="submit">Save brief&nbsp; ✓</button></form></section>`;
    const goalCards = goals.length
      ? goals
          .map(
            (g) =>
              `<article class="goal"><div class="goal-top"><div><h3>${esc(g.title)}</h3><small>${esc(modeLabel(g.mode))}</small></div><span class="tag">${esc(g.status)}</span></div>${g.description ? `<p>${esc(g.description)}</p>` : ""}<div style="margin-top:13px">${progress(g.progress_percent)}<p>${g.completed_sessions || 0} of ${g.target_sessions || 0} sessions · ${fmt(g.progress_percent)}%</p></div><div class="goal-actions"><button class="btn secondary" data-action="edit-goal" data-id="${g.id}">✎ Edit</button><button class="btn teal" data-action="goal-status" data-id="${g.id}" data-status="${g.status === "achieved" ? "active" : "achieved"}">${g.status === "achieved" ? "↺ Reopen" : "✓ Mark achieved"}</button><button class="btn secondary" data-action="goal-status" data-id="${g.id}" data-status="archived">Archive</button></div></article>`,
          )
          .join("")
      : `<div class="empty"><h3>No goals on the page yet</h3><p>A goal turns intention into a number: choose a mode and how many rehearsals you will log.</p></div>`;
    const taskRows = tasks.length
      ? `<ul class="list">${tasks.map((t) => `<li class="row"><button class="btn teal small" data-action="task-status" data-id="${t.id}" data-status="done">✓</button><div style="flex:1;min-width:150px"><strong>${esc(t.title)}</strong><p class="muted" style="font-size:12px;margin-top:3px">${esc(modeLabel(t.mode))} · ${t.estimated_minutes || 30} min · ${t.due_at ? esc(new Date(t.due_at).toLocaleDateString()) : "No date"}</p></div><button class="btn secondary small" data-action="task-status" data-id="${t.id}" data-status="skipped">Skip</button></li>`).join("")}</ul>`
      : `<div class="empty" style="margin-top:16px"><h3>Your schedule is clear</h3><p>Add one dated task — twenty focused minutes is enough to keep the thread.</p></div>`;
    const activities = activity.length
      ? `<ul class="list">${activity.map((s) => `<li><a class="row between" href="/history/${encodeURIComponent(s.public_id)}"><span><strong>${esc(s.title)}</strong><small class="muted" style="display:block;margin-top:4px">${esc(modeLabel(s.mode))} · ${esc(s.created_label || "")}</small></span><span class="serif">${fmt(s.percentage_score)}%</span></a></li>`).join("")}</ul>`
      : `<div class="empty" style="margin-top:16px"><p>Your first rehearsal will appear here.</p></div>`;
    const completedCount = Number(summary.completed_sessions || 0),
      openSessions = Number(summary.open_sessions || 0),
      recordedAnswers = Number(summary.answers_recorded || 0),
      openTasks = Number(summary.open_tasks || 0),
      completedTasks = Number(summary.completed_tasks || 0);
    const content = `${brief}<div id="brief-wrap" style="margin-top:20px">${briefForm}</div><div class="stats" style="margin-top:20px">${stat("Sessions completed", completedCount || "No completed sessions yet", openSessions ? `${openSessions} still open` : "No sessions yet")}${stat("Average score", completedCount ? `${fmt(summary.average_score, 1)}%` : "Not measured", completedCount ? "Across completed sessions" : "Complete a rehearsal to see your score")}${stat("Practice minutes", completedCount ? summary.practice_minutes || 0 : "No sessions yet", completedCount && recordedAnswers ? `${recordedAnswers} answers recorded` : "Minutes appear after a rehearsal")}${stat("Open tasks", openTasks || "No open tasks", completedTasks ? `${completedTasks} completed to date` : "Add a task when you're ready")}</div><section class="card soft-card" style="margin-top:20px"><div class="section-head row between"><div><div class="eyebrow">Performance notebook</div><h2 style="margin-top:7px">What your practice is saying</h2><p>A record of your scores, practice mix and coach's guidance.</p></div><a class="btn secondary small" href="/history">Open history</a></div>${performanceNotebook(analytics)}</section><div class="dashboard-grid" style="margin-top:20px"><div class="stack"><section class="card"><div class="section-head row between"><div><h2>Recent performance</h2><p>Scores from your completed sessions appear here.</p></div><a class="btn secondary small" href="/history">Full history</a></div>${trendChart(activity)}</section><section class="card"><div class="section-head row between"><div><h2>Preparation goals</h2><p>Turn intention into a number.</p></div><button class="btn secondary small" data-action="new-goal">＋ New goal</button></div><div id="goal-form"></div><div class="grid two-col" style="margin-top:16px">${goalCards}</div></section></div><div class="stack"><section class="card soft-card"><h2>Margin notes for you</h2><p class="muted" style="font-size:13px;margin-top:6px">Guidance drawn from your brief, goals and schedule.</p><div class="notice" style="margin-top:16px">✦ ${completed ? "Keep a steady cadence: one focused rehearsal this week will move your path forward." : "Complete your preparation brief to make every recommendation more personal."}</div></section><section class="card soft-card"><div class="section-head row between"><div><h2>Upcoming tasks</h2><p>Small, dated commitments between rehearsals.</p></div><button class="btn secondary small" data-action="new-task">＋ Add task</button></div><div id="task-form"></div>${taskRows}</section><section class="card soft-card"><h2>Recent activity</h2><p class="muted" style="font-size:13px;margin-top:6px">Each entry opens its report.</p>${activities}</section></div></div>`;
    $("#home-content").innerHTML = content;
    renderProgressCard(summary);
    bindHome();
  }
  function renderProgressCard(summary) {
    const root = $("#home-content"),
      earned = summary.achievements || [],
      definitions = summary.achievement_definitions || [];
    if (!root) return;
    const card = document.createElement("section");
    card.className = "card soft-card";
    card.style.marginTop = "20px";
    const level = summary.level || "beginner",
      lp = summary.level_progress || {};
    const earnedKeys = new Set(earned.map((item) => item.key));
    const nextLocked = definitions
      .filter((item) => !earnedKeys.has(item.key))
      .slice(0, 3);
    const visibleKeys = new Set([
      ...earnedKeys,
      ...nextLocked.map((item) => item.key),
    ]);
    const visibleDefinitions = definitions.filter((item) =>
      visibleKeys.has(item.key),
    );
    const hiddenCount = definitions.length - visibleDefinitions.length;
    const hasProgress =
      Number(summary.total_points || 0) > 0 ||
      Number(summary.practice_streak || 0) > 0;
    card.innerHTML = `<div class="row between"><div><div class="eyebrow">Progression</div><h2 style="margin-top:7px">Keep your practice streak alive</h2><p class="muted" style="font-size:13px;margin-top:5px">${hasProgress ? `${summary.total_points || 0} points · ${summary.practice_streak || 0} day streak` : "Complete a rehearsal to earn points and unlock badges."}</p></div><span class="tag">${esc(level)} · ${lp.points_to_next ? `${lp.points_to_next} pts to ${esc(lp.next_level)}` : "top level"}</span></div>${progress(lp.percentage || 0)}<div class="achievement-grid" style="margin-top:16px">${visibleDefinitions
      .map((item) => {
        const badge = earned.find((x) => x.key === item.key);
        return `<div class="achievement ${badge ? "earned" : ""}" title="${esc(item.description)}"><strong>${esc(badge?.icon || item.icon)}</strong><span>${esc(item.name)}</span><small>${badge ? `Earned ${esc(badge.earned_at)}` : "Next milestone"}</small></div>`;
      })
      .join(
        "",
      )}</div>${hiddenCount > 0 ? `<p class="badge-more">+ ${hiddenCount} more milestones unlock as you progress</p>` : ""}`;
    const anchor = root.querySelector(".stats");
    root.insertBefore(card, anchor || root.firstChild);
  }
  function trendChart(activity) {
    const done = activity.filter((s) => s.status === "completed");
    if (done.length < 2)
      return `<div class="empty" style="margin-top:16px;align-items:center;text-align:center"><h3>No performance curve yet</h3><p>Complete two rehearsals and this ruled chart will trace how your scores move.</p><a class="btn small" href="/practice" style="margin-top:14px">Start your first session</a></div>`;
    return `<div class="chart" style="margin-top:16px">${done
      .slice(-8)
      .map(
        (s, i) =>
          `<div class="bar" style="height:${Math.max(8, Number(s.percentage_score || 0))}%"><label>${i + 1}</label></div>`,
      )
      .join("")}</div>`;
  }
  function performanceNotebook(analytics = {}) {
    const trend = analytics.score_trend || [],
      distribution = analytics.distribution || {};
    const distributionEntries = Object.entries(distribution).filter(
      ([, count]) => Number(count) > 0,
    );
    const max = Math.max(
      1,
      ...distributionEntries.map(([, count]) => Number(count)),
    );
    const strengths = analytics.strengths || [],
      improvements = analytics.areas_to_improve || [];
    const advice = analytics.recommendations || [
      "Complete a rehearsal to start building your performance notebook.",
    ];
    const bars =
      distributionEntries
        .map(
          ([mode, count]) =>
            `<div class="mini-bar-row"><span>${esc(modeLabel(mode))}</span><div class="mini-bar"><i style="width:${Math.max(4, (Number(count) / max) * 100)}%"></i></div><strong>${count}</strong></div>`,
        )
        .join("") ||
      `<span class="muted">No sessions yet. Your practice mix appears after your first rehearsal.</span>`;
    const trendHtml = trend.length
      ? trend
          .slice(-8)
          .map(
            (item) =>
              `<span class="trend-dot" title="${esc(item.title)} — ${fmt(item.score)}%"><i style="height:${Math.max(8, Number(item.score || 0))}%"></i><small>${esc(item.date)}</small></span>`,
          )
          .join("")
      : `<span class="muted">No sessions yet. Your score curve appears after a completed rehearsal.</span>`;
    const rubric = (items, empty) =>
      items.length
        ? items
            .map(
              (item) =>
                `<li><span>${esc(item.criterion)}</span><strong>${fmt(item.score, 1)}/10</strong></li>`,
            )
            .join("")
        : `<li class="muted">${empty}</li>`;
    return `<div class="notebook-grid" style="margin-top:18px"><div><small class="label">Score trend</small><div class="trend-strip">${trendHtml}</div></div><div><small class="label">Room distribution</small><div class="mini-bars">${bars}</div></div><div><small class="label">Strongest signals</small><ul class="insight-list">${rubric(strengths, "Keep practising to reveal your strongest areas.")}</ul></div><div><small class="label">Next focus</small><ul class="insight-list">${rubric(improvements, "Your coach will suggest a focus after a scored room.")}</ul></div></div><div class="notice" style="margin-top:18px"><strong>Coach's next note</strong><ul class="list" style="margin-top:8px">${advice.map((item) => `<li>→ ${esc(item)}</li>`).join("")}</ul></div>`;
  }
  function bindHome() {
    $('[data-action="toggle-brief"]')?.addEventListener("click", () =>
      $("#brief-form").classList.toggle("hidden"),
    );
    $("#brief-save")?.addEventListener("submit", async (e) => {
      e.preventDefault();
      const form = e.currentTarget,
        data = Object.fromEntries(new FormData(form).entries());
      data.preferred_modes = [
        ...form.querySelectorAll('[name="preferred_modes"]:checked'),
      ].map((x) => x.value);
      try {
        await api("/api/onboarding", {
          method: "POST",
          body: {
            ...data,
            weekly_target: Number(data.weekly_target),
            weekly_minutes: Number(data.weekly_minutes),
          },
        });
        toast("Your preparation brief is saved.");
        await loadHome();
      } catch (err) {
        toast(errorText(err), true);
      }
    });
    $('[data-action="new-goal"]')?.addEventListener("click", () =>
      showGoalForm(),
    );
    $('[data-action="new-task"]')?.addEventListener("click", () =>
      showTaskForm(),
    );
    $$('[data-action="edit-goal"]').forEach(
      (btn) =>
        (btn.onclick = () => {
          const goal = state.home.goals.find(
            (g) => String(g.id) === btn.dataset.id,
          );
          showGoalForm(goal);
        }),
    );
    $$('[data-action="goal-status"]').forEach(
      (btn) =>
        (btn.onclick = async () => {
          try {
            await api(`/api/goals/${btn.dataset.id}`, {
              method: "PATCH",
              body: { status: btn.dataset.status },
            });
            toast("Goal updated.");
            await loadHome();
          } catch (err) {
            toast(errorText(err), true);
          }
        }),
    );
    $$('[data-action="task-status"]').forEach(
      (btn) =>
        (btn.onclick = async () => {
          try {
            await api(`/api/tasks/${btn.dataset.id}`, {
              method: "PATCH",
              body: { status: btn.dataset.status },
            });
            toast("Task updated.");
            await loadHome();
          } catch (err) {
            toast(errorText(err), true);
          }
        }),
    );
  }
  function showGoalForm(goal = {}) {
    const root = $("#goal-form");
    if (!root) return;
    root.innerHTML = `<form id="goal-save" class="card soft-card" style="margin-top:16px"><div class="form-grid">${field("Goal title", "title", { value: goal.title || "", placeholder: "Land the analyst interview" })}${field("Practice mode", "mode", { select: true, required: false, value: goal.mode || "", options: [["", "Any mode"], ...Object.entries(modes).map(([k, v]) => [k, v.short])] })}${field("Sessions to log", "target_sessions", { type: "number", value: goal.target_sessions || 5 })}${field("Target score (%)", "target_score", { type: "number", required: false, value: goal.target_score || "" })}${field("Due date", "due_date", { type: "date", required: false, value: goal.due_date || "" })}${field("Note to yourself", "description", { required: false, value: goal.description || "", placeholder: "Why this goal matters" })}</div><div class="row" style="margin-top:16px"><button class="btn" type="submit">Save goal</button><button class="btn secondary" type="button" data-action="cancel-form">Cancel</button></div></form>`;
    $("#goal-save").onsubmit = async (e) => {
      e.preventDefault();
      const data = Object.fromEntries(new FormData(e.currentTarget).entries());
      try {
        await api(goal.id ? `/api/goals/${goal.id}` : "/api/goals", {
          method: goal.id ? "PUT" : "POST",
          body: {
            ...data,
            target_sessions: Number(data.target_sessions),
            target_score: Number(data.target_score || 0),
            priority: 2,
          },
        });
        toast("Goal saved.");
        await loadHome();
      } catch (err) {
        toast(errorText(err), true);
      }
    };
    $('[data-action="cancel-form"]', root).onclick = () =>
      (root.innerHTML = "");
  }
  function showTaskForm() {
    const root = $("#task-form");
    if (!root) return;
    root.innerHTML = `<form id="task-save" class="card soft-card" style="margin-top:16px"><div class="form-grid">${field("Task", "title", { placeholder: "Rehearse three behavioural answers" })}${field("Mode", "mode", { select: true, required: false, options: [["", "Any mode"], ...Object.entries(modes).map(([k, v]) => [k, v.short])] })}${field("Do it on", "due_date", { type: "date", required: false })}${field("Estimated minutes", "estimated_minutes", { type: "number", value: 20 })}</div><div class="row" style="margin-top:16px"><button class="btn" type="submit">Add task</button><button class="btn secondary" type="button" data-action="cancel-form">Cancel</button></div></form>`;
    $("#task-save").onsubmit = async (e) => {
      e.preventDefault();
      const data = Object.fromEntries(new FormData(e.currentTarget).entries());
      try {
        await api("/api/tasks", {
          method: "POST",
          body: {
            ...data,
            estimated_minutes: Number(data.estimated_minutes || 20),
            priority: 2,
            details: "",
          },
        });
        toast("Task added.");
        await loadHome();
      } catch (err) {
        toast(errorText(err), true);
      }
    };
    $('[data-action="cancel-form"]', root).onclick = () =>
      (root.innerHTML = "");
  }

  function practicePage() {
    render(
      `<div class="container page-pad"><div class="eyebrow">Practice studio</div><h1 style="font-size:clamp(31px,4vw,46px);margin-top:8px">Choose a room, then rehearse for real</h1><p class="body-copy" style="max-width:720px;margin-top:10px">Four rooms, one coach. Every prompt is generated for your brief, every answer is scored against a rubric, and every sitting is saved so you can pause and pick it up later.</p><div id="practice-content" class="loading">Loading the practice studio…</div></div>`,
      "practice",
    );
    loadPracticeSetup();
  }
  async function loadPracticeSetup() {
    try {
      state.docs = listItems(await api("/api/documents"), "documents");
      const history = await api("/api/practice");
      const open = history.sessions.find(
        (s) => s.is_resumable && !s.live_state,
      );
      drawPracticeSetup(open);
    } catch (err) {
      $("#practice-content").innerHTML = alertBox(errorText(err));
    }
  }
  function setupFields(mode = "interview") {
    if (mode === "interview")
      return `<div class="form-grid">${field("Target role", "role_target", { placeholder: "e.g. Graduate data analyst" })}${field("Target company", "company", { required: false, placeholder: "e.g. Northwind Labs (optional)" })}</div><div class="form-grid">${field(
        "Interview type",
        "interview_type",
        {
          select: true,
          value: "mixed",
          options: [
            ["mixed", "Mixed"],
            ["behavioral", "Behavioral"],
            ["technical", "Technical"],
            ["case", "Case / situational"],
          ],
        },
      )}${field(
        "Interview length",
        "interview_minutes",
        {
          select: true,
          value: "5",
          options: [
            ["1", "1 minute"],
            ["3", "3 minutes"],
            ["5", "5 minutes"],
            ["7", "7 minutes"],
            ["10", "10 minutes"],
          ],
        },
      )}</div><div class="form-grid">${field("Interviewer", "interviewer_id", { select: true, value: "lisa", options: Object.entries(interviewers).map(([key, item]) => [key, item.name]) })}${field("Focus topic", "topic", { required: false, placeholder: "e.g. SQL, stakeholder communication" })}</div>${field("Projects or experience to probe", "project_info", { textarea: true, required: false, rows: 3, placeholder: "A project, internship, achievement or technical area you want the interviewer to probe…" })}`;
    if (mode === "group_discussion")
      return `<div class="form-grid">${field("Discussion topic", "topic", { placeholder: "e.g. Should AI grade exams?" })}${field("Other participants", "participants", { required: false, placeholder: "e.g. 4 or Maya, Arjun, Zoya", hint: "Use a number for a generated panel or names for a personalised one." })}</div>`;
    if (mode === "debate")
      return `<div class="form-grid">${field("Motion", "topic", { placeholder: "e.g. This house would ban private tutoring" })}${field(
        "Your side",
        "side",
        {
          select: true,
          value: "proposition",
          options: [
            ["proposition", "Proposition"],
            ["opposition", "Opposition"],
          ],
        },
      )}${field("Format", "debate_format", {
        select: true,
        value: "british_parliamentary",
        options: [
          ["british_parliamentary", "British Parliamentary"],
          ["public_forum", "Public Forum"],
          ["oxford_style", "Oxford-style"],
          ["asian_parliamentary", "Asian Parliamentary"],
          ["worlds_schools", "Worlds Schools"],
        ],
      })}${field("Rounds", "rounds", { type: "number", value: 3, hint: "Between 1 and 6." })}</div>`;
    return `<div class="form-grid">${field("Coaching focus", "focus_area", { placeholder: "e.g. Handling nerves in final rounds" })}${field("Related topic", "topic", { required: false, placeholder: "e.g. Consulting case prep (optional)" })}</div>`;
  }
  function languageOptions(selected = "en") {
    return Object.entries(
      window.PREPSAGE_LANGUAGES || { en: { name: "English" } },
    )
      .map(
        ([key, item]) =>
          `<option value="${key}" ${key === selected ? "selected" : ""}>${esc(item.name || key)}</option>`,
      )
      .join("");
  }
  function drawPracticeSetup(open) {
    $("#practice-content").innerHTML =
      `${open ? `<div class="notice amber" style="margin-top:22px">↺ <strong>You have a session still open</strong> — ${esc(open.title)} <a class="link" href="/practice/${encodeURIComponent(open.public_id)}" style="margin-left:10px">Resume →</a></div>` : ""}
       <div class="practice-layout">
         <div>
           <section class="card">
             <div class="section-head"><div class="eyebrow">Step one</div><h2 style="margin-top:7px">Pick your room</h2></div>
             <div class="mode-grid">${Object.entries(modes)
               .map(
                 ([key, m], i) =>
                   `<button type="button" class="mode-btn ${i === 0 ? "active" : ""}" data-mode="${key}">${m.icon}&nbsp; ${m.label}</button>`,
               )
               .join("")}</div>
             <p id="mode-blurb" class="body-copy" style="margin-top:15px;font-size:13px">${modes.interview.blurb}</p>
             <div id="room" class="room"><span class="room-icon">${modes.interview.icon}</span><strong>${modes.interview.label}</strong><p>${modes.interview.blurb}</p></div>
           </section>
           <section class="card" style="margin-top:20px">
             <div class="section-head"><div class="eyebrow">Step two</div><h2 style="margin-top:7px">Set the brief</h2></div>
             <form id="setup-form" class="setup-form">
               <div id="setup-fields">${setupFields()}</div>
               <div class="form-grid">
                 ${field("Difficulty", "difficulty", {
                   select: true,
                   value: "intermediate",
                   options: [
                     ["beginner", "Beginner"],
                     ["intermediate", "Intermediate"],
                     ["advanced", "Advanced"],
                     ["expert", "Expert"],
                   ],
                 })}
                 ${field("Response language", "language", { select: true, value: "en", options: Object.entries(window.PREPSAGE_LANGUAGES || { en: { name: "English" } }).map(([key, item]) => [key, item.name || key]) })}
               </div>
               ${field("Anything else your coach should know", "context_notes", { textarea: true, required: false, placeholder: "Job description highlights, the panel's background, a weakness to probe…" })}
               <div id="context-guidance" class="notice context-guidance" aria-live="polite"><strong>Coach's setup note</strong><p>Add a role, job description, panel context, or weakness above. A tailored coaching note will appear here.</p></div>
               <div class="row between" style="margin-top:18px"><span class="notice">✦ Your context will shape the prompts and feedback saved with this rehearsal.</span><button class="btn" type="submit">Enter the rehearsal room&nbsp; →</button></div>
             </form>
           </section>
         </div>
         <section class="card soft-card">
           <div class="section-head"><div class="eyebrow">Optional context</div><h2 style="margin-top:7px;font-size:23px">Bring your résumé or notes</h2><p>PDF, DOCX or TXT up to 2 MB. Your document stays on your account.</p></div>
           <form id="upload-form" class="upload" style="margin-top:16px"><input id="document-file" type="file" accept=".pdf,.docx,.txt" hidden><label for="document-file" style="cursor:pointer"><strong>☁ Drop a file here or click to choose</strong><span>PDF · DOCX · TXT — max 2 MB</span></label></form>
           <div id="doc-list" class="doc-list">${state.docs.length ? state.docs.map(docChip).join("") : `<span class="field-hint">No documents yet — this step is entirely optional.</span>`}</div>
         </section>
       </div>`;
    let currentMode = "interview";
    $$("[data-mode]").forEach((btn) =>
      btn.setAttribute(
        "aria-pressed",
        String(btn.classList.contains("active")),
      ),
    );
    $$("[data-mode]").forEach(
      (btn) =>
        (btn.onclick = () => {
          currentMode = btn.dataset.mode;
          $$("[data-mode]").forEach((button) => {
            button.classList.toggle("active", button === btn);
            button.setAttribute("aria-pressed", String(button === btn));
          });
          $("#mode-blurb").textContent = modes[currentMode].blurb;
          $("#room").innerHTML =
            `<span class="room-icon">${modes[currentMode].icon}</span><strong>${modes[currentMode].label}</strong><p>${modes[currentMode].blurb}</p>`;
          $("#setup-fields").innerHTML = setupFields(currentMode);
          requestContextGuidance();
        }),
    );
    let guidanceTimer = null,
      guidanceRequest = 0;
    let lastGuidance = "";
    const requestContextGuidance = async (force = false) => {
      const input = $("#context_notes"),
        preview = $("#context-guidance");
      if (!input || !preview) return;
      const context = input.value.trim();
      if (context.length < 8) {
        preview.innerHTML =
          "<strong>Coach's setup note</strong><p>Add a role, job description, panel context, or weakness above. A tailored coaching note will appear here.</p>";
        return;
      }
      const requestId = ++guidanceRequest;
      preview.innerHTML =
        "<strong>Coach is reading your context…</strong><p>Preparing a tailored note for this rehearsal.</p>";
      const values = Object.fromEntries(
        new FormData($("#setup-form")).entries(),
      );
      try {
        const result = await api("/api/context-guidance", {
          method: "POST",
          body: {
            ...values,
            mode: currentMode,
            context_notes: context,
            previous_guidance: lastGuidance,
            variation: Date.now(),
          },
        });
        if (requestId !== guidanceRequest) return;
        lastGuidance = result.guidance || "";
        preview.innerHTML = `<strong>Coach's tailored note</strong><p>${esc(result.guidance)}</p><small class="field-hint">${result.provider === "groq" ? "Generated for this brief by your AI coach." : "Tailored locally from this brief while the AI provider is unavailable."}</small><button type="button" class="btn secondary small guidance-refresh" id="guidance-refresh">Generate another note</button>`;
        $("#guidance-refresh")?.addEventListener("click", () =>
          requestContextGuidance(true),
        );
      } catch (_) {
        if (requestId === guidanceRequest)
          preview.innerHTML =
            "<strong>Coach's setup note</strong><p>Your context will still shape the prompts and feedback in the rehearsal.</p>";
      }
    };
    $("#context_notes")?.addEventListener("input", () => {
      clearTimeout(guidanceTimer);
      guidanceTimer = setTimeout(requestContextGuidance, 650);
    });
    $("#context_notes")?.addEventListener("blur", requestContextGuidance);
    $("#upload-form").onsubmit = (e) => e.preventDefault();
    $("#document-file").onchange = async (e) => {
      const selected = e.target.files[0];
      if (!selected) return;
      const allowed = [".pdf", ".docx", ".txt"];
      const extension = `.${selected.name.split(".").pop().toLowerCase()}`;
      if (!allowed.includes(extension) || selected.size > 2 * 1024 * 1024) {
        e.target.value = "";
        return toast(
          "Choose a PDF, DOCX or TXT file no larger than 2 MB.",
          true,
        );
      }
      const data = new FormData();
      data.append("file", selected);
      try {
        const result = await api("/api/documents", {
          method: "POST",
          body: data,
        });
        toast(result.message);
        state.docs = listItems(await api("/api/documents"), "documents");
        $("#doc-list").innerHTML = state.docs.map(docChip).join("");
      } catch (err) {
        toast(errorText(err), true);
      }
    };
    $("#setup-form").onsubmit = async (e) => {
      e.preventDefault();
      const data = Object.fromEntries(new FormData(e.currentTarget).entries());
      data.mode = currentMode;
      data.question_count = defaultQuestionCount;
      data.rounds = Number(data.rounds || 3);
      const selected = $(".doc-chip.selected");
      data.document_id = selected ? Number(selected.dataset.id) : 0;
      const button = $("button[type=submit]", e.currentTarget);
      button.disabled = true;
      button.textContent = "Writing your prompts…";
      try {
        const result = await api("/api/practice", {
          method: "POST",
          body: data,
        });
        location.href = `/practice/${encodeURIComponent(result.public_id)}`;
      } catch (err) {
        button.disabled = false;
        button.textContent = "Enter the rehearsal room →";
        toast(errorText(err), true);
      }
    };
    $$(".doc-chip").forEach(
      (btn) =>
        (btn.onclick = () => {
          $$(".doc-chip").forEach((other) =>
            other.classList.toggle("selected", other === btn),
          );
        }),
    );
  }
  function docChip(doc) {
    const ready = doc.scan_status === "extracted";
    return `<button type="button" class="doc-chip${ready ? "" : " disabled"}" data-id="${doc.id}" ${ready ? "" : "disabled"}>▧ ${esc(doc.name)} <small>${ready ? `${doc.size_kb} KB` : "unavailable — extraction failed"}</small></button>`;
  }

  let interviewClockTimer = null;
  let interviewClockSession = "";
  let interviewStartedAt = 0;
  let interviewTabSwitches = 0;
  let interviewVisibilityHandler = null;
  let interviewAnswerStartedAt = 0;
  let interviewCameraStream = null;
  let interviewCameraSession = "";
  let interviewCameraRequestId = 0;
  let interviewCameraStarting = false;

  function setInterviewPresence(label, mode = "ready") {
    const status = $("#interview-ai-status");
    if (!status) return;
    status.textContent = label;
    status.dataset.state = mode;
    const panel = $(".interview-ai-center");
    if (panel) panel.dataset.state = mode;
    const presence = $(".ai-speaking-pill");
    if (presence) presence.dataset.state = mode;
  }

  function releaseInterviewCamera() {
    interviewCameraRequestId += 1;
    interviewCameraStarting = false;
    if (interviewCameraStream) {
      interviewCameraStream.getTracks().forEach((track) => track.stop());
    }
    interviewCameraStream = null;
    interviewCameraSession = "";
  }

  function setCameraMessage(title, detail, isError = false) {
    const placeholder = $("#interview-camera-placeholder");
    const message = $("#interview-camera-message");
    const hint = $("#interview-camera-hint");
    const retry = $("#interview-camera-enable");
    if (message) message.textContent = title;
    if (hint) hint.textContent = detail;
    if (placeholder) {
      placeholder.classList.remove("hidden");
      placeholder.classList.toggle("has-error", isError);
    }
    if (retry) {
      retry.disabled = interviewCameraStarting;
      retry.textContent = interviewCameraStarting
        ? "Requesting camera…"
        : isError
          ? "Try camera again"
          : "Enable camera";
    }
  }

  async function requestInterviewCamera(sessionId) {
    const video = $("#interview-camera-video");
    const placeholder = $("#interview-camera-placeholder");
    if (!video || !placeholder) return;

    if (
      interviewCameraStream &&
      interviewCameraSession &&
      interviewCameraSession !== sessionId
    )
      releaseInterviewCamera();

    if (interviewCameraStream) {
      video.srcObject = interviewCameraStream;
      video.classList.add("is-ready");
      const track = interviewCameraStream.getVideoTracks()[0];
      const isOn = Boolean(track?.enabled && track.readyState === "live");
      const stage = $("#interview-camera-stage");
      const offState = $("#interview-camera-off-state");
      placeholder.classList.add("hidden");
      stage?.classList.toggle("camera-is-off", !isOn);
      offState?.classList.toggle("hidden", isOn);
      const toggle = $("#interview-camera-toggle");
      const status = $("#interview-camera-status");
      if (toggle) {
        toggle.disabled = false;
        toggle.setAttribute("aria-pressed", String(isOn));
        toggle.setAttribute(
          "aria-label",
          isOn ? "Turn camera off" : "Turn camera on",
        );
        toggle.textContent = "◉";
      }
      if (status) status.textContent = isOn ? "Camera on" : "Camera off";
      video.play().catch(() => {});
      return;
    }

    if (interviewCameraStarting) return;
    if (!navigator.mediaDevices?.getUserMedia) {
      const inFrame = window.self !== window.top;
      setCameraMessage(
        "Camera access is unavailable here",
        !window.isSecureContext
          ? "Camera access requires a secure HTTPS connection. You can still answer by voice or by typing."
          : inFrame
            ? "This browser or preview frame has not enabled camera access. Open the preview in a browser tab or check its site permissions."
            : "Use a current browser with camera access enabled, or continue with audio and typed answers.",
        true,
      );
      return;
    }

    interviewCameraStarting = true;
    interviewCameraSession = sessionId;
    const requestId = ++interviewCameraRequestId;
    setCameraMessage(
      "Connecting to your camera…",
      "Your browser will ask for permission. The camera is only used for this interview preview.",
    );
    const toggle = $("#interview-camera-toggle");
    if (toggle) toggle.disabled = true;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: { ideal: "user" } },
        audio: false,
      });
      if (
        requestId !== interviewCameraRequestId ||
        interviewCameraSession !== sessionId ||
        !$("#interview-camera-video")
      ) {
        stream.getTracks().forEach((track) => track.stop());
        return;
      }
      interviewCameraStream = stream;
      interviewCameraStarting = false;
      const activeVideo = $("#interview-camera-video");
      const activePlaceholder = $("#interview-camera-placeholder");
      activeVideo.srcObject = stream;
      activeVideo.classList.add("is-ready");
      activePlaceholder.classList.add("hidden");
      stream.getVideoTracks()[0]?.addEventListener(
        "ended",
        () => {
          if (interviewCameraStream !== stream) return;
          interviewCameraStream = null;
          interviewCameraSession = "";
          const currentVideo = $("#interview-camera-video");
          currentVideo?.classList.remove("is-ready");
          $("#interview-camera-stage")?.classList.remove("camera-is-off");
          $("#interview-camera-off-state")?.classList.add("hidden");
          const activeToggle = $("#interview-camera-toggle");
          if (activeToggle) activeToggle.disabled = true;
          setCameraMessage(
            "Camera disconnected",
            "Reconnect your camera to show your live video. Audio and typed answers still work.",
            true,
          );
        },
        { once: true },
      );
      try {
        await activeVideo.play();
      } catch (_) {
        // Muted inline video normally autoplays; the preview remains available
        // if this browser requires a user gesture to begin playback.
      }
      const activeToggle = $("#interview-camera-toggle");
      const status = $("#interview-camera-status");
      if (activeToggle) {
        activeToggle.disabled = false;
        activeToggle.setAttribute("aria-pressed", "true");
        activeToggle.setAttribute("aria-label", "Turn camera off");
        activeToggle.textContent = "◉";
      }
      if (status) status.textContent = "Camera on";
      $("#interview-camera-stage")?.classList.remove("camera-is-off");
      $("#interview-camera-off-state")?.classList.add("hidden");
    } catch (error) {
      if (requestId !== interviewCameraRequestId) return;
      interviewCameraStarting = false;
      interviewCameraSession = "";
      const messages = {
        NotAllowedError:
          "Allow camera access for this site or Replit preview in your browser settings, then try again.",
        NotFoundError:
          "No camera was found. You can still answer by voice or by typing.",
        NotReadableError:
          "The camera is busy in another app. Close that app and try again.",
        OverconstrainedError:
          "This camera does not support the requested video mode. Try again.",
        SecurityError:
          "Camera access is blocked by this page or its preview frame. Open the preview in a browser tab or check browser permissions.",
      };
      setCameraMessage(
        "We couldn’t start your camera",
        messages[error?.name] ||
          "Check your camera connection and browser permission, then try again. You can continue without video.",
        true,
      );
      const activeToggle = $("#interview-camera-toggle");
      if (activeToggle) activeToggle.disabled = true;
    }
  }

  function bindInterviewCameraControls(sessionId) {
    $("#interview-camera-enable")?.addEventListener("click", () =>
      requestInterviewCamera(sessionId),
    );
    $("#interview-camera-toggle")?.addEventListener("click", () => {
      const track = interviewCameraStream?.getVideoTracks()[0];
      if (!track) {
        requestInterviewCamera(sessionId);
        return;
      }
      track.enabled = !track.enabled;
      const isOn = track.enabled;
      const toggle = $("#interview-camera-toggle");
      if (toggle) {
        toggle.setAttribute("aria-pressed", String(isOn));
        toggle.setAttribute(
          "aria-label",
          isOn ? "Turn camera off" : "Turn camera on",
        );
        toggle.textContent = "◉";
      }
      const status = $("#interview-camera-status");
      if (status) status.textContent = isOn ? "Camera on" : "Camera off";
      $("#interview-camera-stage")?.classList.toggle("camera-is-off", !isOn);
      $("#interview-camera-off-state")?.classList.toggle("hidden", isOn);
    });
    $("#interview-mic-shortcut")?.addEventListener("click", () =>
      $("#interview-voice")?.click(),
    );
    const settingsToggle = $("#interview-camera-settings-toggle");
    const settingsPanel = $("#interview-camera-settings");
    settingsToggle?.addEventListener("click", () => {
      const open = settingsPanel?.classList.contains("hidden");
      settingsPanel?.classList.toggle("hidden", !open);
      settingsToggle.setAttribute("aria-expanded", String(Boolean(open)));
    });
  }

  window.addEventListener("pagehide", releaseInterviewCamera);

  async function sessionPage(publicId) {
    render(
      `<div class="container interview-page page-pad"><div id="session-content" class="loading">Opening your rehearsal room…</div></div>`,
      "practice",
    );
    try {
      state.session = await api(
        `/api/practice/${encodeURIComponent(publicId)}`,
      );
      if (state.session.live_state?.kind) {
        state.live = {
          public_id: publicId,
          kind: state.session.live_state.kind,
          state: state.session.live_state,
        };
        $("#session-content").id = "arena-content";
        drawLiveArena();
      } else if (state.session.mode === "interview")
        drawInterviewSession(publicId, true);
      else drawSession(publicId);
    } catch (err) {
      $("#session-content").innerHTML =
        alertBox(errorText(err)) +
        `<a class="btn" href="/practice" style="margin-top:18px">← Back to the studio</a>`;
    }
  }

  function interviewMessageHtml(messages = []) {
    return messages
      .map((item, index) => {
        const user = item.speaker === "user" || item.speaker === "You";
        return `<div class="chat-msg ${user ? "user" : "assistant"}"><small>${esc(user ? "You" : item.speaker || "Interviewer")}</small><p>${esc(item.content)}</p>${!user ? `<button type="button" class="btn secondary small interview-speak" data-index="${index}">Listen</button>` : ""}</div>`;
      })
      .join("");
  }

  function setInterviewRoomChrome(activeRoom, paused, completed) {
    const pageShell = $(".page-shell");
    const nav = $(".topbar-inner > nav");
    if (!pageShell || !nav) return;
    pageShell.classList.add("interview-focus-mode");
    nav.classList.add("interview-topbar-controls");
    nav.setAttribute("aria-label", "Interview controls");
    nav.innerHTML = activeRoom
      ? `<div class="interview-topbar-status"><div class="interview-topbar-timer"><span>Time remaining</span><strong id="interview-clock">00:00</strong></div><button class="btn danger small" type="button" id="finish-interview">End interview</button></div>`
      : `<div class="interview-topbar-status"><span class="interview-topbar-state">${paused ? "Interview paused" : completed ? "Interview complete" : "Interview"}</span></div>`;
  }

  function drawInterviewSession(publicId, autoSpeak = false) {
    const s = state.session || {};
    const current = Number(s.current_index || 0);
    const total = s.questions?.length || s.question_count || 0;
    const interviewer = s.interviewer || interviewers.lisa;
    const question = s.questions?.[current] || {};
    const messages = s.interview_messages || [];
    const completed = s.status === "completed" || current >= total;
    const paused = s.status === "paused";
    const activeRoom = !paused && !completed;
    if (!activeRoom) releaseInterviewCamera();
    setInterviewRoomChrome(activeRoom, paused, completed);

    const questionCard = completed
      ? `<section class="card interview-current-question is-complete"><div><small>Interview complete</small><h2>Thank you for your time.</h2><p>Your transcript, question-by-question feedback and readiness report are ready.</p></div></section>`
      : `<section class="card interview-current-question"><div class="current-question-top"><small>Current question · ${Math.min(current + 1, total)} of ${total}</small><span class="tag">${esc(question.focus || "Interview")}</span></div><h2>${esc(question.prompt || "Your next question is loading.")}</h2>${activeRoom ? `<button type="button" class="btn secondary small" id="interview-listen-question">▶ Hear question</button>` : ""}</section>`;

    const cameraPanel = activeRoom
      ? `<div class="camera-stage" id="interview-camera-stage">
          <video id="interview-camera-video" class="interview-camera-video" autoplay muted playsinline aria-label="Your live camera preview"></video>
          <div id="interview-camera-placeholder" class="camera-placeholder" role="status" aria-live="polite">
            <span class="camera-placeholder-icon" aria-hidden="true">◉</span>
            <strong id="interview-camera-message">Connecting to your camera…</strong>
            <p id="interview-camera-hint">Your browser will ask for permission. This video stays in your browser and is not sent to the interviewer.</p>
            <button type="button" class="btn secondary small" id="interview-camera-enable">Enable camera</button>
          </div>
          <div id="interview-camera-off-state" class="camera-off-state hidden">
            <span class="camera-placeholder-icon" aria-hidden="true">◉</span>
            <strong>Your camera is off</strong>
            <span>Turn it back on whenever you’re ready.</span>
          </div>
          <div class="camera-overlay">
            <span class="camera-self-label"><i></i> You</span>
            <span id="interview-camera-status" class="camera-status">Camera connecting</span>
            <div class="camera-controls">
              <button type="button" class="camera-control-button" id="interview-mic-shortcut" aria-label="Start recording your answer" title="Speak answer">🎙</button>
              <button type="button" class="camera-control-button" id="interview-camera-toggle" aria-label="Enable camera" aria-pressed="false" disabled title="Camera">◉</button>
              <button type="button" class="camera-control-button" id="interview-camera-settings-toggle" aria-label="Open interview settings" aria-expanded="false" aria-controls="interview-camera-settings" title="Settings">⚙</button>
            </div>
          </div>
          <div id="interview-camera-settings" class="camera-settings hidden" role="dialog" aria-label="Interview settings">
            <strong>Room settings</strong>
            <p>Camera permission is controlled by your browser. In Replit Preview, allow camera access for the preview site. The video is local to this browser.</p>
            <button type="button" class="btn secondary small" id="pause-interview">Ⅱ Pause and save</button>
          </div>
        </div>`
      : `<div class="camera-stage camera-paused">
          <div class="camera-placeholder">
            <span class="camera-placeholder-icon" aria-hidden="true">◉</span>
            <strong>${paused ? "Camera paused" : "Interview finished"}</strong>
            <p>${paused ? "Your camera is off while the interview is paused." : "Your camera is no longer active."}</p>
          </div>
        </div>`;

    const roomControls = paused
      ? `<div class="interview-state-actions"><button class="btn" data-action="resume-interview">▶ Resume interview</button><button class="btn secondary" data-action="abandon-interview">Archive interview</button></div>`
      : completed
        ? `<a class="btn" href="/history/${encodeURIComponent(publicId)}">Open the interview report&nbsp; →</a>`
        : "";

    const responsePanel = activeRoom
      ? `<form id="interview-answer-form" class="card interview-answer-dock">
          <div class="interview-answer-form">
            <div class="interview-answer-modes" role="group" aria-label="Choose how to answer">
              <button type="button" class="answer-mode-tab is-active" id="interview-mode-voice" aria-pressed="true">🎙 Speak Answer</button>
              <button type="button" class="answer-mode-tab" id="interview-type-toggle" aria-expanded="false" aria-controls="interview-text-fallback" aria-pressed="false">⌨ Type Answer</button>
            </div>
            <div class="interview-composer" id="interview-composer" data-mode="voice">
              <div class="interview-voice-controls">
                <button type="button" class="btn teal interview-record-button" id="interview-voice" aria-label="Start recording your answer">🎙 Start speaking</button>
                <p id="interview-voice-status" class="field-hint interview-voice-status" role="status" aria-live="polite">Let the interviewer finish, then tap to record. Review the transcript before sending.</p>
              </div>
              <div id="interview-voice-preview" class="voice-preview interview-voice-preview hidden">
                <label class="field"><span class="label">Review your transcript</span><textarea class="textarea" id="interview-voice-text" rows="2" aria-label="Review and edit the Groq transcript"></textarea></label>
                <div class="interview-voice-preview-actions"><button type="button" class="btn secondary small" id="interview-voice-retry">Record again</button><button type="button" class="answer-send-button" id="interview-voice-use" aria-label="Send reviewed spoken answer" title="Send answer">↑</button></div>
              </div>
              <section id="interview-text-fallback" class="interview-type-answer hidden">
                <label class="field"><span class="label">Your response</span><textarea class="textarea" id="interview-answer" rows="2" placeholder="Type your answer…" aria-label="Type your interview response"></textarea><span class="field-hint" id="interview-word-count">0 words</span></label>
                <button class="answer-send-button" id="interview-send-text" type="submit" aria-label="Send typed answer" title="Send answer">↑</button>
              </section>
            </div>
          </div>
        </form>`
      : `<section class="card interview-answer-dock interview-room-state"><div class="eyebrow">${paused ? "Interview paused" : "Interview complete"}</div><h3>${paused ? "Your place is checkpointed" : "Your report is ready"}</h3><p>${paused ? "Your transcript and timing are saved. Resume whenever you are ready." : "Your answers and feedback have been saved to this interview."}</p>${roomControls}</section>`;

    $("#session-content").innerHTML = `
      <div class="interview-provider-notice">${providerNotice(s)}</div>
      <div class="interview-workspace">
        <section class="card interview-camera-card" aria-label="Your camera">
          ${cameraPanel}
        </section>
        <section class="interview-ai-center" aria-label="AI interviewer" data-state="${paused ? "paused" : completed ? "complete" : "ready"}">
          <div class="ai-stage-heading"><span class="ai-stage-kicker">AI interviewer</span><strong>${esc(interviewer.name || "Your interviewer")}</strong></div>
          <div class="ai-ripple" aria-hidden="true">
            <span class="ai-ripple-ring"></span><span class="ai-ripple-ring"></span><span class="ai-ripple-ring"></span>
            <div class="ai-avatar">${esc(interviewer.initials || "AI")}</div>
          </div>
          <div class="ai-waveform" aria-hidden="true">${[3, 5, 8, 11, 7, 12, 9, 5, 10, 6, 4].map((height) => `<i style="--wave-height:${height}px"></i>`).join("")}</div>
          <div class="ai-speaking-pill"><span class="ai-presence-dot"></span><span id="interview-ai-status" data-state="${paused ? "paused" : completed ? "complete" : "ready"}">${activeRoom ? "Ready for your answer" : paused ? "Interview paused" : "Interview complete"}</span></div>
        </section>
        <aside class="interview-transcript-column" aria-label="Question and conversation transcript">
          ${questionCard}
          <section class="card interview-conversation-panel">
            <div class="interview-conversation-heading"><div><span class="eyebrow">Live transcript</span><h2>Conversation</h2></div><span class="interview-turn-count">${messages.length ? `${messages.length} ${messages.length === 1 ? "turn" : "turns"}` : "Just getting started"}</span></div>
            <div id="interview-transcript" class="chat-thread interview-transcript" aria-live="polite">${interviewMessageHtml(messages) || `<div class="interview-empty-transcript"><span aria-hidden="true">✦</span><p>Your conversation will appear here as you answer.</p></div>`}</div>
          </section>
        </aside>
      </div>
      ${responsePanel}`;

    bindInterviewRoom(publicId);
    if (activeRoom) {
      bindInterviewCameraControls(publicId);
      requestInterviewCamera(publicId);
      if (autoSpeak) {
        const hasCandidateAnswered = messages.some(
          (item) => item.speaker === "user" || item.speaker === "You",
        );
        const opening =
          !hasCandidateAnswered && current === 0
            ? messages.find(
                (item) => item.speaker !== "user" && item.speaker !== "You",
              )?.content
            : "";
        const interviewerPrompt = [opening, question.prompt]
          .map((part) => String(part || "").trim())
          .filter(Boolean)
          .join(" ");
        if (interviewerPrompt)
          speakText(
            interviewerPrompt,
            s.language || "en",
            interviewer.key || "lisa",
          );
      }
    }
    const transcript = $("#interview-transcript");
    if (transcript) transcript.scrollTop = transcript.scrollHeight;
  }

  async function finishInterview(publicId, forced = false) {
    if (!forced && !confirm("End this interview and write the report?")) return;
    clearInterval(interviewClockTimer);
    try {
      await api("/interview/end", {
        method: "POST",
        body: { public_id: publicId, tab_switch_count: interviewTabSwitches },
      });
      toast("Interview ended — your report is ready.");
      location.href = `/history/${encodeURIComponent(publicId)}`;
    } catch (err) {
      toast(errorText(err), true);
    }
  }

  function bindInterviewRoom(publicId) {
    const s = state.session || {};
    if (interviewClockSession !== publicId) {
      interviewClockSession = publicId;
      interviewStartedAt = Date.now() - Number(s.duration_seconds || 0) * 1000;
      interviewTabSwitches = Number(s.tab_switch_count || 0);
    }
    clearInterval(interviewClockTimer);
    const updateClock = () => {
      const limit = Number(s.time_limit_seconds || 0);
      const elapsed = Math.max(
        0,
        Math.floor((Date.now() - interviewStartedAt) / 1000),
      );
      const remaining = Math.max(0, limit - elapsed);
      const node = $("#interview-clock");
      if (node)
        node.textContent = `${String(Math.floor(remaining / 60)).padStart(2, "0")}:${String(remaining % 60).padStart(2, "0")}`;
      if (limit && remaining <= 0 && s.status === "in_progress") {
        clearInterval(interviewClockTimer);
        toast("Interview time is up. Writing your report…");
        finishInterview(publicId, true);
      }
    };
    updateClock();
    if (s.status === "in_progress")
      interviewClockTimer = setInterval(updateClock, 1000);
    if (interviewVisibilityHandler)
      document.removeEventListener(
        "visibilitychange",
        interviewVisibilityHandler,
      );
    interviewVisibilityHandler = () => {
      if (document.hidden && state.session?.status === "in_progress") {
        interviewTabSwitches += 1;
        toast(
          "Tab switch detected. Stay in the interview room to keep your transcript clean.",
          true,
        );
      }
    };
    document.addEventListener("visibilitychange", interviewVisibilityHandler);
    $$(".interview-speak").forEach(
      (button) =>
        (button.onclick = () =>
          speakText(
            (s.interview_messages || [])[Number(button.dataset.index)]?.content,
            s.language || "en",
            s.interviewer?.key || "lisa",
            true,
          )),
    );
    $("#interview-listen-question")?.addEventListener("click", () =>
      speakText(
        s.questions?.[Number(s.current_index || 0)]?.prompt,
        s.language || "en",
        s.interviewer?.key || "lisa",
        true,
      ),
    );
    const answer = $("#interview-answer");
    if (!answer) {
      $('[data-action="resume-interview"]')?.addEventListener(
        "click",
        async () => {
          try {
            await api(`/api/practice/${encodeURIComponent(publicId)}`, {
              method: "PATCH",
              body: {
                status: "in_progress",
                tab_switch_count: interviewTabSwitches,
              },
            });
            state.session = await api(
              `/api/practice/${encodeURIComponent(publicId)}`,
            );
            drawInterviewSession(publicId, true);
          } catch (err) {
            toast(errorText(err), true);
          }
        },
      );
      $('[data-action="abandon-interview"]')?.addEventListener(
        "click",
        async () => {
          if (!confirm("Archive this interview?")) return;
          try {
            await api(`/api/practice/${encodeURIComponent(publicId)}`, {
              method: "PATCH",
              body: {
                status: "abandoned",
                tab_switch_count: interviewTabSwitches,
              },
            });
            location.href = "/practice";
          } catch (err) {
            toast(errorText(err), true);
          }
        },
      );
      return;
    }
    interviewAnswerStartedAt = Date.now();
    let answerSubmitting = false;
    let recordingBusy = false;
    let pendingTurnIdempotencyKey = "";
    const voiceStatus = $("#interview-voice-status");
    const voiceButton = $("#interview-voice");
    const voiceSendButton = $("#interview-voice-use");
    const voiceModeButton = $("#interview-mode-voice");
    const cameraMicShortcut = $("#interview-mic-shortcut");
    const typedSendButton = $("#interview-send-text");
    const typedPanel = $("#interview-text-fallback");
    const typedToggle = $("#interview-type-toggle");
    const composer = $("#interview-composer");
    const pauseButton = $("#pause-interview");
    const finishButton = $("#finish-interview");
    const updateRoomActions = () => {
      const busy = answerSubmitting || recordingBusy;
      if (pauseButton) pauseButton.disabled = busy;
      if (finishButton) finishButton.disabled = busy;
    };
    const showTypedAnswer = (visible) => {
      typedPanel?.classList.toggle("hidden", !visible);
      answer.required = visible;
      typedToggle?.setAttribute("aria-expanded", String(visible));
      typedToggle?.setAttribute("aria-pressed", String(visible));
      voiceModeButton?.setAttribute("aria-pressed", String(!visible));
      typedToggle?.classList.toggle("is-active", visible);
      voiceModeButton?.classList.toggle("is-active", !visible);
      if (composer) composer.dataset.mode = visible ? "text" : "voice";
      if (visible) answer.focus();
    };
    typedToggle?.addEventListener("click", () => showTypedAnswer(true));
    voiceModeButton?.addEventListener("click", () => showTypedAnswer(false));
    answer.oninput = () => {
      const wordCount = $("#interview-word-count");
      if (wordCount)
        wordCount.textContent = `${answer.value.trim() ? answer.value.trim().split(/\s+/).length : 0} words`;
    };
    const sendInterviewAnswer = async (rawText, source) => {
      if (answerSubmitting) return false;
      const text = String(rawText || "").trim();
      if (text.length < 12) {
        toast("Give the interviewer a little more to work with.", true);
        return false;
      }
      if (!pendingTurnIdempotencyKey)
        pendingTurnIdempotencyKey =
          window.crypto?.randomUUID?.() ||
          `${publicId}-${Date.now()}-${Math.random().toString(36).slice(2)}`;
      answerSubmitting = true;
      updateRoomActions();
      if (voiceSendButton) voiceSendButton.disabled = true;
      if (typedSendButton) {
        typedSendButton.disabled = true;
        typedSendButton.textContent = "…";
      }
      if (voiceButton) voiceButton.disabled = true;
      if (voiceStatus)
        voiceStatus.textContent =
          source === "voice"
            ? "Transcript ready. Sending your spoken answer to the interviewer…"
            : "Sending your typed answer to the interviewer…";
      setInterviewPresence("Reviewing your answer…", "thinking");
      try {
        const result = await api(
          `/api/interview/${encodeURIComponent(publicId)}/turn`,
          {
            method: "POST",
            headers: { "Idempotency-Key": pendingTurnIdempotencyKey },
            body: {
              answer: text,
              source,
              response_seconds: Math.floor(
                (Date.now() - interviewAnswerStartedAt) / 1000,
              ),
              tab_switch_count: interviewTabSwitches,
            },
          },
        );
        pendingTurnIdempotencyKey = "";
        state.session = result.session;
        drawInterviewSession(publicId);
        const nextPrompt =
          !result.completed &&
          result.session?.questions?.[
            Number(result.session?.current_index || 0)
          ]?.prompt;
        let interviewerSpeech = String(result.interviewer_reply || "").trim();
        if (
          nextPrompt &&
          !interviewerSpeech
            .toLowerCase()
            .includes(String(nextPrompt).toLowerCase())
        )
          interviewerSpeech = `${interviewerSpeech} ${nextPrompt}`.trim();
        if (interviewerSpeech)
          speakText(
            interviewerSpeech,
            result.session?.language || s.language || "en",
            result.session?.interviewer?.key || s.interviewer?.key || "lisa",
          );
        if (result.completed)
          toast("Interview complete — your report is ready.");
        if (result.gamification?.points_earned)
          toast(`+${result.gamification.points_earned} points earned.`);
        return true;
      } catch (err) {
        if (voiceStatus)
          voiceStatus.textContent =
            "The answer is still here. Try sending it again or record a new response.";
        setInterviewPresence("Ready when you are", "ready");
        toast(errorText(err), true);
        return false;
      } finally {
        answerSubmitting = false;
        updateRoomActions();
        if (voiceSendButton) voiceSendButton.disabled = false;
        if (voiceButton) voiceButton.disabled = false;
        if (typedSendButton) {
          typedSendButton.disabled = false;
          typedSendButton.textContent = "↑";
        }
      }
    };
    bindVoiceRecorder(
      "#interview-answer",
      "#interview-voice",
      () => s.language || "en",
      {
        previewId: "#interview-voice-preview",
        textId: "#interview-voice-text",
        useId: "#interview-voice-use",
        retryId: "#interview-voice-retry",
        onBusyChange: (busy) => {
          recordingBusy = busy;
          updateRoomActions();
          if (cameraMicShortcut) {
            cameraMicShortcut.setAttribute("aria-pressed", String(busy));
            cameraMicShortcut.setAttribute(
              "aria-label",
              busy
                ? "Stop recording your answer"
                : "Start recording your answer",
            );
            cameraMicShortcut.textContent = busy ? "■" : "🎙";
          }
          const transcriptPreview = $("#interview-voice-preview");
          if (busy || transcriptPreview?.classList.contains("hidden"))
            setInterviewPresence(
              busy ? "Listening to your answer" : "Ready for your answer",
              busy ? "listening" : "ready",
            );
        },
        onReady: () => {
          if (voiceStatus)
            voiceStatus.textContent =
              "Transcript ready. Review or edit it, then send your spoken answer.";
          setInterviewPresence("Transcript ready to review", "ready");
        },
        onUse: (transcript) => sendInterviewAnswer(transcript, "voice"),
      },
    );
    $("#interview-answer-form").onsubmit = async (event) => {
      event.preventDefault();
      await sendInterviewAnswer(answer.value, "text");
    };
    $("#pause-interview")?.addEventListener("click", async () => {
      try {
        await api(`/api/practice/${encodeURIComponent(publicId)}`, {
          method: "PATCH",
          body: { status: "paused", tab_switch_count: interviewTabSwitches },
        });
        state.session = await api(
          `/api/practice/${encodeURIComponent(publicId)}`,
        );
        drawInterviewSession(publicId);
        toast("Interview paused and checkpointed.");
      } catch (err) {
        toast(errorText(err), true);
      }
    });
    $("#finish-interview")?.addEventListener("click", () =>
      finishInterview(publicId),
    );
  }

  function drawSession(publicId) {
    const s = state.session,
      total = s.questions?.length || s.question_count || 0,
      current = Number(s.current_index || 0),
      running = s.status === "in_progress" && current < total,
      paused = s.status === "paused";
    const question = s.questions?.[current] || {};
    const ledger = (s.responses || [])
      .map(
        (r) =>
          `<li class="ledger-row"><header><strong>Prompt ${r.number}</strong><span class="tag">${r.is_skipped ? "Skipped" : `${fmt(r.score, 1)} / 10`}</span></header><p>${esc(r.question)}</p>${r.feedback ? `<p>${esc(r.feedback)}</p>` : ""}</li>`,
      )
      .join("");
    const panel = paused
      ? `<section class="card"><div class="eyebrow">Paused</div><h2 style="margin-top:8px">Your place is checkpointed</h2><p class="body-copy" style="margin-top:8px">Nothing has been lost. Resume whenever you are ready.</p><div class="session-actions"><button class="btn" data-action="resume">▶ Resume session</button><button class="btn secondary" data-action="abandon">Archive session</button></div></section>`
      : running
        ? `<section class="card"><div class="prompt"><small>${esc(question.focus || "Current prompt")}</small><h2>${esc(question.prompt)}</h2><div class="row" style="margin-top:13px"><button type="button" class="btn secondary small" id="read-question">◉ Listen to prompt</button></div>${question.guidance ? `<div class="notice" style="margin-top:13px">💡 ${esc(question.guidance)}</div>` : ""}</div><label class="field" style="margin-top:18px"><span class="label">Your answer</span><textarea class="textarea" id="answer-box" rows="9" placeholder="Speak in full sentences, as you would in the room…"></textarea><div class="answer-tools"><span id="word-count">0 words</span><div class="row"><button type="button" class="btn secondary small" id="dictate">◉ Browser dictate</button><button type="button" class="btn secondary small" id="server-voice">◉ Transcribe voice</button><button type="button" class="btn secondary small" id="insert-dictation">▧ Insert dictation</button></div></div><span class="field-hint">Browser dictation works without an API key. Server transcription uses Groq Whisper when configured.</span></label><div class="session-actions"><button class="btn" id="submit-answer">Submit answer&nbsp; ✓</button><button class="btn secondary" id="skip-answer">Skip this prompt&nbsp; →</button><button class="btn secondary" data-action="pause">Ⅱ Pause and save</button></div></section>`
        : `<section class="card"><div class="eyebrow">All prompts answered</div><h2 style="margin-top:8px">This sitting is complete</h2><p class="body-copy" style="margin-top:8px">Your report holds the question-by-question scores, rubric breakdown and transcript.</p><a class="btn" href="/history/${encodeURIComponent(publicId)}" style="margin-top:18px">Open the report&nbsp; →</a></section>`;
    $("#session-content").innerHTML =
      `<section class="card session-header"><div class="row between"><div><div class="eyebrow">${esc(s.mode_label || modeLabel(s.mode))}</div><h1 style="font-size:clamp(29px,4vw,44px);margin-top:8px">${esc(s.title)}</h1><div class="session-meta"><span class="status-chip">${esc(s.status_label || statuses[s.status] || s.status)}</span><span class="tag">◈ ${esc(s.difficulty)}</span>${s.side ? `<span class="tag" style="border-color:rgba(180,128,31,.4);color:#8a5f12">⚑ ${esc(s.side)}</span>` : ""}</div></div><a class="btn secondary small" href="/history">▤ History</a></div>${providerNotice(s)}<div><div class="row between"><span class="label">Prompt ${Math.min(current + 1, total)} of ${total}</span><span class="field-hint">${fmt(total ? (current / total) * 100 : 0)}% through</span></div>${progress(total ? (current / total) * 100 : 0)}</div></section><div class="session-layout"><div>${panel}</div><div class="stack"><section class="card soft-card"><div class="section-head"><div class="eyebrow">Session ledger</div><h2 style="margin-top:7px;font-size:23px">What you have banked</h2></div><div class="stats" style="grid-template-columns:repeat(3,1fr);margin-top:16px">${stat("Answered", s.answered_count || 0)}${stat("Score", fmt(s.total_score, 1))}${stat("Time", s.duration_label || "0 min")}</div>${s.summary_feedback ? `<div class="notice" style="margin-top:16px">${esc(s.summary_feedback)}</div>` : ""}<ul class="list">${ledger || `<li class="muted">Nothing recorded yet — your first answer will appear here.</li>`}</ul></section><div class="room"><span class="room-icon">${modes[s.mode]?.icon || "✦"}</span><strong>${esc(s.mode_label || modeLabel(s.mode))}</strong><p>Keep your answer specific, structured and purposeful.</p></div></div></div>`;
    bindSession(publicId);
  }
  function bindSession(publicId) {
    $("#read-question")?.addEventListener("click", () =>
      speakText(
        state.session?.questions?.[Number(state.session?.current_index || 0)]
          ?.prompt,
        state.session?.language || "en",
        "lisa",
        true,
      ),
    );
    const answer = $("#answer-box");
    const answerStartedAt = Date.now();
    if (answer) {
      answer.oninput = () => {
        $("#word-count").textContent =
          `${answer.value.trim() ? answer.value.trim().split(/\s+/).length : 0} words`;
      };
    }
    const send = async (skip = false) => {
      const text = answer?.value.trim() || "";
      if (!skip && text.length < 20)
        return toast(
          "Write at least 20 characters so your coach has something to assess.",
          true,
        );
      const button = skip ? $("#skip-answer") : $("#submit-answer");
      if (button) {
        button.disabled = true;
        button.textContent = skip ? "Skipping…" : "Scoring your answer…";
      }
      try {
        const result = await api(
          `/api/practice/${encodeURIComponent(publicId)}/answer`,
          {
            method: "POST",
            body: {
              answer: skip ? "" : text,
              source: skip ? "skipped" : "text",
              response_seconds: Math.max(
                0,
                Math.floor((Date.now() - answerStartedAt) / 1000),
              ),
            },
          },
        );
        state.session = result;
        if (result.gamification?.points_earned)
          toast(`+${result.gamification.points_earned} points earned.`);
        (result.gamification?.new_achievements || []).forEach((item) =>
          toast(`Achievement unlocked: ${item.name}`),
        );
        if (state.session.status === "completed") {
          toast("Session complete — your report is ready.");
          setTimeout(
            () => (location.href = `/history/${encodeURIComponent(publicId)}`),
            350,
          );
        } else drawSession(publicId);
      } catch (err) {
        if (button) {
          button.disabled = false;
          button.textContent = skip ? "Skip this prompt →" : "Submit answer ✓";
        }
        toast(errorText(err), true);
      }
    };
    $("#submit-answer")?.addEventListener("click", () => send(false));
    $("#skip-answer")?.addEventListener("click", () => send(true));
    $('[data-action="pause"]')?.addEventListener("click", async () => {
      try {
        await api(`/api/practice/${encodeURIComponent(publicId)}`, {
          method: "PATCH",
          body: { status: "paused" },
        });
        state.session = await api(
          `/api/practice/${encodeURIComponent(publicId)}`,
        );
        toast("Paused and checkpointed.");
        drawSession(publicId);
      } catch (err) {
        toast(errorText(err), true);
      }
    });
    $('[data-action="resume"]')?.addEventListener("click", async () => {
      try {
        await api(`/api/practice/${encodeURIComponent(publicId)}`, {
          method: "PATCH",
          body: { status: "in_progress" },
        });
        state.session = await api(
          `/api/practice/${encodeURIComponent(publicId)}`,
        );
        drawSession(publicId);
      } catch (err) {
        toast(errorText(err), true);
      }
    });
    $('[data-action="abandon"]')?.addEventListener("click", async () => {
      if (!confirm("Archive this rehearsal? You can no longer resume it."))
        return;
      try {
        await api(`/api/practice/${encodeURIComponent(publicId)}`, {
          method: "PATCH",
          body: { status: "abandoned" },
        });
        location.href = "/practice";
      } catch (err) {
        toast(errorText(err), true);
      }
    });
    let recognition,
      transcript = "";
    $("#dictate")?.addEventListener("click", () => {
      const Speech = window.SpeechRecognition || window.webkitSpeechRecognition;
      if (!Speech) {
        toast(
          "Using microphone recording and server transcription for this browser.",
        );
        $("#server-voice")?.click();
        return;
      }
      if (recognition) {
        recognition.stop();
        recognition = null;
        return;
      }
      recognition = new Speech();
      recognition.continuous = true;
      recognition.interimResults = true;
      recognition.onresult = (e) => {
        transcript = [...e.results].map((r) => r[0].transcript).join(" ");
        toast("Dictation captured — insert it when ready.");
      };
      recognition.onerror = () =>
        toast(
          "No dictation was captured. You can always type your answer.",
          true,
        );
      recognition.start();
    });
    $("#insert-dictation")?.addEventListener("click", () => {
      if (!transcript)
        return toast("Start dictation first, or type your answer.", true);
      answer.value = `${answer.value.trim()} ${transcript}`.trim();
      answer.dispatchEvent(new Event("input"));
      toast("Dictation added — edit it freely before submitting.");
    });
    bindVoiceRecorder(
      "#answer-box",
      "#server-voice",
      () => state.session?.language || "en",
    );
  }

  async function historyPage() {
    render(
      `<div class="container page-pad"><div class="eyebrow">History</div><h1 style="font-size:clamp(31px,4vw,46px);margin-top:8px">Every sitting, kept on the record</h1><p class="body-copy" style="max-width:720px;margin-top:10px">Search by role or motion, filter by room or state, resume anything still open, and open any report in full.</p><div id="history-content" class="loading">Loading your archive…</div></div>`,
      "history",
    );
    state.historyFilters = {};
    try {
      state.sessions = await api(historyUrl(0));
      drawHistory();
    } catch (err) {
      $("#history-content").innerHTML = alertBox(errorText(err));
    }
  }
  const historyPageSize = 20;
  const historyUrl = (offset = 0) => {
    const params = new URLSearchParams({
      ...state.historyFilters,
      limit: historyPageSize,
      offset,
    });
    return `/api/practice?${params}`;
  };
  function drawHistory() {
    const data = state.sessions || { sessions: [], counts: {} },
      counts = data.counts || {};
    $("#history-content").innerHTML =
      `<div id="history-stats" class="stats" style="margin-top:22px">${stat("Sessions", counts.total || 0)}${stat("Completed", counts.completed || 0)}${stat("Still open", counts.open || 0)}${stat("Average score", `${fmt(counts.average)}%`)}</div><div class="card" style="margin-top:20px"><div class="filters"><label class="field"><span class="label">Search</span><input class="input" id="history-search" placeholder="Role, motion or title…"></label><label class="field compact"><span class="label">Mode</span><select class="select" id="history-mode"><option value="all">All modes</option>${Object.entries(
        modes,
      )
        .map(([k, v]) => `<option value="${k}">${v.short}</option>`)
        .join(
          "",
        )}</select></label><label class="field compact"><span class="label">Difficulty</span><select class="select" id="history-difficulty"><option value="all">Any level</option>${["beginner", "intermediate", "advanced", "expert"].map((level) => `<option value="${level}">${level[0].toUpperCase() + level.slice(1)}</option>`).join("")}</select></label><label class="field compact"><span class="label">State</span><select class="select" id="history-status"><option value="all">Any state</option>${Object.entries(
        statuses,
      )
        .map(([k, v]) => `<option value="${k}">${v}</option>`)
        .join(
          "",
        )}</select></label><label class="field compact"><span class="label">Min score %</span><input class="input" id="history-min-score" type="number" min="0" max="100" placeholder="0"></label><button class="btn secondary small" id="history-filter">Apply filters</button></div></div><div id="session-list" class="grid two-col" style="margin-top:20px">${sessionCards(data.sessions)}</div>${historyPager(data)}`;
    $("#history-search").value = state.historyFilters.search || "";
    $("#history-mode").value = state.historyFilters.mode || "all";
    $("#history-difficulty").value = state.historyFilters.difficulty || "all";
    $("#history-status").value = state.historyFilters.status || "all";
    $("#history-min-score").value = state.historyFilters.min_score || "";
    $("#history-filter").onclick = async () => {
      state.historyFilters = {
        search: $("#history-search").value,
        mode: $("#history-mode").value,
        status: $("#history-status").value,
        difficulty: $("#history-difficulty").value,
        min_score: $("#history-min-score").value,
      };
      try {
        state.sessions = await api(historyUrl(0));
        drawHistory();
      } catch (err) {
        toast(errorText(err), true);
      }
    };
    $$("[data-history-page]").forEach(
      (button) =>
        (button.onclick = async () => {
          if (button.disabled) return;
          try {
            state.sessions = await api(
              historyUrl(Number(button.dataset.offset || 0)),
            );
            drawHistory();
          } catch (err) {
            toast(errorText(err), true);
          }
        }),
    );
  }
  function historyPager(data = {}) {
    const limit = Number(data.limit || historyPageSize),
      offset = Number(data.offset || 0),
      total = Number(data.total || 0);
    if (total <= limit && offset === 0) return "";
    return `<div class="row between" style="margin-top:16px"><span class="field-hint">Showing ${total ? offset + 1 : 0}–${Math.min(offset + limit, total)} of ${total}</span><div class="row"><button class="btn secondary small" data-history-page data-offset="${Math.max(0, offset - limit)}" ${offset <= 0 ? "disabled" : ""}>Previous</button><button class="btn secondary small" data-history-page data-offset="${offset + limit}" ${offset + limit >= total ? "disabled" : ""}>Next</button></div></div>`;
  }
  function sessionCards(list) {
    return list.length
      ? list
          .map((s) => {
            const activity = s.is_live
              ? `◉ ${s.live_contribution_count || 0} contribution${s.live_contribution_count === 1 ? "" : "s"}`
              : `☷ ${s.answered_count || 0} of ${s.question_count || 0} answered`;
            const progressHtml = s.is_live
              ? ""
              : `<div style="margin-top:13px">${progress(((s.answered_count || 0) / Math.max(s.question_count || 1, 1)) * 100)}</div>`;
            return `<article class="session-card"><div class="session-top"><div><small>${esc(s.mode_label || modeLabel(s.mode))}</small><h3>${esc(s.title)}</h3><p>${esc(s.created_label || "")}</p></div><span class="status-chip">${esc(s.status_label || statuses[s.status])}</span></div><div class="row" style="margin-top:13px"><span class="field-hint">${activity}</span><span class="field-hint">◷ ${esc(s.duration_label || "0 min")}</span>${s.status === "completed" ? `<strong style="color:#1a5a57">${fmt(s.percentage_score)}% · readiness ${fmt(s.readiness_score)}</strong>` : ""}</div>${progressHtml}<div class="row" style="margin-top:16px"><a class="btn secondary small" href="/history/${encodeURIComponent(s.public_id)}">▧ View report</a>${s.is_resumable ? `<a class="btn small" href="/practice/${encodeURIComponent(s.public_id)}">▶ Resume</a>` : ""}</div></article>`;
          })
          .join("")
      : `<div class="card empty" style="grid-column:1/-1"><h2>Your archive starts with your first rehearsal</h2><p>Nothing is pre-filled here — every entry is a sitting you actually completed.</p><a class="btn" href="/practice" style="margin-top:16px">Open the practice studio</a></div>`;
  }

  async function reportPage(publicId) {
    render(
      `<div class="container page-pad"><div id="report-content" class="loading">Writing your report view…</div></div>`,
      "history",
    );
    try {
      state.session = await api(
        `/api/practice/${encodeURIComponent(publicId)}`,
      );
      drawReport(publicId);
    } catch (err) {
      $("#report-content").innerHTML =
        alertBox(errorText(err)) +
        `<a class="btn" href="/history" style="margin-top:18px">← Back to history</a>`;
    }
  }
  function drawReport(publicId) {
    const s = state.session,
      responses = s.responses || [],
      graded = responses.filter((r) => r.is_graded),
      rubric = s.rubric || [];
    const responseHtml = responses.length
      ? responses
          .map(
            (r) =>
              `<article class="response"><div class="row between"><div><small class="eyebrow">Prompt ${r.number}</small><h3>${esc(r.question)}</h3></div><span class="tag">${r.is_skipped ? "Skipped" : `${fmt(r.score, 1)} / 10`}</span></div>${r.is_skipped ? `<p class="muted" style="margin-top:14px">You moved past this prompt, so nothing was scored.</p>` : `<div class="answer-box"><small class="label">Your answer</small><p>${esc(r.answer)}</p><div class="row" style="margin-top:9px"><span class="field-hint">${r.word_count} words</span><span class="field-hint">${r.response_seconds}s</span><span class="field-hint">${esc(r.answer_source)}</span></div></div>`}${r.feedback ? `<div class="notice" style="margin-top:12px">▱ ${esc(r.feedback)}</div>` : ""}${r.rubric?.length ? `<div style="margin-top:13px">${r.rubric.map(rubricRow).join("")}</div>` : ""}</article>`,
          )
          .join("")
      : `<p class="muted">No answers were recorded in this session.</p>`;
    const wudc = s.wudc
      ? `<section class="card soft-card wudc-card" style="margin-top:20px"><div class="row between"><div><div class="eyebrow">WUDC speaker scale</div><h2 style="margin-top:7px">${esc(s.wudc.name)}</h2><p class="muted" style="margin-top:7px">${esc(s.wudc.description)}</p></div><strong class="wudc-score">${esc(s.wudc.score)}</strong></div><div class="notice" style="margin-top:14px">This tournament-style speaker score is mapped from your PrepSage report score and is separate from your 0–100 readiness score.</div></section>`
      : "";
    $("#report-content").innerHTML =
      `<section class="card"><div class="row between"><div><div class="eyebrow">${esc(s.mode_label || modeLabel(s.mode))}</div><h1 style="font-size:clamp(29px,4vw,44px);margin-top:8px">${esc(s.title)}</h1><div class="session-meta"><span class="status-chip">${esc(s.status_label || statuses[s.status])}</span><span class="tag">◈ ${esc(s.difficulty)}</span></div><p class="field-hint" style="margin-top:12px">Recorded ${esc(s.created_label || "")}</p></div><div class="row"><a class="btn secondary small" href="/history">← History</a>${s.is_resumable ? `<a class="btn small" href="/practice/${encodeURIComponent(publicId)}">▶ Resume</a>` : ""}</div></div></section><div class="report-score">${stat("Score", `${fmt(s.percentage_score)}%`)}${stat("Readiness", fmt(s.readiness_score))}${stat("Answers graded", graded.length)}${stat("Time in room", s.duration_label || "0 min")}</div><div class="notice" style="margin-top:16px">Readiness ${fmt(s.readiness_score)} / 100${progress(s.readiness_score)}</div>${wudc}<div class="dashboard-grid" style="margin-top:20px"><div class="stack"><section class="card"><div class="section-head"><div class="eyebrow">Closing note</div><h2 style="margin-top:7px">Your coach's summary</h2></div><p class="body-copy" style="margin-top:16px">${esc(s.summary_feedback || "The closing summary appears once the session is complete.")}</p>${s.strengths?.length ? `<h3 style="margin-top:18px">Worked well</h3><ul class="list">${s.strengths.map((x) => `<li>✓ ${esc(x)}</li>`).join("")}</ul>` : ""}${s.improvements?.length ? `<h3 style="margin-top:18px;color:#8a5f12">To work on</h3><ul class="list">${s.improvements.map((x) => `<li>→ ${esc(x)}</li>`).join("")}</ul>` : ""}</section><section class="card"><div class="section-head"><div class="eyebrow">Rubric</div><h2 style="margin-top:7px">How the room judged you</h2></div>${rubric.length ? `<div style="margin-top:14px">${rubric.map(rubricRow).join("")}</div>` : `<p class="muted" style="margin-top:14px">The rubric breakdown appears once the session is complete.</p>`}</section></div><div><section class="card soft-card"><div class="eyebrow">The brief</div><h2 style="margin-top:7px;font-size:23px">What you set up</h2><dl class="list" style="margin-top:14px">${[
        ["Role", s.role_target],
        ["Company", s.company],
        ["Interviewer", s.interviewer?.name],
        ["Interview type", s.interview_type],
        ["Projects or experience", s.project_info],
        ["Topic or motion", s.topic],
        ["Coaching focus", s.focus_area],
        ["Side", s.side],
        ["Format", s.debate_format],
        ["Rounds", s.rounds],
        ["Notes", s.context_notes],
      ]
        .filter((x) => x[1])
        .map(
          (x) =>
            `<li><small class="label">${esc(x[0])}</small><p style="margin-top:4px">${esc(x[1])}</p></li>`,
        )
        .join(
          "",
        )}</dl></section></div></div><section class="card" style="margin-top:20px"><div class="section-head"><div class="eyebrow">Question by question</div><h2 style="margin-top:7px">Every prompt, answer and score</h2></div><div class="stack" style="margin-top:16px">${responseHtml}</div></section>${s.transcript ? `<section class="card" style="margin-top:20px"><div class="eyebrow">Transcript</div><h2 style="margin-top:7px">The sitting, verbatim</h2><pre class="answer-box" style="max-height:360px;overflow:auto">${esc(s.transcript)}</pre></section>` : ""}`;
  }
  function rubricRow(item) {
    return `<div class="rubric-row"><strong>${esc(item.criterion)}</strong><strong style="color:#1a5a57">${fmt(item.score, 1)} / ${fmt(item.max_score || 10)}</strong><small>${esc(item.comment || "")}</small></div>`;
  }

  let activeAudio = null;
  let activeSpeech = null;
  let speechRun = 0;
  function speechChunks(text, maxLength = 220) {
    const sentences = text.match(/[^.!?]+[.!?]+|[^.!?]+$/g) || [text];
    const chunks = [];
    let current = "";
    for (const sentence of sentences) {
      const words = sentence.trim().split(/\s+/);
      for (const word of words) {
        if (current && `${current} ${word}`.length > maxLength) {
          chunks.push(current);
          current = word;
        } else {
          current = current ? `${current} ${word}` : word;
        }
      }
      if (current && /[.!?]$/.test(sentence.trim())) {
        chunks.push(current);
        current = "";
      }
    }
    if (current) chunks.push(current);
    return chunks;
  }
  function browserVoice(language, voiceProfile) {
    const voices = window.speechSynthesis?.getVoices?.() || [];
    const languageRoot = (language || "en").split("-")[0].toLowerCase();
    const localized = voices.filter((voice) =>
      (voice.lang || "").toLowerCase().startsWith(languageRoot),
    );
    const genderHint =
      voiceProfile === "bob"
        ? /\b(male|man|david|daniel|alex|fred|george|james|tom)\b/i
        : /\b(female|woman|samantha|victoria|karen|zira|aria|jenny|susan)\b/i;
    return (
      localized.find((voice) => genderHint.test(voice.name)) ||
      voices.find((voice) => genderHint.test(voice.name)) ||
      localized[0] ||
      null
    );
  }
  function speakInBrowser(text, speechLanguage, voiceProfile, run) {
    if (!window.speechSynthesis || !window.SpeechSynthesisUtterance) {
      if (activeSpeech?.run === run) activeSpeech = null;
      setInterviewPresence("Read the response or continue when ready", "ready");
      toast("Speech playback is not available in this browser.", true);
      return;
    }
    const chunks = speechChunks(text);
    if (!chunks.length) {
      if (activeSpeech?.run === run) activeSpeech = null;
      setInterviewPresence("Ready for your answer", "ready");
      return;
    }
    const voice = browserVoice(speechLanguage, voiceProfile);
    let index = 0;
    const speakNext = () => {
      if (run !== speechRun) return;
      if (index >= chunks.length) {
        if (activeSpeech?.run === run) activeSpeech = null;
        setInterviewPresence("Ready for your answer", "ready");
        return;
      }
      const utterance = new SpeechSynthesisUtterance(chunks[index++]);
      utterance.lang = speechLanguage;
      utterance.rate = 0.96;
      if (voice) utterance.voice = voice;
      utterance.onstart = () => {
        if (run === speechRun)
          setInterviewPresence("AI is speaking", "speaking");
      };
      utterance.onend = speakNext;
      utterance.onerror = (event) => {
        if (run === speechRun && activeSpeech?.run === run) activeSpeech = null;
        if (run === speechRun)
          setInterviewPresence("Ready for your answer", "ready");
        if (
          run === speechRun &&
          !["interrupted", "canceled"].includes(event.error)
        )
          toast(
            "Speech playback stopped. You can continue reading the response.",
            true,
          );
      };
      window.speechSynthesis.speak(utterance);
    };
    speakNext();
  }
  function haltSpeechPlayback(invalidate = true) {
    if (invalidate) speechRun += 1;
    window.speechSynthesis?.cancel?.();
    if (activeAudio) {
      activeAudio.pause();
      try {
        activeAudio.currentTime = 0;
      } catch (_) {}
      activeAudio = null;
    }
    activeSpeech = null;
  }
  async function speakText(
    text,
    language = "en",
    voiceProfile = "lisa",
    togglePlayback = false,
  ) {
    if (!text?.trim()) return;
    const normalizedText = text.trim();
    if (togglePlayback && activeSpeech?.text === normalizedText) {
      haltSpeechPlayback();
      setInterviewPresence("AI voice paused", "ready");
      return;
    }
    const speechLanguage =
      window.PREPSAGE_LANGUAGES?.[language]?.speech || "en-IN";
    const run = ++speechRun;
    haltSpeechPlayback(false);
    activeSpeech = { run, text: normalizedText };
    setInterviewPresence("Preparing interviewer audio…", "thinking");
    let result = null;
    try {
      result = await api("/api/speech/synthesize", {
        method: "POST",
        body: { text: normalizedText, language, voice_profile: voiceProfile },
      });
    } catch (_) {
      // Browser synthesis below is the no-network/no-server-audio path.
    }
    if (run !== speechRun) return;
    if (result?.audio_data) {
      const audio = new Audio(
        `data:${result.mime_type || "audio/mpeg"};base64,${result.audio_data}`,
      );
      activeAudio = audio;
      audio.onplaying = () => {
        if (run === speechRun)
          setInterviewPresence("AI is speaking", "speaking");
      };
      audio.onended = () => {
        if (activeAudio === audio) activeAudio = null;
        if (activeSpeech?.run === run) activeSpeech = null;
        setInterviewPresence("Ready for your answer", "ready");
      };
      audio.onerror = () => {
        if (activeAudio === audio) activeAudio = null;
        if (activeSpeech?.run === run) activeSpeech = null;
        setInterviewPresence("Ready for your answer", "ready");
      };
      try {
        await audio.play();
        if (run !== speechRun) {
          audio.pause();
          return;
        }
        return audio;
      } catch (_) {
        if (activeAudio === audio) activeAudio = null;
      }
    }
    if (run !== speechRun) return;
    activeSpeech = { run, text: normalizedText };
    speakInBrowser(normalizedText, speechLanguage, voiceProfile, run);
  }

  async function coachPage() {
    render(
      `<div class="container page-pad"><div class="eyebrow">AI coach</div><h1 style="font-size:clamp(31px,4vw,46px);margin-top:8px">A coach between rehearsals</h1><p class="body-copy" style="max-width:720px;margin-top:10px">Ask for a better structure, a confidence reset, a debate strategy, or feedback on an answer. The coach replies in your chosen language.</p><div id="coach-content" class="loading">Opening your coaching notebook…</div></div>`,
      "coach",
    );
    try {
      if (!window.PREPSAGE_LANGUAGES)
        window.PREPSAGE_LANGUAGES = await api("/api/languages");
      const saved = JSON.parse(
        localStorage.getItem("prepsage-coach-thread") || "[]",
      );
      drawCoach(saved);
    } catch (err) {
      $("#coach-content").innerHTML = alertBox(errorText(err));
    }
  }
  function drawCoach(messages = []) {
    const modesList = [
      ["coach", "Open coaching"],
      ["interview", "Interview answer"],
      ["debate", "Debate strategy"],
      ["confidence", "Confidence reset"],
      ["feedback", "Answer feedback"],
    ];
    const quickPrompts = [
      "Turn this into a STAR answer.",
      "How do I sound more confident?",
      "Give me a debate strategy.",
      "Help me structure my next response.",
    ];
    $("#coach-content").innerHTML =
      `<div class="dashboard-grid"><section class="card"><div class="row between"><div><div class="eyebrow">Conversation</div><h2 style="margin-top:7px">Bring a real question</h2></div><button class="btn secondary small" id="clear-coach">Clear thread</button></div><div class="quick-prompts">${quickPrompts.map((prompt) => `<button type="button" class="tag quick-prompt">${esc(prompt)}</button>`).join("")}</div><div id="coach-thread" class="chat-thread">${messages.length ? messages.map((m, i) => `<div class="chat-msg ${m.role === "user" ? "user" : "assistant"}"><small>${m.role === "user" ? "You" : "PrepSage"}</small><p>${esc(m.content)}</p>${m.role !== "user" ? `<button class="btn secondary small speak-reply" data-index="${i}">Listen</button>` : ""}</div>`).join("") : `<div class="empty"><h3>What are you preparing for?</h3><p>Try: “Help me turn this project into a strong STAR answer.”</p></div>`}</div><form id="coach-form" class="form"><div class="form-grid">${field("Coaching mode", "coach-mode", { select: true, value: "coach", options: modesList })}${field("Language", "coach-language", { select: true, value: "en", options: Object.entries(window.PREPSAGE_LANGUAGES || { en: { name: "English" } }).map(([key, item]) => [key, item.name || key]) })}</div>${field("Your question", "coach-message", { textarea: true, rows: 4, placeholder: "Tell me what you want to improve…", value: "" })}<div class="answer-tools"><span class="field-hint">Speak your question or type it below.</span><button type="button" class="btn secondary small" id="coach-voice">◉ Record question</button></div><div id="coach-voice-preview" class="voice-preview hidden"><div class="field-hint">Check your transcript before using it</div><p id="coach-voice-text"></p><div class="row"><button type="button" class="btn secondary small" id="coach-voice-retry">Retry</button><button type="button" class="btn small" id="coach-voice-use">Use this transcript</button></div></div><div class="row between"><span class="field-hint">The coach uses Groq when configured and stays usable with local guidance.</span><button class="btn" type="submit">Ask your coach&nbsp; →</button></div></form></section><aside class="card soft-card"><div class="eyebrow">Five ways in</div><h2 style="margin-top:7px;font-size:24px">Make the next answer easier</h2><ul class="list" style="margin-top:16px"><li><strong>Structure</strong><span>Turn a loose thought into a clear opening, evidence and close.</span></li><li><strong>Pressure</strong><span>Practise concise answers when the room feels fast.</span></li><li><strong>Clash</strong><span>Find the strongest opposing point before the debate finds you.</span></li><li><strong>Language</strong><span>Keep your thinking and feedback in a familiar language.</span></li></ul></aside></div>`;
    const thread = [...messages];
    $("#clear-coach").onclick = () => {
      localStorage.removeItem("prepsage-coach-thread");
      drawCoach([]);
    };
    bindVoiceRecorder(
      "#coach-message",
      "#coach-voice",
      () => $("#coach-language")?.value || "en",
      {
        previewId: "#coach-voice-preview",
        textId: "#coach-voice-text",
        useId: "#coach-voice-use",
        retryId: "#coach-voice-retry",
      },
    );
    $$(".quick-prompt").forEach(
      (button) =>
        (button.onclick = () => {
          $("#coach-message").value = button.textContent;
          $("#coach-message").focus();
        }),
    );
    $$(".speak-reply").forEach(
      (button) =>
        (button.onclick = () =>
          speakText(
            thread[Number(button.dataset.index)]?.content,
            $("#coach-language")?.value || "en",
            "lisa",
            true,
          )),
    );
    $("#coach-form").onsubmit = async (event) => {
      event.preventDefault();
      const form = event.currentTarget,
        input = $("#coach-message"),
        message = input.value.trim(),
        button = $("button[type=submit]", form);
      if (!message) return toast("Write a question for your coach.", true);
      const data = Object.fromEntries(new FormData(form).entries());
      thread.push({ role: "user", content: message });
      localStorage.setItem("prepsage-coach-thread", JSON.stringify(thread));
      button.disabled = true;
      button.textContent = "Thinking…";
      try {
        const result = await api("/api/coach", {
          method: "POST",
          body: {
            message,
            history: thread,
            mode: data["coach-mode"],
            language: data["coach-language"],
            user_goal: state.user.target_role || "",
          },
        });
        thread.push({ role: "assistant", content: result.reply });
        localStorage.setItem("prepsage-coach-thread", JSON.stringify(thread));
        drawCoach(thread);
        speakText(result.reply, result.language);
      } catch (err) {
        thread.pop();
        localStorage.setItem("prepsage-coach-thread", JSON.stringify(thread));
        toast(errorText(err), true);
        button.disabled = false;
        button.textContent = "Ask your coach →";
      }
    };
  }

  function arenaPage() {
    stopSharedRoomSync();
    render(
      `<div class="container page-pad"><div class="eyebrow">Live rooms</div><h1 style="font-size:clamp(31px,4vw,46px);margin-top:8px">Practise the room, not just the answer</h1><p class="body-copy" style="max-width:760px;margin-top:10px">Choose a solo AI simulation or meet other participants in a shared debate or group discussion.</p><div class="mode-grid arena-tabs" role="tablist" aria-label="Live room type" style="max-width:520px"><button type="button" class="mode-btn active" id="arena-solo-tab" role="tab" aria-selected="true">Solo AI practice</button><button type="button" class="mode-btn" id="arena-multiplayer-tab" role="tab" aria-selected="false">Multiplayer rooms</button></div><div id="arena-content" class="loading">Preparing the live rooms…</div></div>`,
      "arena",
    );
    $("#arena-solo-tab").onclick = () => setArenaTab("solo");
    $("#arena-multiplayer-tab").onclick = () => setArenaTab("multiplayer");
    const params = new URLSearchParams(location.search);
    const inviteCode = params.get("invite");
    const roomId = params.get("room");
    if (inviteCode) {
      setArenaTab("multiplayer");
      joinPrivateInvite(inviteCode);
      return;
    }
    if (roomId) {
      setArenaTab("multiplayer");
      loadSharedRoom(roomId);
      return;
    }
    if (!window.PREPSAGE_LANGUAGES)
      api("/api/languages")
        .then((data) => {
          window.PREPSAGE_LANGUAGES = data;
          if ($("#arena-solo-tab")?.classList.contains("active"))
            drawArenaSetup();
        })
        .catch((err) => {
          if ($("#arena-solo-tab")?.classList.contains("active"))
            $("#arena-content").innerHTML = alertBox(errorText(err));
        });
    else drawArenaSetup();
  }
  function setArenaTab(tab) {
    stopSharedRoomSync();
    const solo = tab === "solo";
    $("#arena-solo-tab")?.classList.toggle("active", solo);
    $("#arena-multiplayer-tab")?.classList.toggle("active", !solo);
    $("#arena-solo-tab")?.setAttribute("aria-selected", String(solo));
    $("#arena-multiplayer-tab")?.setAttribute("aria-selected", String(!solo));
    if (solo) {
      drawArenaSetup();
    } else if (state.sharedRoom?.room) {
      drawSharedRoom();
      startSharedRoomSync(state.sharedRoom.room.public_id);
    } else {
      drawMultiplayerHub();
    }
  }
  function multiplayerLanguageOptions() {
    return Object.entries(
      window.PREPSAGE_LANGUAGES || { en: { name: "English" } },
    ).map(([key, item]) => [key, item.name || key]);
  }
  function drawMultiplayerHub(message = "") {
    stopSharedRoomSync();
    $("#arena-content").innerHTML = `<div class="multiplayer-hub">
        <section class="card">
          <div class="row between section-head">
            <div><div class="eyebrow">Shared sessions</div><h2 style="margin-top:7px">Create a live room</h2><p class="muted" style="margin-top:6px">Choose public so anyone can find it, or private and share its invite code.</p></div>
          </div>
          ${alertBox(message)}
          <div class="grid two-col">
            <form id="multiplayer-create" class="form">
              <div class="form-grid">${field("Room type", "mode", {
                select: true,
                value: "debate",
                options: [
                  ["debate", "Live debate"],
                  ["group_discussion", "Group discussion"],
                ],
              })}${field("Who can join?", "visibility", {
                select: true,
                value: "public",
                options: [
                  ["public", "Public room"],
                  ["private", "Private · invite code only"],
                ],
              })}</div>
              <div class="form-grid">${field("Room title (optional)", "title", { required: false, placeholder: "A short name for the room" })}${field("Topic or motion", "topic", { placeholder: "What should the room discuss?" })}</div>
              <div id="multiplayer-debate-settings">
                <div class="form-grid">${field("Your debate side", "side", {
                  select: true,
                  value: "proposition",
                  options: [
                    ["proposition", "Proposition"],
                    ["opposition", "Opposition"],
                  ],
                })}${field("Format", "debate_format", {
                  select: true,
                  value: "british_parliamentary",
                  options: [
                    ["british_parliamentary", "British Parliamentary"],
                    ["public_forum", "Public Forum"],
                    ["oxford_style", "Oxford-style"],
                    ["asian_parliamentary", "Asian Parliamentary"],
                    ["worlds_schools", "Worlds Schools"],
                  ],
                })}</div>
              </div>
              <div class="form-grid">${field("Difficulty", "difficulty", {
                select: true,
                value: "intermediate",
                options: [
                  ["beginner", "Beginner"],
                  ["intermediate", "Intermediate"],
                  ["advanced", "Advanced"],
                  ["expert", "Expert"],
                ],
              })}${field("Language", "language", { select: true, value: "en", options: multiplayerLanguageOptions() })}</div>
              <div class="form-grid">${field("Maximum people", "max_participants", { type: "number", value: "12", hint: "Between 2 and 24, including you." })}<label class="field multiplayer-ai-toggle"><span class="label">AI participation</span><span class="multiplayer-check"><input id="multiplayer-ai" name="ai_enabled" type="checkbox"><span>Include an AI speaker / panel</span></span><span class="field-hint">AI debate speeches stay on the selected side.</span></label></div>
              <div id="multiplayer-ai-settings" class="field">${field(
                "AI debate side",
                "ai_side",
                {
                  select: true,
                  value: "opposition",
                  options: [
                    ["proposition", "Proposition"],
                    ["opposition", "Opposition"],
                  ],
                },
              )}<span class="field-hint">In group discussion, AI joins as a discussion panel.</span></div>
              <div class="row" style="margin-top:8px"><button class="btn" type="submit">Create shared room&nbsp; →</button></div>
            </form>
            <aside class="card soft-card">
              <div class="eyebrow">Join a private room</div><h3 style="margin-top:7px">Use an invite code</h3>
              <p class="body-copy" style="margin-top:8px">Private rooms do not appear in the public list. Ask the host for the room’s 16-character code or invitation link.</p>
              <form id="multiplayer-private-join" class="form" style="margin-top:14px">
                ${field("Invite code", "invite_code", { placeholder: "Paste the code from the host" })}
                <div class="form-grid">${field("Display name (optional)", "display_name", { required: false, value: state.user?.full_name || "", placeholder: "Name shown to the room" })}${field(
                  "Debate side",
                  "private_side",
                  {
                    select: true,
                    required: false,
                    value: "",
                    options: [
                      ["", "Suggest a balanced side"],
                      ["proposition", "Proposition"],
                      ["opposition", "Opposition"],
                    ],
                  },
                )}</div>
                <button class="btn secondary" type="submit">Join private room</button>
              </form>
            </aside>
          </div>
        </section>
        <section class="card" style="margin-top:20px">
          <div class="row between section-head"><div><div class="eyebrow">Open rooms</div><h2 style="margin-top:7px">Join a public session</h2><p class="muted" style="margin-top:6px">Side suggestions favor the less-populated debate team.</p></div><div class="row"><select id="multiplayer-room-filter" class="select" aria-label="Filter public rooms"><option value="all">All room types</option><option value="debate">Debates</option><option value="group_discussion">Group discussions</option></select><button class="btn secondary small" type="button" id="multiplayer-refresh">Refresh</button></div></div>
          <div id="multiplayer-public-rooms" class="room-list"><div class="loading">Loading public rooms…</div></div>
        </section>
      </div>`;
    const modeInput = $("#mode", $("#multiplayer-create"));
    const aiInput = $("#multiplayer-ai");
    const updateCreateFields = () => {
      const isDebate = modeInput.value === "debate";
      $("#multiplayer-debate-settings").classList.toggle("hidden", !isDebate);
      $("#multiplayer-ai-settings").classList.toggle(
        "hidden",
        !isDebate || !aiInput.checked,
      );
    };
    modeInput.onchange = updateCreateFields;
    aiInput.onchange = updateCreateFields;
    updateCreateFields();
    $("#max_participants").min = "2";
    $("#max_participants").max = "24";
    $("#multiplayer-room-filter").onchange = loadPublicMultiplayerRooms;
    $("#multiplayer-refresh").onclick = loadPublicMultiplayerRooms;
    $("#multiplayer-create").onsubmit = async (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      const data = Object.fromEntries(new FormData(form).entries());
      data.ai_enabled = aiInput.checked;
      data.max_participants = Number(data.max_participants);
      if (data.mode !== "debate") {
        data.side = "proposition";
        delete data.ai_side;
      } else if (!data.ai_enabled) {
        delete data.ai_side;
      }
      const button = $('button[type="submit"]', form);
      button.disabled = true;
      button.textContent = "Creating room…";
      try {
        const result = await api("/api/multiplayer/rooms", {
          method: "POST",
          body: data,
        });
        state.sharedRoom = {
          room: result.room,
          invite_code: result.invite_code || "",
        };
        history.replaceState(
          {},
          "",
          `/arena?room=${encodeURIComponent(result.room.public_id)}`,
        );
        drawSharedRoom();
        startSharedRoomSync(result.room.public_id);
      } catch (err) {
        button.disabled = false;
        button.textContent = "Create shared room →";
        toast(errorText(err), true);
      }
    };
    $("#multiplayer-private-join").onsubmit = async (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      const data = Object.fromEntries(new FormData(form).entries());
      const button = $('button[type="submit"]', form);
      button.disabled = true;
      button.textContent = "Joining…";
      try {
        await joinSharedRoom({
          invite_code: data.invite_code.trim(),
          display_name: data.display_name.trim(),
          side: data.private_side || undefined,
        });
      } catch (err) {
        button.disabled = false;
        button.textContent = "Join private room";
        toast(errorText(err), true);
      }
    };
    loadPublicMultiplayerRooms();
  }
  async function loadPublicMultiplayerRooms() {
    const target = $("#multiplayer-public-rooms");
    if (!target) return;
    target.innerHTML = `<div class="loading">Loading public rooms…</div>`;
    try {
      const result = await api("/api/multiplayer/rooms");
      if (!$("#multiplayer-public-rooms")) return;
      const filter = $("#multiplayer-room-filter")?.value || "all";
      const rooms = (result.rooms || []).filter(
        (room) => filter === "all" || room.mode === filter,
      );
      target.innerHTML = rooms.length
        ? rooms
            .map((room) => {
              const counts = room.side_counts || {};
              const suggested =
                (counts.proposition || 0) <= (counts.opposition || 0)
                  ? "proposition"
                  : "opposition";
              return `<article class="multiplayer-room-card">
              <div class="row between"><div><span class="tag">${room.mode === "debate" ? "Live debate" : "Group discussion"}</span><span class="tag">${esc(room.participant_count)}/${esc(room.max_participants)} people</span></div><span class="status-chip">Open</span></div>
              <h3 style="margin-top:12px">${esc(room.title || room.topic)}</h3><p class="body-copy" style="margin-top:5px">${esc(room.topic)}</p>
              <div class="multiplayer-room-meta"><span>Host: ${esc(room.host_name)}</span>${room.mode === "debate" ? `<span>Proposition ${esc(counts.proposition || 0)} · Opposition ${esc(counts.opposition || 0)}</span>` : `<span>${esc(room.debate_format || "Live discussion")}</span>`}</div>
              <div class="row between" style="margin-top:12px">${room.mode === "debate" ? `<label class="field side-choice"><span class="label">Join side · suggested ${suggested === "proposition" ? "Proposition" : "Opposition"}</span><select class="select" data-public-side="${esc(room.public_id)}"><option value="${suggested}">${suggested === "proposition" ? "Proposition" : "Opposition"} (suggested)</option><option value="${suggested === "proposition" ? "opposition" : "proposition"}">${suggested === "proposition" ? "Opposition" : "Proposition"}</option></select></label>` : `<span class="field-hint">Join the discussion with the group.</span>`}<button class="btn small" type="button" data-join-public="${esc(room.public_id)}">Join room</button></div>
            </article>`;
            })
            .join("")
        : `<div class="empty"><p>No open ${filter === "all" ? "public rooms" : filter === "debate" ? "debates" : "group discussions"} right now. Create one and invite others.</p></div>`;
      $$("[data-join-public]", target).forEach((button) => {
        button.onclick = async () => {
          const publicId = button.dataset.joinPublic;
          const side = $$("[data-public-side]", target).find(
            (select) => select.dataset.publicSide === publicId,
          )?.value;
          button.disabled = true;
          button.textContent = "Joining…";
          try {
            await joinSharedRoom({ public_id: publicId, side });
          } catch (err) {
            button.disabled = false;
            button.textContent = "Join room";
            toast(errorText(err), true);
            loadPublicMultiplayerRooms();
          }
        };
      });
    } catch (err) {
      target.innerHTML = `${alertBox(errorText(err))}<button class="btn secondary small" id="multiplayer-retry">Try again</button>`;
      $("#multiplayer-retry").onclick = loadPublicMultiplayerRooms;
    }
  }
  async function joinSharedRoom(details) {
    const result = await api("/api/multiplayer/rooms/join", {
      method: "POST",
      body: details,
    });
    state.sharedRoom = { room: result.room, invite_code: "" };
    history.replaceState(
      {},
      "",
      `/arena?room=${encodeURIComponent(result.room.public_id)}`,
    );
    drawSharedRoom();
    startSharedRoomSync(result.room.public_id);
    toast(`Joined ${result.room.title}.`);
    return result.room;
  }
  async function loadSharedRoom(publicId) {
    try {
      const result = await api(
        `/api/multiplayer/rooms/${encodeURIComponent(publicId)}`,
      );
      state.sharedRoom = { room: result.room, invite_code: "" };
      drawSharedRoom();
      startSharedRoomSync(publicId);
    } catch (err) {
      // A public room link is also a join link. Private rooms still require
      // their invite code and are never joined through a public room id.
      if (err?.status === 404) {
        try {
          await joinSharedRoom({ public_id: publicId });
          return;
        } catch (joinError) {
          $("#arena-content").innerHTML =
            `${alertBox(errorText(joinError))}<button class="btn secondary" id="return-to-room-list">Back to rooms</button>`;
          $("#return-to-room-list").onclick = returnToMultiplayerHub;
          return;
        }
      }
      $("#arena-content").innerHTML =
        `${alertBox(errorText(err))}<button class="btn secondary" id="return-to-room-list">Back to rooms</button>`;
      $("#return-to-room-list").onclick = returnToMultiplayerHub;
    }
  }
  async function joinPrivateInvite(inviteCode, displayName = "", side = "") {
    try {
      await joinSharedRoom({
        invite_code: inviteCode,
        display_name: displayName,
        side: side || undefined,
      });
    } catch (err) {
      history.replaceState({}, "", "/arena");
      drawMultiplayerHub(errorText(err));
    }
  }
  function returnToMultiplayerHub() {
    state.sharedRoom = null;
    history.replaceState({}, "", "/arena");
    drawMultiplayerHub();
  }
  function stopSharedRoomSync() {
    if (sharedRoomPollTimer) clearInterval(sharedRoomPollTimer);
    if (sharedRoomHeartbeatTimer) clearInterval(sharedRoomHeartbeatTimer);
    sharedRoomPollTimer = null;
    sharedRoomHeartbeatTimer = null;
    sharedRoomPollBusy = false;
  }
  function startSharedRoomSync(publicId) {
    stopSharedRoomSync();
    sharedRoomPollTimer = setInterval(() => refreshSharedRoom(publicId), 2500);
    sharedRoomHeartbeatTimer = setInterval(async () => {
      try {
        await api(
          `/api/multiplayer/rooms/${encodeURIComponent(publicId)}/heartbeat`,
          {
            method: "POST",
          },
        );
      } catch (_) {
        // Polling will report a lost membership or expired sign-in.
      }
    }, 12000);
  }
  function sharedRoomRenderKey(room) {
    return JSON.stringify({
      version: room.state_version,
      status: room.status,
      participants: (room.participants || []).map((item) => [
        item.display_name,
        item.side,
        item.is_host,
        item.online,
      ]),
    });
  }
  async function refreshSharedRoom(publicId) {
    if (sharedRoomPollBusy || state.sharedRoom?.room?.public_id !== publicId)
      return;
    sharedRoomPollBusy = true;
    try {
      const result = await api(
        `/api/multiplayer/rooms/${encodeURIComponent(publicId)}`,
      );
      const previousKey = sharedRoomRenderKey(state.sharedRoom.room);
      state.sharedRoom.room = result.room;
      if (previousKey !== sharedRoomRenderKey(result.room))
        drawSharedRoom(true);
    } catch (err) {
      if (err?.status === 404) {
        stopSharedRoomSync();
        state.sharedRoom = null;
        $("#arena-content").innerHTML =
          `${alertBox("Your room membership is no longer active.")}<button class="btn secondary" id="return-to-room-list">Back to rooms</button>`;
        $("#return-to-room-list").onclick = returnToMultiplayerHub;
      }
    } finally {
      sharedRoomPollBusy = false;
    }
  }
  async function copySharedText(text) {
    try {
      await navigator.clipboard.writeText(text);
      toast("Copied to clipboard.");
    } catch (_) {
      const input = document.createElement("textarea");
      input.value = text;
      input.setAttribute("readonly", "");
      input.style.position = "fixed";
      input.style.opacity = "0";
      document.body.appendChild(input);
      input.select();
      const copied = document.execCommand("copy");
      input.remove();
      toast(
        copied
          ? "Copied to clipboard."
          : "Copy failed. Select and copy the text manually.",
        !copied,
      );
    }
  }
  function drawSharedRoom(preserveDraft = false) {
    const shared = state.sharedRoom;
    if (!shared?.room || !$("#arena-content")) return;
    const room = shared.room;
    const isDebate = room.mode === "debate";
    const active = room.status === "in_progress";
    const draft = preserveDraft ? $("#shared-turn-message")?.value : null;
    const wasFocused =
      preserveDraft && document.activeElement?.id === "shared-turn-message";
    const events = room.events || [];
    const participants = room.participants || [];
    const publicLink = `${location.origin}/arena?room=${encodeURIComponent(room.public_id)}`;
    const privateInviteLink = shared.invite_code
      ? `${location.origin}/arena?invite=${encodeURIComponent(shared.invite_code)}`
      : "";
    $("#arena-content").innerHTML = `<section class="card shared-room">
        <div class="row between shared-room-heading">
          <div><div class="eyebrow">${isDebate ? "Multiplayer debate" : "Multiplayer group discussion"} · ${room.visibility === "private" ? "Private room" : "Public room"}</div>
            <h1 style="font-size:clamp(27px,4vw,39px);margin-top:7px">${esc(room.title)}</h1>
            <p class="body-copy" style="margin-top:5px">${esc(room.topic)}</p>
            <div class="session-meta"><span class="status-chip">${active ? "Live" : "Room ended"}</span><span class="tag">${esc(room.participant_count)}/${esc(room.max_participants)} participants</span>${isDebate ? `<span class="tag">${esc(room.debate_format.replaceAll("_", " "))}</span>` : ""}${room.ai_enabled ? `<span class="tag">AI ${isDebate ? `· ${esc(room.ai_side)}` : "panel"} on</span>` : ""}</div>
          </div>
          <button class="btn secondary small" id="back-to-multiplayer">Room list</button>
        </div>
        ${shared.invite_code ? `<div class="notice invite-notice"><strong>Private invite code</strong><p>Share this code or invitation link with people you want in the room. It is only shown to the host when the room is created.</p><div class="row invite-code-row"><code>${esc(shared.invite_code)}</code><button class="btn secondary small" type="button" id="copy-invite-code">Copy code</button><button class="btn secondary small" type="button" id="copy-invite-link">Copy invite link</button></div></div>` : ""}
        ${room.visibility === "public" ? `<div class="room-share-row"><span class="field-hint">Invite others to this public room:</span><button class="btn secondary small" type="button" id="copy-public-link">Copy room link</button></div>` : ""}
        <div class="shared-room-layout">
          <main>
            <div class="row between"><div><div class="eyebrow">Shared transcript</div><h2 style="margin-top:6px">Room conversation</h2></div><span class="field-hint">Updates sync automatically</span></div>
            <div id="shared-room-transcript" class="chat-thread shared-room-transcript" aria-live="polite">${events.length ? events.map((item) => `<article class="shared-event ${item.role === "system" ? "system" : item.role === "ai" ? "ai" : "participant"}"><div class="row between"><strong>${esc(item.speaker)}</strong><div class="row">${item.side ? `<span class="tag">${esc(item.side)}</span>` : ""}${item.speech_type ? `<span class="tag">${esc(item.speech_type)}</span>` : ""}</div></div><p>${esc(item.content)}</p><small>${item.role === "system" ? "Room update" : item.role === "ai" ? "AI participant" : "Participant turn"}</small></article>`).join("") : `<div class="empty"><p>The room is ready. Start the conversation with your first turn.</p></div>`}</div>
            ${
              active
                ? `<form id="shared-room-turn" class="form shared-turn-form">${
                    isDebate
                      ? field("Speech type", "shared-speech-type", {
                          select: true,
                          value: "constructive",
                          options: [
                            ["constructive", "Constructive"],
                            ["rebuttal", "Rebuttal"],
                            ["summary", "Summary / reply"],
                          ],
                        })
                      : ""
                  }${field(isDebate ? "Your speech" : "Your contribution", "shared-turn-message", { textarea: true, rows: isDebate ? 6 : 4, placeholder: isDebate ? `Speak for the ${esc((participants.find((item) => item.is_self)?.side || "selected").toLowerCase())} side…` : "Add a clear point and respond to the group…" })}<div class="row between"><span class="field-hint">${isDebate ? `Your side: ${esc(participants.find((item) => item.is_self)?.side || "not assigned")}` : "Your contribution will be visible to everyone in the room."}</span><button class="btn" type="submit">Send ${isDebate ? "speech" : "contribution"}&nbsp; →</button></div></form>`
                : ""
            }
            ${
              room.evaluation
                ? `<section class="notice shared-evaluation"><strong>Room report</strong><p>${esc(room.evaluation.detailed_feedback || room.evaluation.summary || room.evaluation.feedback || "The room has been scored.")}</p>${room.evaluation.overall_score != null ? `<span class="tag">Room score ${esc(room.evaluation.overall_score)}/${isDebate ? 100 : 10}</span>` : ""}${
                    !isDebate
                      ? `<div class="multiplayer-score-grid">${[
                          [
                            "Participation",
                            room.evaluation.participation_score,
                          ],
                          ["Content", room.evaluation.content_score],
                          [
                            "Communication",
                            room.evaluation.communication_score,
                          ],
                          ["Leadership", room.evaluation.leadership_score],
                        ]
                          .map(
                            ([label, score]) =>
                              `<span><small>${label}</small><strong>${score == null ? "—" : `${esc(score)}/10`}</strong></span>`,
                          )
                          .join("")}</div>`
                      : ""
                  }</section>`
                : ""
            }
          </main>
          <aside class="card soft-card shared-participants"><div class="eyebrow">Participants</div><h2 style="font-size:23px;margin-top:7px">${participants.length} joined</h2><ul class="shared-participant-list">${participants.map((item) => `<li><span class="avatar" aria-hidden="true">${esc(initials({ full_name: item.display_name }))}</span><span class="shared-participant-name"><strong>${esc(item.display_name)}${item.is_self ? " (you)" : ""}</strong><small>${item.is_host ? "Host" : isDebate ? esc(item.side) : "Participant"} · ${item.online ? "Online" : "Away"}</small></span><span class="online-dot ${item.online ? "online" : ""}" title="${item.online ? "Online" : "Away"}"></span></li>`).join("")}</ul>
            <div class="notice" style="margin-top:16px">${isDebate ? `Proposition ${esc(room.side_counts?.proposition || 0)} · Opposition ${esc(room.side_counts?.opposition || 0)}` : room.ai_enabled ? "The AI panel contributes alongside room members." : "Everyone in the room can take part in the discussion."}</div>
            ${room.is_host && active ? `<button class="btn danger" style="width:100%;margin-top:14px" id="finish-shared-room">Close room and score</button>` : ""}
            ${!room.is_host || !active ? `<button class="btn secondary" style="width:100%;margin-top:14px" id="leave-shared-room">${active ? "Leave room" : "Leave and return"}</button>` : ""}
          </aside>
        </div>
      </section>`;
    if (draft !== null) {
      const message = $("#shared-turn-message");
      if (message) {
        message.value = draft;
        if (wasFocused) message.focus();
      }
    }
    $("#back-to-multiplayer").onclick = returnToMultiplayerHub;
    $("#copy-invite-code")?.addEventListener("click", () =>
      copySharedText(shared.invite_code),
    );
    $("#copy-invite-link")?.addEventListener("click", () =>
      copySharedText(privateInviteLink),
    );
    $("#copy-public-link")?.addEventListener("click", () =>
      copySharedText(publicLink),
    );
    $("#shared-room-turn")?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      const message = $("#shared-turn-message").value.trim();
      if (message.length < 10)
        return toast("Write at least 10 characters for your turn.", true);
      const button = $('button[type="submit"]', form);
      button.disabled = true;
      button.textContent = "Sending…";
      try {
        const result = await api(
          `/api/multiplayer/rooms/${encodeURIComponent(room.public_id)}/turn`,
          {
            method: "POST",
            body: {
              request_id: crypto.randomUUID
                ? crypto.randomUUID()
                : `${Date.now()}-${Math.random().toString(36).slice(2)}`,
              message,
              speech_type: $("#shared-speech-type")?.value || "constructive",
            },
          },
        );
        shared.room = result.room;
        drawSharedRoom();
      } catch (err) {
        button.disabled = false;
        button.textContent = `Send ${isDebate ? "speech" : "contribution"} →`;
        toast(errorText(err), true);
        if (err?.status === 409) refreshSharedRoom(room.public_id);
      }
    });
    $("#finish-shared-room")?.addEventListener("click", async (event) => {
      if (!confirm("Close this room for everyone and score the session?"))
        return;
      const button = event.currentTarget;
      button.disabled = true;
      button.textContent = "Scoring room…";
      try {
        const result = await api(
          `/api/multiplayer/rooms/${encodeURIComponent(room.public_id)}/finish`,
          { method: "POST" },
        );
        shared.room = result.room;
        drawSharedRoom();
        toast("The room is closed and its report is ready.");
      } catch (err) {
        button.disabled = false;
        button.textContent = "Close room and score";
        toast(errorText(err), true);
      }
    });
    $("#leave-shared-room")?.addEventListener("click", async () => {
      if (
        active &&
        !confirm(
          "Leave this shared room? You can rejoin it while it remains open.",
        )
      )
        return;
      try {
        await api(
          `/api/multiplayer/rooms/${encodeURIComponent(room.public_id)}/leave`,
          { method: "POST" },
        );
        stopSharedRoomSync();
        state.sharedRoom = null;
        history.replaceState({}, "", "/arena");
        drawMultiplayerHub();
      } catch (err) {
        toast(errorText(err), true);
      }
    });
    const transcript = $("#shared-room-transcript");
    if (transcript) transcript.scrollTop = transcript.scrollHeight;
  }
  function drawArenaSetup() {
    $("#arena-content").innerHTML =
      `<div class="grid two-col"><section class="card"><div class="eyebrow">Choose a room</div><h2 style="margin-top:7px">Live simulation</h2><div class="mode-grid" style="margin-top:18px"><button class="mode-btn active" data-live-mode="debate">⚖ Live debate</button><button class="mode-btn" data-live-mode="group_discussion">◎ Group discussion</button></div><form id="arena-start" class="form" style="margin-top:22px"><div id="arena-fields">${arenaFields("debate")}</div><div class="row" style="margin-top:16px"><button class="btn" type="submit">Enter live room&nbsp; →</button></div></form></section><aside class="card soft-card"><div class="eyebrow">What is different</div><h2 style="margin-top:7px;font-size:24px">Live turns and live pressure</h2><p class="body-copy" style="margin-top:12px">Unlike a one-shot prompt, the panel responds to what you just said. Raise a point of information, rebut a claim, then finish with a WUDC-style report.</p><div class="notice" style="margin-top:18px">Voice transcription is available inside both live rooms when Groq Whisper is configured.</div></aside></div>`;
    let liveMode = "debate";
    const attachRandomMotion = () => {
      if (liveMode !== "debate" || $("#random-motion")) return;
      $("#arena-fields").insertAdjacentHTML(
        "afterbegin",
        `<button type="button" class="btn secondary small" id="random-motion">↻ Try a random motion</button>`,
      );
      $("#random-motion").onclick = () => {
        const motions = [
          "This house would ban social media for under-16s",
          "This house believes remote work should be the default",
          "This house would make voting compulsory",
          "This house believes AI will create more jobs than it destroys",
          "This house would make university education free",
          "This house believes climate action should come before economic growth",
        ];
        $("#motion").value =
          motions[Math.floor(Math.random() * motions.length)];
      };
    };
    attachRandomMotion();
    $$("[data-live-mode]").forEach(
      (button) =>
        (button.onclick = () => {
          liveMode = button.dataset.liveMode;
          $$("[data-live-mode]").forEach((item) => {
            item.classList.toggle("active", item === button);
            item.setAttribute("aria-pressed", String(item === button));
          });
          $("#arena-fields").innerHTML = arenaFields(liveMode);
          attachRandomMotion();
        }),
    );
    $$("[data-live-mode]").forEach((button) =>
      button.setAttribute(
        "aria-pressed",
        String(button.classList.contains("active")),
      ),
    );
    $("#arena-start").onsubmit = async (event) => {
      event.preventDefault();
      const data = Object.fromEntries(
          new FormData(event.currentTarget).entries(),
        ),
        button = $("button[type=submit]", event.currentTarget);
      data.language = data.language || "en";
      button.disabled = true;
      button.textContent = "Opening the room…";
      try {
        const result = await api(
          `/api/live/${liveMode === "debate" ? "debate" : "gd"}/start`,
          { method: "POST", body: data },
        );
        state.live = {
          public_id: result.public_id,
          kind: liveMode === "debate" ? "debate" : "group_discussion",
          state: result.state,
        };
        drawLiveArena();
      } catch (err) {
        button.disabled = false;
        button.textContent = "Enter live room →";
        toast(errorText(err), true);
      }
    };
  }
  function arenaFields(mode) {
    const langs = Object.entries(
      window.PREPSAGE_LANGUAGES || { en: { name: "English" } },
    ).map(([key, item]) => [key, item.name || key]);
    if (mode === "debate")
      return `${field("Motion", "motion", { placeholder: "This house would make university education free" })}<div class="form-grid">${field(
        "Your side",
        "side",
        {
          select: true,
          value: "proposition",
          options: [
            ["proposition", "Proposition"],
            ["opposition", "Opposition"],
          ],
        },
      )}${field("Format", "format", {
        select: true,
        value: "british_parliamentary",
        options: [
          ["british_parliamentary", "British Parliamentary"],
          ["public_forum", "Public Forum"],
          ["oxford_style", "Oxford-style"],
          ["asian_parliamentary", "Asian Parliamentary"],
          ["worlds_schools", "Worlds Schools"],
        ],
      })}</div><div class="form-grid">${field("Difficulty", "difficulty", {
        select: true,
        value: "intermediate",
        options: [
          ["beginner", "Beginner"],
          ["intermediate", "Intermediate"],
          ["advanced", "Advanced"],
          ["expert", "Expert"],
        ],
      })}${field("Language", "language", { select: true, value: "en", options: langs })}</div>`;
    return `<div class="form-grid">${field("Discussion topic", "topic", { placeholder: "Should AI be used to grade exams?" })}${field("Participants or names", "participants", { required: false, placeholder: "3 or Maya, Arjun, Zoya", hint: "Enter a number for a named panel, or comma-separated names." })}</div><div class="form-grid">${field(
      "Difficulty",
      "difficulty",
      {
        select: true,
        value: "intermediate",
        options: [
          ["beginner", "Beginner"],
          ["intermediate", "Intermediate"],
          ["advanced", "Advanced"],
          ["expert", "Expert"],
        ],
      },
    )}${field("Language", "language", { select: true, value: "en", options: langs })}</div>`;
  }
  function liveTranscript(items = []) {
    return items
      .map(
        (item) =>
          `<div class="chat-msg ${item.speaker === "user" || item.speaker === "You" ? "user" : "assistant"}"><small>${esc(item.speaker)}</small><p>${esc(item.content)}</p>${item.speaker !== "user" && item.speaker !== "You" ? `<button class="btn secondary small live-speak" data-text="${esc(item.content)}">Listen</button>` : ""}</div>`,
      )
      .join("");
  }
  function bindVoiceRecorder(
    targetSelector,
    buttonSelector,
    languageGetter = () => "en",
    previewOptions = null,
  ) {
    const target = $(targetSelector),
      button = $(buttonSelector);
    if (!target || !button) return;
    let recorder = null,
      stream = null,
      chunks = [],
      recognition = null,
      browserTranscript = "";
    let starting = false;
    let pendingTranscript = "";
    const speechLanguage = () =>
      window.PREPSAGE_LANGUAGES?.[languageGetter()]?.speech || "en-IN";
    const insert = (text) => {
      if (!text?.trim()) return false;
      target.value = `${target.value.trim()} ${text.trim()}`.trim();
      target.dispatchEvent(new Event("input", { bubbles: true }));
      return true;
    };
    const preview = previewOptions ? $(previewOptions.previewId) : null;
    const previewText = previewOptions ? $(previewOptions.textId) : null;
    const finishTranscript = (text) => {
      if (!text?.trim()) return false;
      if (!preview) return insert(text);
      pendingTranscript = text.trim();
      if (previewText) {
        if ("value" in previewText) previewText.value = pendingTranscript;
        else previewText.textContent = pendingTranscript;
      }
      preview.classList.remove("hidden");
      previewOptions.onReady?.(pendingTranscript);
      return true;
    };
    const reviewedTranscript = () =>
      (previewText && "value" in previewText
        ? previewText.value
        : pendingTranscript
      ).trim();
    const hidePreview = () => {
      pendingTranscript = "";
      preview?.classList.add("hidden");
    };
    if (previewOptions) {
      $(previewOptions.useId)?.addEventListener("click", async () => {
        const transcript = reviewedTranscript();
        if (!transcript) {
          toast("Add or record an answer before sending it.", true);
          return;
        }
        if (previewOptions.onUse) {
          const accepted = await previewOptions.onUse(transcript);
          if (accepted === false) return;
          hidePreview();
          return;
        }
        if (insert(transcript))
          toast("Transcript added — edit it freely before sending.");
        hidePreview();
      });
      $(previewOptions.retryId)?.addEventListener("click", () => {
        hidePreview();
        button.click();
      });
    }
    button.onclick = async () => {
      if (recorder && recorder.state === "recording") {
        button.disabled = true;
        button.textContent = "Transcribing…";
        recorder.stop();
        return;
      }
      if (starting || recorder) return;
      if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
        return toast(
          "Audio recording is not available in this browser. You can type instead.",
          true,
        );
      }
      starting = true;
      previewOptions?.onBusyChange?.(true);
      button.dataset.idle = button.dataset.idle || button.textContent.trim();
      button.disabled = true;
      button.textContent = "Requesting microphone…";
      chunks = [];
      browserTranscript = "";
      try {
        stream = await navigator.mediaDevices.getUserMedia({
          audio: {
            echoCancellation: true,
            noiseSuppression: true,
            autoGainControl: true,
          },
        });
        const mimeCandidates = [
          "audio/webm;codecs=opus",
          "audio/ogg;codecs=opus",
          "audio/mp4",
          "audio/webm",
        ];
        const mime = mimeCandidates.find((type) =>
          MediaRecorder.isTypeSupported?.(type),
        );
        recorder = new MediaRecorder(
          stream,
          mime ? { mimeType: mime } : undefined,
        );
        const Speech =
          window.SpeechRecognition || window.webkitSpeechRecognition;
        if (Speech) {
          recognition = new Speech();
          recognition.continuous = true;
          recognition.interimResults = false;
          recognition.lang = speechLanguage();
          recognition.onresult = (event) => {
            browserTranscript = [...event.results]
              .map((result) => result[0].transcript)
              .join(" ");
          };
          recognition.onerror = () => {};
          try {
            recognition.start();
          } catch (_) {
            recognition = null;
          }
        }
        recorder.ondataavailable = (event) => {
          if (event.data.size) chunks.push(event.data);
        };
        recorder.onstop = async () => {
          try {
            recognition?.stop();
          } catch (_) {}
          stream?.getTracks().forEach((track) => track.stop());
          stream = null;
          try {
            if (!chunks.length)
              throw new Error(
                "No audio was captured. Check your microphone and try again.",
              );
            const mimeType =
              recorder?.mimeType || chunks[0]?.type || "audio/webm";
            const extension = mimeType.includes("ogg")
              ? "ogg"
              : mimeType.includes("mp4")
                ? "m4a"
                : "webm";
            const blob = new Blob(chunks, {
              type: mimeType,
            });
            if (!blob.size)
              throw new Error("The recording was empty. Please try again.");
            const form = new FormData();
            form.append("audio", blob, `voice.${extension}`);
            const result = await api(
              `/api/speech/transcribe?language=${encodeURIComponent(languageGetter())}`,
              { method: "POST", body: form },
            );
            const transcript = (
              result.transcript ||
              browserTranscript ||
              ""
            ).trim();
            if (!transcript)
              throw new Error(
                result.error ||
                  "No words were recognized. Check your microphone and try again.",
              );
            const inserted = finishTranscript(transcript);
            if (inserted && !preview)
              toast("Voice transcript inserted — edit it before sending.");
            else if (!inserted)
              toast(
                "No words were recognized. You can type your response instead.",
                true,
              );
          } catch (err) {
            const transcript = browserTranscript.trim();
            if (transcript && finishTranscript(transcript)) {
              if (!preview)
                toast("Voice transcript inserted — edit it before sending.");
            } else {
              toast(err?.message || errorText(err), true);
            }
          } finally {
            stream?.getTracks().forEach((track) => track.stop());
            stream = null;
            recorder = null;
            recognition = null;
            chunks = [];
            starting = false;
            button.disabled = false;
            button.textContent = button.dataset.idle || "◉ Record voice";
            previewOptions?.onBusyChange?.(false);
          }
        };
        recorder.onerror = () => {
          try {
            if (recorder?.state === "recording") recorder.stop();
          } catch (_) {}
        };
        recorder.start(1000);
        starting = false;
        button.disabled = false;
        button.textContent = "■ Stop recording";
      } catch (err) {
        stream?.getTracks().forEach((track) => track.stop());
        stream = null;
        recorder = null;
        recognition = null;
        starting = false;
        button.disabled = false;
        button.textContent = button.dataset.idle || "◉ Record voice";
        previewOptions?.onBusyChange?.(false);
        const errors = {
          NotAllowedError:
            "Allow microphone access for this site, then try again.",
          NotFoundError:
            "No microphone was found. Connect or enable a microphone and try again.",
          NotReadableError:
            "The microphone is busy in another app. Close that app and try again.",
          SecurityError: "Microphone access requires a secure HTTPS page.",
        };
        toast(
          errors[err?.name] ||
            err?.message ||
            "Could not start microphone recording.",
          true,
        );
      }
    };
  }
  function drawLiveArena() {
    const live = state.live,
      current = live.state || {},
      isDebate = live.kind === "debate";
    const items = isDebate ? current.speeches || [] : current.messages || [];
    const feedback = isDebate ? current.last_feedback : null;
    const poiAllowed = [
      "british_parliamentary",
      "asian_parliamentary",
      "worlds_schools",
    ].includes(current.format);
    const debateControls = isDebate
      ? `${field("Speech type", "speech_type", {
          select: true,
          value: "constructive",
          options: [
            ["constructive", "Constructive"],
            ["rebuttal", "Rebuttal"],
            ["summary", "Summary / reply"],
          ],
        })}`
      : "";
    const roomMeta = isDebate
      ? `<span class="tag">Round ${esc(current.round || 1)}</span>`
      : `<span class="tag">${esc((current.participants || []).length || 0)} participants</span>`;
    $("#arena-content").innerHTML =
      `<section class="card"><div class="row between"><div><div class="eyebrow">${isDebate ? "Live debate" : "Group discussion"}</div><h1 style="font-size:clamp(28px,4vw,40px);margin-top:7px">${esc(isDebate ? current.motion : current.topic)}</h1><div class="session-meta">${roomMeta}<span class="status-chip">Live · ${esc(current.language || "en")}</span></div></div></div><div class="live-layout" style="margin-top:20px"><div><div id="live-transcript" class="chat-thread live-transcript">${liveTranscript(items)}</div><form id="live-form" class="form">${isDebate ? `${debateControls}${field("Your speech", "live-message", { textarea: true, rows: 7, placeholder: "Set out one claim, support it, and explain why it matters…" })}<div class="answer-tools"><span class="field-hint">Voice is transcribed before sending.</span><button type="button" class="btn secondary small" id="live-voice">◉ Record speech</button></div><div class="row"><button class="btn" type="submit">Send speech&nbsp; →</button>${poiAllowed ? `<button type="button" class="btn secondary" id="poi-button">Raise a POI</button>` : ""}` : `${field("Your contribution", "live-message", { textarea: true, rows: 5, placeholder: "Enter the discussion with a clear point and example…" })}<div class="answer-tools"><span class="field-hint">Voice is transcribed before sending.</span><button type="button" class="btn secondary small" id="live-voice">◉ Record contribution</button></div><div class="row"><button class="btn" type="submit">Join discussion&nbsp; →</button>`}<button type="button" class="btn danger" id="end-live">Finish and score</button></div></form></div><aside class="card soft-card"><div class="eyebrow">Room notes</div><h2 style="font-size:23px;margin-top:7px">${isDebate ? "Find the clash" : "Build the room"}</h2><p class="body-copy" style="margin-top:10px">${isDebate ? "Answer the opponent's strongest point. A POI is a short interruption that tests their case." : "Make a point, support it, and connect it to what another participant said. Leadership includes synthesis."}</p><div class="notice" style="margin-top:16px">Your responses are recorded automatically. Use Listen to hear AI turns aloud.</div></aside></div></section>`;
    if (feedback) {
      const transcript = $("#live-transcript");
      transcript?.insertAdjacentHTML(
        "afterend",
        `<div class="notice live-feedback"><strong>Coach's turn feedback</strong><p>${esc(feedback.detailed_feedback || feedback.feedback || feedback.summary || "Your turn was recorded. Keep building the clash.")}</p>${feedback.overall_score != null ? `<span class="tag">Current room score · ${esc(feedback.overall_score)}/100</span>` : ""}</div>`,
      );
    }
    $$(".live-speak").forEach(
      (button) =>
        (button.onclick = () =>
          speakText(button.dataset.text, current.language, "lisa", true)),
    );
    bindVoiceRecorder(
      "#live-message",
      "#live-voice",
      () => current.language || "en",
    );
    $("#live-form").onsubmit = async (event) => {
      event.preventDefault();
      const text = $("#live-message").value.trim();
      if (text.length < (isDebate ? 20 : 10))
        return toast("Write a little more for the room to respond.", true);
      const button = $("button[type=submit]", event.currentTarget);
      button.disabled = true;
      button.textContent = "The room is responding…";
      try {
        const result = await api(
          `/api/live/${isDebate ? "debate/speech" : "gd/message"}`,
          {
            method: "POST",
            body: {
              public_id: live.public_id,
              [isDebate ? "speech" : "message"]: text,
              speech_type: $("#speech_type")?.value || "constructive",
            },
          },
        );
        live.state = result.state;
        drawLiveArena();
        if (result.ai_response)
          speakText(result.ai_response, live.state.language);
      } catch (err) {
        button.disabled = false;
        button.textContent = isDebate ? "Send speech →" : "Join discussion →";
        toast(errorText(err), true);
      }
    };
    $("#poi-button")?.addEventListener("click", async () => {
      const poi = prompt("Your point of information:");
      if (!poi?.trim()) return;
      try {
        const result = await api("/api/live/debate/poi", {
          method: "POST",
          body: { public_id: live.public_id, poi },
        });
        live.state = result.state;
        drawLiveArena();
        speakText(result.ai_response, live.state.language);
      } catch (err) {
        toast(errorText(err), true);
      }
    });
    $("#end-live").onclick = async () => {
      if (!confirm("Finish this live room and write its report?")) return;
      try {
        const result = await api(
          `/api/live/${isDebate ? "debate/end" : "gd/end"}`,
          { method: "POST", body: { public_id: live.public_id } },
        );
        state.session = result.session;
        toast("Report ready — saved to your history.");
        location.href = `/history/${encodeURIComponent(live.public_id)}`;
      } catch (err) {
        toast(errorText(err), true);
      }
    };
  }

  async function accountPage() {
    render(
      `<div class="container page-pad"><div id="account-content" class="loading">Loading your account…</div></div>`,
      "account",
    );
    try {
      const [me, home] = await Promise.all([
        api("/api/auth/me"),
        api("/api/home"),
      ]);
      state.user = me.user;
      drawAccount("", home);
    } catch (err) {
      $("#account-content").innerHTML = alertBox(errorText(err));
    }
  }
  function drawAccount(message = "", home = state.home || {}) {
    const u = state.user;
    const summary = home.summary || {},
      analytics = home.analytics || {},
      distribution = analytics.distribution || {};
    $("#account-content").innerHTML =
      `<section class="card"><div class="row between"><div class="row"><span class="avatar" style="width:52px;height:52px">${esc(initials(u))}</span><div><div class="eyebrow">Your account</div><h1 style="font-size:34px;margin-top:5px">${esc(u.full_name || u.email)}</h1><p class="muted" style="font-size:13px">${esc(u.email)}</p></div></div><span class="role-chip ${u.role === "admin" ? "admin" : ""}">${u.role === "admin" ? "♢ Administrator" : "⌂ Student"}</span></div></section>${alertBox(message, true)}<div class="stats" style="margin-top:20px">${stat("Total points", u.total_points || 0, `${u.level || "beginner"} level`)}${stat("Sessions", summary.total_sessions || 0, `${summary.completed_sessions || 0} completed`)}${stat("Day streak", u.practice_streak || 0, "Keep the thread going")}${stat("Average score", `${fmt(summary.average_score, 1)}%`, "Across completed rooms")}</div><section class="card soft-card" style="margin-top:20px"><div class="section-head"><div class="eyebrow">Practice record</div><h2 style="margin-top:7px">Your preparation mix</h2></div><div class="stats" style="margin-top:16px">${stat("Debates", distribution.debate || 0)}${stat("Interviews", distribution.interview || 0)}${stat("Group discussions", distribution.group_discussion || 0)}${stat("Coaching rooms", distribution.coaching || 0)}</div></section><div class="grid two-col" style="margin-top:20px"><section class="card"><h2>Profile basics</h2><p class="muted" style="margin-top:6px;font-size:13px">These details shape how your coach addresses you and which prompts we prepare.</p><form id="profile-form" class="form">${field("Full name", "full_name", { value: u.full_name, autocomplete: "name" })}<div class="form-grid">${field("Headline", "headline", { required: false, value: u.headline, placeholder: "e.g. Aspiring Data Scientist" })}${field("Target role", "target_role", { required: false, value: u.target_role, placeholder: "e.g. Graduate Analyst" })}${field("Institution", "institution", { required: false, value: u.institution, placeholder: "e.g. Oxford University" })}${field("Field of study", "field_of_study", { required: false, value: u.field_of_study, placeholder: "e.g. Computer Science" })}${field("Phone", "phone", { required: false, value: u.phone, autocomplete: "tel" })}${field("Time zone", "timezone", { required: false, value: u.timezone || "UTC" })}</div><button class="btn" type="submit">Save profile&nbsp; ✓</button></form></section><section class="card soft-card"><h2>Security</h2><p class="muted" style="margin-top:6px;font-size:13px">Rotate your password or sign out of every other device.</p><form id="password-form" class="form">${field("Current password", "current_password", { type: "password", autocomplete: "current-password" })}${field("New password", "new_password", { type: "password", autocomplete: "new-password" })}${field("Confirm new password", "confirm_new_password", { type: "password", autocomplete: "new-password" })}<button class="btn" type="submit">Update password&nbsp; ✓</button></form><button class="btn secondary" style="margin-top:18px" id="revoke-sessions">♢ Sign out other sessions</button></section></div>`;
    const securityPanel = $("#revoke-sessions")?.closest("section");
    const securityHint = $("p.muted", securityPanel);
    if (securityHint)
      securityHint.textContent = "Manage your password and signed-in devices.";
    const revokeSessionsButton = $("#revoke-sessions");
    if (revokeSessionsButton)
      revokeSessionsButton.textContent = "♢ Log out other sessions";
    $("#account-content").insertAdjacentHTML(
      "beforeend",
      `<section class="card soft-card" style="margin-top:20px"><div class="row between"><div><div class="eyebrow">Motion</div><h2 style="margin-top:7px">Animation preferences</h2><p class="muted" style="margin-top:6px;font-size:13px">Choose how much motion PrepSage uses across the studio, or replay the opening film.</p></div><div class="row"><div class="motion-switch" role="group" aria-label="Animation preference"><button type="button" class="btn small" data-motion-mode="full">Full motion</button><button type="button" class="btn secondary small" data-motion-mode="reduced">Reduced</button></div><button type="button" class="btn secondary small" data-action="replay-intro">▶ Replay intro</button></div></div></section>`,
    );
    const motionMode = window.PrepSageMotion?.getMotion?.() || "auto";
    $$("[data-motion-mode]").forEach((button) => {
      const isReduced = button.dataset.motionMode === "reduced";
      button.classList.toggle(
        "is-active",
        isReduced === (motionMode === "reduced"),
      );
      button.onclick = () => {
        window.PrepSageMotion?.setMotion(button.dataset.motionMode);
        drawAccount(message, home);
      };
    });
    const replayIntro = $('[data-action="replay-intro"]');
    if (replayIntro) {
      replayIntro.onclick = () => {
        window.PrepSageMotion?.playOpening({
          name: state.user.full_name || state.user.email,
        });
      };
    }
    $("#account-content").insertAdjacentHTML(
      "beforeend",
      '<section class="account-logout" aria-labelledby="account-logout-heading"><div><h2 id="account-logout-heading">Log out</h2><p>End your current session on this device.</p></div><button type="button" class="btn danger" data-action="logout">Log out</button></section>',
    );
    bindLogoutButton($('#account-content [data-action="logout"]'));
    $("#profile-form").onsubmit = async (e) => {
      e.preventDefault();
      try {
        const result = await api("/api/account", {
          method: "PUT",
          body: Object.fromEntries(new FormData(e.currentTarget).entries()),
        });
        state.user = result.user;
        drawAccount(result.message, home);
      } catch (err) {
        toast(errorText(err), true);
      }
    };
    $("#password-form").onsubmit = async (e) => {
      e.preventDefault();
      const d = Object.fromEntries(new FormData(e.currentTarget).entries());
      if (d.new_password !== d.confirm_new_password)
        return toast("New passwords do not match.", true);
      try {
        const result = await api("/api/account/password", {
          method: "POST",
          body: d,
        });
        drawAccount(result.message, home);
      } catch (err) {
        toast(errorText(err), true);
      }
    };
    $("#revoke-sessions").onclick = async () => {
      if (!confirm("Log out of every other device?")) return;
      try {
        const result = await api("/api/account/sessions/revoke", {
          method: "POST",
        });
        toast(result.message);
      } catch (err) {
        toast(errorText(err), true);
      }
    };
  }

  async function adminPage() {
    render(
      `<div class="container page-pad"><div id="admin-content" class="loading">Opening the operations console…</div></div>`,
      "admin",
    );
    try {
      state.admin.overview = await api("/api/admin/overview");
      drawAdmin();
    } catch (err) {
      $("#admin-content").innerHTML = alertBox(errorText(err));
    }
  }
  function drawAdmin() {
    const o = state.admin.overview || {};
    $("#admin-content").innerHTML =
      `<div class="eyebrow">Administration</div><h1 style="font-size:clamp(31px,4vw,46px);margin-top:8px">Operations console</h1><p class="body-copy" style="max-width:760px;margin-top:10px">Everything the studio runs on: the roster and its access, the prompt bank, every rehearsal on record, and the immutable audit ledger.</p><div class="admin-tabs">${[
        ["overview", "Operations map"],
        ["learners", "Learners"],
        ["content", "Prompt bank"],
        ["sessions", "Sessions & results"],
        ["audit", "Audit history"],
      ]
        .map(
          ([k, v]) =>
            `<button class="admin-tab ${state.admin.tab === k ? "active" : ""}" data-tab="${k}">${v}</button>`,
        )
        .join(
          "",
        )}</div><div id="admin-panel" style="margin-top:20px">${adminPanel()}</div>`;
    $$("[data-tab]").forEach(
      (btn) =>
        (btn.onclick = async () => {
          state.admin.tab = btn.dataset.tab;
          try {
            if (state.admin.tab === "learners")
              state.admin.learners = await api("/api/admin/learners");
            if (state.admin.tab === "content")
              state.admin.content = await api("/api/admin/content");
            if (state.admin.tab === "sessions")
              state.admin.sessions = await api("/api/admin/sessions");
            if (state.admin.tab === "audit")
              state.admin.audit = await api("/api/admin/audit");
            drawAdmin();
          } catch (err) {
            toast(errorText(err), true);
          }
        }),
    );
    bindAdmin();
  }
  function adminPanel() {
    const tab = state.admin.tab,
      o = state.admin.overview || {};
    if (tab === "overview")
      return `<div class="card"><div class="section-head"><div class="eyebrow">Operations map</div><h2 style="margin-top:7px">The studio at a glance</h2><p>Live counts drawn straight from the ledger — nothing here is seeded or simulated.</p></div><div class="metric-grid" style="margin-top:20px">${stat("Learners", o.users || 0)}${stat("Sessions", o.sessions || 0)}${stat("Completed", o.completed_sessions || 0)}${stat("Failed", o.failed_sessions || 0)}${stat("Prompt bank", o.content || 0)}</div><div class="notice" style="margin-top:20px">♢ Health: ${o.failed_sessions ? "Watching — review failed sessions." : "All systems steady."}</div></div>`;
    if (tab === "learners") {
      const data = state.admin.learners || {},
        rows = listItems(data, "learners");
      return `<section class="card"><div class="row between section-head"><div><div class="eyebrow">Learners</div><h2 style="margin-top:7px">Roster and access control</h2></div><a class="btn secondary small" href="/api/admin/export/learners" download>Download CSV</a></div><div class="table-wrap"><table class="table"><thead><tr><th>Learner</th><th>Role</th><th>Access</th><th>Sessions</th><th>Controls</th></tr></thead><tbody>${rows.map((l) => `<tr><td><strong>${esc(l.full_name)}</strong><span>${esc(l.email)}</span></td><td><span class="tag">${esc(l.role)}</span></td><td><span class="tag">${l.is_active ? "Active" : "Deactivated"}</span></td><td>${l.session_count || 0}</td><td><div class="row">${l.is_active ? `<button class="btn danger small" data-learner="${l.id}" data-active="false">Deactivate</button>` : `<button class="btn small" data-learner="${l.id}" data-active="true">Activate</button>`}<button class="btn secondary small" data-role="${l.id}" data-value="${l.role === "admin" ? "student" : "admin"}">${l.role === "admin" ? "Make student" : "Make admin"}</button></div></td></tr>`).join("") || `<tr><td colspan="5">No learners yet.</td></tr>`}</tbody></table></div>${adminPager("learners", data)}</section>`;
    }
    if (tab === "content") {
      const data = state.admin.content || {},
        rows = listItems(data, "content");
      return `<section class="card"><div class="row between section-head"><div><div class="eyebrow">Prompt bank</div><h2 style="margin-top:7px">Preparation content</h2></div><button class="btn small" data-action="new-content">＋ New prompt</button></div><div id="content-form"></div><div class="table-wrap"><table class="table"><thead><tr><th>Prompt</th><th>Mode</th><th>Level</th><th>State</th><th>Controls</th></tr></thead><tbody>${rows.map((c) => `<tr><td><strong>${esc(c.title)}</strong><span>${esc(c.prompt).slice(0, 120)}…</span></td><td>${esc(modeLabel(c.mode))}</td><td>${esc(c.difficulty)}</td><td><span class="tag">${c.is_active ? "Active" : "Retired"}</span></td><td><div class="row"><button class="btn secondary small" data-edit-content="${c.id}">Edit</button><button class="btn secondary small" data-toggle-content="${c.id}" data-active="${!c.is_active}">${c.is_active ? "Retire" : "Activate"}</button></div></td></tr>`).join("") || `<tr><td colspan="5">No prompts yet — author the first one above.</td></tr>`}</tbody></table></div>${adminPager("content", data)}</section>`;
    }
    if (tab === "sessions") {
      const data = state.admin.sessions || {},
        rows = listItems(data, "sessions");
      return `<section class="card"><div class="row between section-head"><div><div class="eyebrow">Sessions & results</div><h2 style="margin-top:7px">Every rehearsal on record</h2></div><a class="btn secondary small" href="/api/admin/export/sessions" download>Download CSV</a></div><div class="table-wrap"><table class="table"><thead><tr><th>Learner</th><th>Room</th><th>State</th><th>Score</th><th>Timing</th></tr></thead><tbody>${rows.map((s) => `<tr><td>${esc(s.learner_email)}</td><td><strong>${esc(s.title)}</strong><span>${esc(s.mode_label)}</span></td><td><span class="tag">${esc(s.status_label)}</span></td><td>${fmt(s.percentage_score)}%</td><td>${esc(s.duration_label)}</td></tr>`).join("") || `<tr><td colspan="5">No sessions yet.</td></tr>`}</tbody></table></div>${adminPager("sessions", data)}</section>`;
    }
    const data = state.admin.audit || {},
      rows = listItems(data, "records");
    return `<section class="card"><div class="row between section-head"><div><div class="eyebrow">Audit history</div><h2 style="margin-top:7px">The append-only ledger</h2></div><a class="btn secondary small" href="/api/admin/export/audit" download>Download CSV</a></div><div class="table-wrap"><table class="table"><thead><tr><th>When</th><th>Actor</th><th>Action</th><th>Outcome</th><th>Detail</th></tr></thead><tbody>${rows.map((a) => `<tr><td>${esc(a.created_at)}</td><td>${esc(a.actor_email)}</td><td>${esc(a.action)}</td><td><span class="tag">${esc(a.severity)}</span></td><td>${esc(a.description)}</td></tr>`).join("") || `<tr><td colspan="5">No audit events yet.</td></tr>`}</tbody></table></div>${adminPager("audit", data)}</section>`;
  }
  function adminPager(kind, data = {}) {
    const limit = Number(data.limit || 50),
      offset = Number(data.offset || 0),
      total = Number(data.total || 0);
    if (total <= limit && offset === 0) return "";
    return `<div class="row between" style="margin-top:16px"><span class="field-hint">Showing ${total ? offset + 1 : 0}–${Math.min(offset + limit, total)} of ${total}</span><div class="row"><button class="btn secondary small" data-admin-page="${kind}" data-offset="${Math.max(0, offset - limit)}" ${offset <= 0 ? "disabled" : ""}>Previous</button><button class="btn secondary small" data-admin-page="${kind}" data-offset="${offset + limit}" ${offset + limit >= total ? "disabled" : ""}>Next</button></div></div>`;
  }
  function bindAdmin() {
    $$("[data-learner]").forEach(
      (btn) =>
        (btn.onclick = async () => {
          if (!confirm("Apply this access change?")) return;
          try {
            await api(`/api/admin/learners/${btn.dataset.learner}`, {
              method: "PATCH",
              body: { is_active: btn.dataset.active === "true" },
            });
            state.admin.learners = await api("/api/admin/learners");
            drawAdmin();
          } catch (err) {
            toast(errorText(err), true);
          }
        }),
    );
    $$("[data-role]").forEach(
      (btn) =>
        (btn.onclick = async () => {
          if (!confirm("Apply this role change?")) return;
          try {
            await api(`/api/admin/learners/${btn.dataset.role}`, {
              method: "PATCH",
              body: { role: btn.dataset.value },
            });
            state.admin.learners = await api("/api/admin/learners");
            drawAdmin();
          } catch (err) {
            toast(errorText(err), true);
          }
        }),
    );
    $$("[data-admin-page]").forEach(
      (btn) =>
        (btn.onclick = async () => {
          if (btn.disabled) return;
          const kind = btn.dataset.adminPage;
          try {
            const limit = Number(state.admin[kind]?.limit || 50);
            state.admin[kind] = await api(
              `/api/admin/${kind}?limit=${limit}&offset=${Number(btn.dataset.offset || 0)}`,
            );
            drawAdmin();
          } catch (err) {
            toast(errorText(err), true);
          }
        }),
    );
    $('[data-action="new-content"]')?.addEventListener("click", () =>
      showContentForm(),
    );
    $$("[data-edit-content]").forEach(
      (btn) =>
        (btn.onclick = () =>
          showContentForm(
            listItems(state.admin.content, "content").find(
              (c) => String(c.id) === btn.dataset.editContent,
            ),
          )),
    );
    $$("[data-toggle-content]").forEach(
      (btn) =>
        (btn.onclick = async () => {
          try {
            await api(`/api/admin/content/${btn.dataset.toggleContent}`, {
              method: "PATCH",
              body: { is_active: btn.dataset.active === "true" },
            });
            state.admin.content = await api("/api/admin/content");
            drawAdmin();
          } catch (err) {
            toast(errorText(err), true);
          }
        }),
    );
  }
  function showContentForm(item = {}) {
    const root = $("#content-form");
    if (!root) return;
    root.innerHTML = `<form id="content-save" class="card soft-card" style="margin:16px 0"><div class="form-grid three">${field("Mode", "mode", { select: true, value: item.mode || "interview", options: Object.entries(modes).map(([k, v]) => [k, v.short]) })}${field(
      "Difficulty",
      "difficulty",
      {
        select: true,
        value: item.difficulty || "intermediate",
        options: [
          ["beginner", "Beginner"],
          ["intermediate", "Intermediate"],
          ["advanced", "Advanced"],
          ["expert", "Expert"],
        ],
      },
    )}${field("Expected duration (seconds)", "duration_seconds", { type: "number", value: item.duration_seconds || 120 })}</div><div class="form-grid three" style="margin-top:16px">${field("Title", "title", { value: item.title || "", placeholder: "Walk me through a project you led" })}${field("Category", "category", { value: item.category || "general" })}${field("Topic", "topic", { required: false, value: item.topic || "" })}</div>${field("Prompt", "prompt", { textarea: true, value: item.prompt || "", rows: 4 })}${field("Coach guidance", "guidance", { textarea: true, required: false, value: item.guidance || "", rows: 3 })}<div class="row" style="margin-top:16px"><button class="btn" type="submit">Save prompt</button><button class="btn secondary" type="button" data-action="cancel-content">Cancel</button></div></form>`;
    $("#content-save").onsubmit = async (e) => {
      e.preventDefault();
      const data = Object.fromEntries(new FormData(e.currentTarget).entries());
      try {
        await api(
          item.id ? `/api/admin/content/${item.id}` : "/api/admin/content",
          {
            method: item.id ? "PUT" : "POST",
            body: {
              ...data,
              duration_seconds: Number(data.duration_seconds),
              tags: [],
              rubric: {},
            },
          },
        );
        state.admin.content = await api("/api/admin/content");
        drawAdmin();
        toast("Prompt saved.");
      } catch (err) {
        toast(errorText(err), true);
      }
    };
    $('[data-action="cancel-content"]', root).onclick = () =>
      (root.innerHTML = "");
  }

  async function start() {
    try {
      const result = await api("/api/auth/me");
      state.user = result.user;
    } catch (_) {
      state.user = null;
    }
    try {
      if (!window.PREPSAGE_LANGUAGES)
        window.PREPSAGE_LANGUAGES = await api("/api/languages");
    } catch (_) {}
    if (window.PrepSageMotion && window.PrepSageMotion.consumeQueuedOpening) {
      const queuedOpening = window.PrepSageMotion.consumeQueuedOpening();
      if (queuedOpening && state.user) {
        window.PrepSageMotion.playOpening(queuedOpening);
      }
    }
    const path = location.pathname,
      parts = path.split("/").filter(Boolean);
    if (path === "/" || !path) return welcome();
    if (path === "/login")
      return state.user
        ? location.replace(consumePostAuthDestination())
        : authPage(false);
    if (path === "/register")
      return state.user
        ? location.replace(consumePostAuthDestination())
        : authPage(true);
    if (!state.user) {
      if (
        path === "/arena" &&
        (location.search.includes("invite=") ||
          location.search.includes("room="))
      ) {
        sessionStorage.setItem(
          "prepsage-post-auth-destination",
          `/arena${location.search}`,
        );
      }
      return location.replace("/login");
    }
    if (!["/arena", "/debate", "/gd"].includes(path)) stopSharedRoomSync();
    if (path === "/home" || path === "/dashboard") return homePage();
    if (path === "/practice") return practicePage();
    if (parts[0] === "practice" && parts[1])
      return sessionPage(decodeURIComponent(parts[1]));
    if (path === "/arena" || path === "/debate" || path === "/gd")
      return arenaPage();
    if (path === "/interview") return practicePage();
    if (path === "/coach") return coachPage();
    if (path === "/history") return historyPage();
    if (parts[0] === "history" && parts[1])
      return reportPage(decodeURIComponent(parts[1]));
    if (path === "/account" || path === "/profile") return accountPage();
    if (path === "/admin")
      return state.user.role === "admin"
        ? adminPage()
        : location.replace("/account");
    return welcome();
  }
  function consumePostAuthDestination() {
    const path = sessionStorage.getItem("prepsage-post-auth-destination") || "";
    sessionStorage.removeItem("prepsage-post-auth-destination");
    return path.startsWith("/arena?") ? path : "/home";
  }
  window.addEventListener("popstate", () => {
    const activeLink = $(".nav-link.active");
    if (activeLink) {
      try {
        sessionStorage.setItem(
          navHighlightStorageKey,
          activeLink.getAttribute("href"),
        );
      } catch (_) {}
    }
    start();
  });
  document.addEventListener("click", (event) => {
    const link = event.target.closest("a");
    if (
      !link ||
      link.origin !== location.origin ||
      link.target === "_blank" ||
      link.hasAttribute("download")
    )
      return;
    const activeLink = $(".nav-link.active");
    if (activeLink) {
      try {
        sessionStorage.setItem(
          navHighlightStorageKey,
          activeLink.getAttribute("href"),
        );
      } catch (_) {}
    }
    event.preventDefault();
    history.pushState({}, "", link.href);
    start();
  });
  start();
})();
