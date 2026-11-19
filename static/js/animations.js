/* ==========================================================================
   PrepSage Motion Engine
   A dependency-free companion to app.js. It ports the useful ideas from the
   supplied animation packs (particle field, scroll progress, counters, typing,
   ripple, parallax, reveal-on-scroll) and adds the post-login opening film.
   Everything degrades gracefully and honours prefers-reduced-motion.
   ========================================================================== */
(function () {
  "use strict";

  const MOTION_KEY = "prepsage-motion";
  const OPENING_KEY = "prepsage-opening-pending";
  const reduceMedia = window.matchMedia("(prefers-reduced-motion: reduce)");
  const timers = new Set();
  const observers = [];
  const parallaxNodes = new Set();

  let particleField = null;
  let scrollBound = false;
  let rippleBound = false;
  let watchBound = false;
  let parallaxBound = false;

  const later = (fn, ms) => {
    const id = setTimeout(() => {
      timers.delete(id);
      fn();
    }, ms);
    timers.add(id);
    return id;
  };

  const clearTimers = () => {
    timers.forEach((id) => clearTimeout(id));
    timers.clear();
  };

  const escapeHtml = (value) =>
    String(value == null ? "" : value).replace(
      /[&<>"']/g,
      (ch) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[ch],
    );

  function getMotion() {
    try {
      return localStorage.getItem(MOTION_KEY) || "auto";
    } catch (_) {
      return "auto";
    }
  }

  function reduced() {
    const mode = getMotion();
    if (mode === "reduced") return true;
    if (mode === "full") return false;
    return reduceMedia.matches;
  }

  /* ------------------------------------------------------------------------
     Ambient particle field
     ------------------------------------------------------------------------ */
  class ParticleField {
    constructor(host) {
      this.host = host;
      this.canvas = document.createElement("canvas");
      this.ctx = this.canvas.getContext("2d");
      this.dots = [];
      this.pointer = { x: -9999, y: -9999 };
      this.raf = 0;
      this.running = true;
      this.dpr = Math.min(window.devicePixelRatio || 1, 2);
      this.onResize = this.onResize.bind(this);
      this.onMove = this.onMove.bind(this);
      this.onVisibility = this.onVisibility.bind(this);
      this.frame = this.frame.bind(this);
      host.appendChild(this.canvas);
      window.addEventListener("resize", this.onResize);
      window.addEventListener("pointermove", this.onMove, { passive: true });
      document.addEventListener("visibilitychange", this.onVisibility);
      this.onResize();
      this.raf = requestAnimationFrame(this.frame);
    }

    onResize() {
      const w = window.innerWidth;
      const h = window.innerHeight;
      this.w = w;
      this.h = h;
      this.canvas.width = Math.round(w * this.dpr);
      this.canvas.height = Math.round(h * this.dpr);
      this.canvas.style.width = w + "px";
      this.canvas.style.height = h + "px";
      this.ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
      const count = Math.max(180, Math.min(400, Math.round((w * h) / 5500)));
      this.dots = Array.from({ length: count }, () => this.spawn());
    }

    spawn() {
      const palette = [
        "123,213,255",
        "255,207,104",
        "237,245,255",
        "169,139,255",
      ];
      return {
        x: Math.random() * this.w,
        y: Math.random() * this.h,
        vx: (Math.random() - 0.5) * 0.22,
        vy: (Math.random() - 0.5) * 0.22,
        r: Math.random() * 2.0 + 0.75,
        a: Math.random() * 0.4 + 0.15,
        c: palette[Math.floor(Math.random() * palette.length)],
      };
    }

    onMove(event) {
      this.pointer.x = event.clientX;
      this.pointer.y = event.clientY;
    }

    onVisibility() {
      this.running = !document.hidden;
      if (this.running) this.raf = requestAnimationFrame(this.frame);
    }

    frame() {
      if (!this.running) return;
      const ctx = this.ctx;
      const w = this.w;
      const h = this.h;
      ctx.clearRect(0, 0, w, h);

      for (const dot of this.dots) {
        dot.x += dot.vx;
        dot.y += dot.vy;
        if (dot.x < -20) dot.x = w + 20;
        if (dot.x > w + 20) dot.x = -20;
        if (dot.y < -20) dot.y = h + 20;
        if (dot.y > h + 20) dot.y = -20;

        const dx = dot.x - this.pointer.x;
        const dy = dot.y - this.pointer.y;
        const dist = Math.hypot(dx, dy);
        if (dist < 120 && dist > 0.1) {
          const push = ((120 - dist) / 120) * 0.55;
          dot.x += (dx / dist) * push;
          dot.y += (dy / dist) * push;
        }

        ctx.beginPath();
        ctx.fillStyle = `rgba(${dot.c},${dot.a})`;
        ctx.arc(dot.x, dot.y, dot.r, 0, Math.PI * 2);
        ctx.fill();
      }

      for (let i = 0; i < this.dots.length; i += 1) {
        for (let j = i + 1; j < this.dots.length; j += 1) {
          const a = this.dots[i];
          const b = this.dots[j];
          const dist = Math.hypot(a.x - b.x, a.y - b.y);
          if (dist < 135) {
            ctx.beginPath();
            ctx.strokeStyle = `rgba(123,213,255,${((1 - dist / 135) * 0.13).toFixed(3)})`;
            ctx.lineWidth = 0.6;
            ctx.moveTo(a.x, a.y);
            ctx.lineTo(b.x, b.y);
            ctx.stroke();
          }
        }
      }

      this.raf = requestAnimationFrame(this.frame);
    }

    destroy() {
      cancelAnimationFrame(this.raf);
      window.removeEventListener("resize", this.onResize);
      window.removeEventListener("pointermove", this.onMove);
      document.removeEventListener("visibilitychange", this.onVisibility);
      this.canvas.remove();
    }
  }

  function mountParticles() {
    if (reduced() || particleField) return;
    let host = document.querySelector(".ps-particles");
    if (!host) {
      host = document.createElement("div");
      host.className = "ps-particles";
      host.setAttribute("aria-hidden", "true");
      document.body.appendChild(host);
    }
    particleField = new ParticleField(host);
  }

  function unmountParticles() {
    if (!particleField) return;
    particleField.destroy();
    particleField = null;
  }

  /* ------------------------------------------------------------------------
     Scroll progress
     ------------------------------------------------------------------------ */
  function mountScrollProgress() {
    if (document.querySelector(".ps-scroll-progress")) return;
    const bar = document.createElement("div");
    bar.className = "ps-scroll-progress";
    bar.setAttribute("aria-hidden", "true");
    document.body.appendChild(bar);
    let ticking = false;
    const update = () => {
      ticking = false;
      const doc = document.documentElement;
      const max = doc.scrollHeight - window.innerHeight;
      const pct =
        max > 0 ? Math.min(100, Math.max(0, (window.scrollY / max) * 100)) : 0;
      bar.style.width = pct + "%";
    };
    window.addEventListener(
      "scroll",
      () => {
        if (ticking) return;
        ticking = true;
        requestAnimationFrame(update);
      },
      { passive: true },
    );
    window.addEventListener("resize", update);
    update();
  }

  /* ------------------------------------------------------------------------
     Reveal on scroll
     ------------------------------------------------------------------------ */
  const REVEAL_SELECTOR = [
    ".card",
    ".stat",
    ".goal",
    ".session-card",
    ".achievement",
    ".list > li",
    ".empty",
    ".notice",
    ".practice-card",
    ".panel",
    ".form-card",
    ".account-logout",
  ].join(", ");

  function reveal(root) {
    if (reduced()) return;
    const scope = root || document;
    const nodes = Array.from(scope.querySelectorAll(REVEAL_SELECTOR)).filter(
      (el) => {
        if (el.closest(".ps-opening")) return false;
        if (el.closest(".ps-home .ps-hero")) return false;
        return true;
      },
    );
    if (!nodes.length) return;

    nodes.forEach((el, index) => {
      if (el.classList.contains("ps-reveal")) return;
      el.classList.add("ps-reveal");
      el.style.setProperty("--d", Math.min(index, 8) * 0.045 + "s");
    });

    const pending = nodes.filter((el) => !el.classList.contains("ps-in"));
    if (!pending.length) return;

    if (!("IntersectionObserver" in window)) {
      pending.forEach((el) => el.classList.add("ps-in"));
      return;
    }

    const io = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (!entry.isIntersecting) return;
          entry.target.classList.add("ps-in");
          io.unobserve(entry.target);
        });
      },
      { threshold: 0.08, rootMargin: "0px 0px -4% 0px" },
    );

    pending.forEach((el, index) => {
      const rect = el.getBoundingClientRect();
      const inView = rect.top < window.innerHeight * 0.95 && rect.bottom > 0;
      if (inView) {
        later(() => el.classList.add("ps-in"), 40 + index * 45);
      } else {
        io.observe(el);
      }
    });

    observers.push(io);
  }

  /* ------------------------------------------------------------------------
     Animated counters
     ------------------------------------------------------------------------ */
  function counters(root) {
    if (reduced()) return;
    const scope = root || document;
    scope
      .querySelectorAll(".stat strong, .readiness-box strong")
      .forEach((el) => {
        if (el.dataset.countDone) return;
        const raw = el.textContent.trim();
        const match = raw.match(/^(-?[\d,]+(?:\.\d+)?)(.*)$/);
        if (!match) return;
        const target = parseFloat(match[1].replace(/,/g, ""));
        if (!Number.isFinite(target) || target === 0) return;

        el.dataset.countDone = "1";
        const decimals = (match[1].split(".")[1] || "").length;
        const suffix = match[2] || "";
        const hasComma = match[1].includes(",");
        const start = performance.now();
        const duration = 1100;

        const step = (now) => {
          const t = Math.min(1, (now - start) / duration);
          const eased = 1 - Math.pow(1 - t, 3);
          const value = target * eased;
          el.textContent =
            (hasComma
              ? Math.round(value).toLocaleString("en-US")
              : value.toFixed(decimals)) + suffix;
          if (t < 1) {
            requestAnimationFrame(step);
          } else {
            el.textContent =
              (hasComma
                ? target.toLocaleString("en-US")
                : target.toFixed(decimals)) + suffix;
          }
        };
        requestAnimationFrame(step);
      });
  }

  /* ------------------------------------------------------------------------
     Typing effect
     ------------------------------------------------------------------------ */
  function typing(root) {
    if (reduced()) return;
    const scope = root || document;
    scope.querySelectorAll("[data-typing]").forEach((el) => {
      if (el.dataset.typingReady) return;
      const phrases = (el.dataset.typing || "").split("|").filter(Boolean);
      if (!phrases.length) return;
      el.dataset.typingReady = "1";

      let phrase = 0;
      let char = 0;
      let deleting = false;

      const tick = () => {
        if (!el.isConnected) return;
        const current = phrases[phrase];
        if (!deleting) {
          char += 1;
          el.textContent = current.slice(0, char);
          if (char >= current.length) {
            deleting = true;
            later(tick, 1700);
            return;
          }
          later(tick, 62 + Math.random() * 42);
        } else {
          char -= 1;
          el.textContent = current.slice(0, char);
          if (char <= 0) {
            deleting = false;
            phrase = (phrase + 1) % phrases.length;
            later(tick, 280);
            return;
          }
          later(tick, 28);
        }
      };

      later(tick, 520);
    });
  }

  /* ------------------------------------------------------------------------
     Cursor spotlight
     ------------------------------------------------------------------------ */
  function spotlight(root) {
    if (reduced()) return;
    const scope = root || document;
    scope
      .querySelectorAll(".card, .stat, .ps-feature, .ps-room, .achievement")
      .forEach((el) => {
        if (el.dataset.spotlight) return;
        el.dataset.spotlight = "1";
        el.classList.add("ps-spotlight");
        el.addEventListener("pointermove", (event) => {
          const rect = el.getBoundingClientRect();
          el.style.setProperty("--mx", event.clientX - rect.left + "px");
          el.style.setProperty("--my", event.clientY - rect.top + "px");
        });
      });
  }

  /* ------------------------------------------------------------------------
     Parallax
     ------------------------------------------------------------------------ */
  function parallax(root) {
    if (reduced()) return;
    const scope = root || document;
    scope.querySelectorAll("[data-parallax]").forEach((el) => {
      el.classList.add("ps-parallax");
      parallaxNodes.add(el);
    });
    if (parallaxBound) return;
    parallaxBound = true;
    let ticking = false;
    const update = () => {
      ticking = false;
      const y = window.scrollY || window.pageYOffset;
      parallaxNodes.forEach((el) => {
        if (!el.isConnected) {
          parallaxNodes.delete(el);
          return;
        }
        const speed = parseFloat(el.dataset.parallax) || 0.06;
        el.style.transform = `translate3d(0, ${(-y * speed).toFixed(2)}px, 0)`;
      });
    };
    window.addEventListener(
      "scroll",
      () => {
        if (ticking) return;
        ticking = true;
        requestAnimationFrame(update);
      },
      { passive: true },
    );
    window.addEventListener("resize", update);
    update();
  }

  /* ------------------------------------------------------------------------
     Magnetic buttons
     ------------------------------------------------------------------------ */
  function magnetic(root) {
    if (reduced()) return;
    const scope = root || document;
    scope.querySelectorAll(".ps-magnetic").forEach((el) => {
      if (el.dataset.magnetic) return;
      el.dataset.magnetic = "1";
      el.addEventListener("pointermove", (event) => {
        const rect = el.getBoundingClientRect();
        const x = (event.clientX - rect.left - rect.width / 2) * 0.12;
        const y = (event.clientY - rect.top - rect.height / 2) * 0.18;
        el.style.transform = `translate(${x.toFixed(1)}px, ${y.toFixed(1)}px)`;
      });
      el.addEventListener("pointerleave", () => {
        el.style.transform = "";
      });
    });
  }

  /* ------------------------------------------------------------------------
     Ripple
     ------------------------------------------------------------------------ */
  function bindRipple() {
    if (rippleBound) return;
    rippleBound = true;
    document.addEventListener(
      "pointerdown",
      (event) => {
        if (reduced()) return;
        const target = event.target.closest(
          ".btn, .ps-btn, .ps-room, .ps-feature, .nav-link",
        );
        if (!target) return;
        const rect = target.getBoundingClientRect();
        const size = Math.max(rect.width, rect.height) * 2.2;
        const ripple = document.createElement("span");
        ripple.className = "ps-ripple";
        ripple.style.width = size + "px";
        ripple.style.height = size + "px";
        ripple.style.left = event.clientX - rect.left - size / 2 + "px";
        ripple.style.top = event.clientY - rect.top - size / 2 + "px";
        target.appendChild(ripple);
        setTimeout(() => ripple.remove(), 780);
      },
      { passive: true },
    );
  }

  /* ------------------------------------------------------------------------
     In-page smooth scrolling (kept out of the SPA router)
     ------------------------------------------------------------------------ */
  function bindScrollLinks() {
    document.addEventListener("click", (event) => {
      const trigger = event.target.closest("[data-scroll-to]");
      if (!trigger) return;
      const target = document.getElementById(trigger.dataset.scrollTo);
      if (!target) return;
      event.preventDefault();
      target.scrollIntoView({
        behavior: reduced() ? "auto" : "smooth",
        block: "start",
      });
    });
  }

  /* ------------------------------------------------------------------------
     Page transition
     ------------------------------------------------------------------------ */
  function enterPage() {
    if (reduced()) return;
    const main = document.querySelector("#app .main");
    if (!main) return;
    main.classList.remove("ps-page-enter");
    void main.offsetWidth;
    main.classList.add("ps-page-enter");
    later(() => main.classList.remove("ps-page-enter"), 700);
  }

  /* ------------------------------------------------------------------------
     Orchestration
     ------------------------------------------------------------------------ */
  function enhance(root) {
    const scope = root || document.getElementById("app") || document;
    reveal(scope);
    counters(scope);
    typing(scope);
    spotlight(scope);
    parallax(scope);
    magnetic(scope);
  }

  function reset() {
    clearTimers();
    observers.forEach((io) => io.disconnect());
    observers.length = 0;
  }

  function afterRender(root) {
    reset();
    enterPage();
    enhance(root || document.getElementById("app") || document);
  }

  function watchDom() {
    if (watchBound) return;
    const app = document.getElementById("app");
    if (!app) return;
    watchBound = true;
    let timer = null;
    const mo = new MutationObserver((mutations) => {
      let relevant = false;
      for (const mutation of mutations) {
        for (const node of mutation.addedNodes) {
          if (node.nodeType === 1) {
            relevant = true;
            break;
          }
        }
        if (relevant) break;
      }
      if (!relevant) return;
      clearTimeout(timer);
      timer = setTimeout(() => {
        const root = document.getElementById("app");
        if (root) enhance(root);
      }, 120);
    });
    mo.observe(app, { childList: true, subtree: true });
  }

  /* ------------------------------------------------------------------------
     Opening film
     ------------------------------------------------------------------------ */
  function queueOpening(name) {
    try {
      sessionStorage.setItem(OPENING_KEY, JSON.stringify({ name: name || "" }));
    } catch (_) {}
  }

  function consumeQueuedOpening() {
    try {
      const raw = sessionStorage.getItem(OPENING_KEY);
      if (!raw) return null;
      sessionStorage.removeItem(OPENING_KEY);
      return JSON.parse(raw);
    } catch (_) {
      return null;
    }
  }

  function playOpening(options) {
    const opts = options || {};
    return new Promise((resolve) => {
      if (reduced() || document.querySelector(".ps-opening")) {
        resolve();
        return;
      }

      const name = String(opts.name || "").trim();
      const first = name ? name.split(/\s+/)[0] : "there";
      const letters = "PrepSage"
        .split("")
        .map(
          (ch, index) =>
            `<span class="${index >= 4 ? "is-accent" : ""}" style="--i:${index}">${ch}</span>`,
        )
        .join("");

      const dust = Array.from({ length: 125 }, () => {
        const left = (Math.random() * 100).toFixed(2);
        const top = (Math.random() * 100).toFixed(2);
        const size = (Math.random() * 2.4 + 1).toFixed(1);
        const dur = (Math.random() * 5 + 5).toFixed(1);
        const delay = (Math.random() * 4).toFixed(1);
        const color = ["#7bd5ff", "#ffcf68", "#a98bff", "#edf5ff"][
          Math.floor(Math.random() * 4)
        ];
        return `<i style="left:${left}%;top:${top}%;width:${size}px;height:${size}px;background:${color};color:${color};--dur:${dur}s;--delay:${delay}s"></i>`;
      }).join("");

      const overlay = document.createElement("div");
      overlay.className = "ps-opening";
      overlay.setAttribute("role", "dialog");
      overlay.setAttribute("aria-modal", "true");
      overlay.setAttribute("aria-label", "Welcome to PrepSage");
      overlay.innerHTML = `
        <div class="ps-opening__panel ps-opening__panel--l" aria-hidden="true"></div>
        <div class="ps-opening__panel ps-opening__panel--r" aria-hidden="true"></div>
        <div class="ps-opening__glow" aria-hidden="true"></div>
        <div class="ps-opening__particles" aria-hidden="true">${dust}</div>
        <div class="ps-opening__stage">
          <div class="ps-opening__kicker">Preparation studio</div>
          <div class="ps-opening__rule" aria-hidden="true"></div>
          <div class="ps-opening__mark" aria-hidden="true"><i></i><i></i><i></i><span>✦</span></div>
          <div class="ps-opening__word" aria-label="PrepSage">${letters}</div>
          <p class="ps-opening__tag">Prepare today. Perform tomorrow.</p>
          <p class="ps-opening__welcome">Welcome back, <b>${escapeHtml(first)}</b></p>
          <div class="ps-opening__bar" aria-hidden="true"><i></i></div>
          <button class="ps-opening__skip" type="button">Skip intro</button>
        </div>`;

      document.body.appendChild(overlay);
      document.body.classList.add("ps-opening-open");

      let finished = false;
      let autoTimer = null;
      let cleanupTimer = null;

      const cleanup = () => {
        window.removeEventListener("keydown", onKey);
        overlay.removeEventListener("click", finish);
        overlay.remove();
        document.body.classList.remove("ps-opening-open");
        resolve();
      };

      const finish = () => {
        if (finished) return;
        finished = true;
        if (autoTimer) clearTimeout(autoTimer);
        overlay.classList.add("is-leaving");
        cleanupTimer = setTimeout(cleanup, 980);
      };

      const onKey = (event) => {
        if (
          event.key === "Escape" ||
          event.key === "Enter" ||
          event.key === " "
        ) {
          event.preventDefault();
          finish();
        }
      };

      window.addEventListener("keydown", onKey);
      overlay.addEventListener("click", finish);
      autoTimer = setTimeout(finish, 2950);

      const skip = overlay.querySelector(".ps-opening__skip");
      if (skip) {
        setTimeout(() => {
          if (skip.isConnected) skip.focus({ preventScroll: true });
        }, 420);
      }

      // Keep the cleanup timer referenced so linters do not flag it.
      void cleanupTimer;
    });
  }

  /* ------------------------------------------------------------------------
     Motion preference
     ------------------------------------------------------------------------ */
  function applyMotionMode() {
    const mode = getMotion();
    document.documentElement.dataset.motion =
      mode === "reduced" ? "reduced" : "full";
    if (reduced()) {
      unmountParticles();
      document
        .querySelectorAll(".ps-reveal")
        .forEach((el) => el.classList.add("ps-in"));
    } else {
      mountParticles();
    }
  }

  function setMotion(mode) {
    try {
      localStorage.setItem(MOTION_KEY, mode);
    } catch (_) {}
    applyMotionMode();
    if (mode === "reduced") {
      document
        .querySelectorAll(".ps-reveal")
        .forEach((el) => el.classList.add("ps-in"));
    } else {
      afterRender(document.getElementById("app"));
    }
  }

  /* ------------------------------------------------------------------------
     Boot
     ------------------------------------------------------------------------ */
  function boot() {
    applyMotionMode();
    mountScrollProgress();
    bindRipple();
    bindScrollLinks();
    watchDom();
    const app = document.getElementById("app");
    if (app) enhance(app);
  }

  if (reduceMedia.addEventListener) {
    reduceMedia.addEventListener("change", applyMotionMode);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }

  window.PrepSageMotion = {
    playOpening,
    queueOpening,
    consumeQueuedOpening,
    afterRender,
    enhance,
    setMotion,
    getMotion,
    reduced,
  };
})();
