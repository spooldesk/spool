/* Progressive enhancement: all content and navigation are available without JS. */
(() => {
  "use strict";
  const toggle = document.getElementById('theme-toggle');
  function themeState(){
    const dark=document.documentElement.dataset.theme==='dark';
    toggle?.setAttribute('aria-pressed',String(dark));
    const surface = getComputedStyle(document.documentElement).getPropertyValue('--sd-canvas').trim();
    document.querySelector('meta[name="theme-color"]')?.setAttribute('content',surface || (dark?'#09090b':'#fafafa'));
  }
  themeState();
  toggle?.addEventListener('click',()=>{
    const theme=document.documentElement.dataset.theme==='dark'?'light':'dark';
    document.documentElement.dataset.theme=theme;
    try{localStorage.setItem('spooldesk-theme',theme);}catch(_){}
    themeState();
  });
  const motion = window.matchMedia("(prefers-reduced-motion: reduce)");
  const ticket = document.querySelector(".sample-ticket");
  document.querySelectorAll("[data-channel]").forEach(button => {
    button.addEventListener("click", () => {
      document.querySelectorAll("[data-channel]").forEach(item => item.setAttribute("aria-pressed", String(item === button)));
      document.getElementById("ticket-subject").textContent = document.getElementById(`sample-${button.dataset.channel}`).textContent;
      document.getElementById("ticket-source").textContent = button.childNodes[1].textContent.trim();
      if (!motion.matches) {
        ticket.classList.remove("is-changing");
        void ticket.offsetWidth;
        ticket.classList.add("is-changing");
      }
    });
  });
  const preview = document.getElementById("product-preview");
  let latestRequest = 0;
  document.querySelectorAll("[data-preview]").forEach(button => {
    button.addEventListener("click", () => {
      const request = ++latestRequest;
      const next = new Image();
      preview.classList.add("is-loading");
      next.onload = () => {
        if (request !== latestRequest) return;
        preview.src = button.dataset.preview;
        preview.alt = button.dataset.alt;
        preview.classList.remove("is-loading");
        document.getElementById("preview-name").textContent = button.dataset.name;
        document.getElementById("full-preview").href = button.dataset.preview;
        document.querySelectorAll("[data-preview]").forEach(item => item.setAttribute("aria-pressed", String(item === button)));
      };
      next.onerror = () => { if (request === latestRequest) preview.classList.remove("is-loading"); };
      next.src = button.dataset.preview;
    });
  });
  if ("IntersectionObserver" in window && !motion.matches) {
    const observer = new IntersectionObserver(entries => {
      entries.forEach(entry => {
        if (entry.isIntersecting) {
          entry.target.classList.remove("will-reveal");
          observer.unobserve(entry.target);
        }
      });
    }, { threshold: 0.08 });
    document.querySelectorAll(".reveal").forEach(element => {
      if (element.getBoundingClientRect().top > window.innerHeight) {
        observer.observe(element);
        element.classList.add("will-reveal");
      }
    });
    motion.addEventListener("change", event => {
      if (event.matches) {
        document.querySelectorAll(".will-reveal").forEach(element => element.classList.remove("will-reveal"));
        observer.disconnect();
      }
    });
  }
})();
