/* Smart Donation Recommender - front-end logic */
(function () {
  "use strict";

  const TYPES = {
    blood:   { emoji: "🩸", color: "#c8102e", label: "Blood" },
    books:   { emoji: "📚", color: "#1f5fbf", label: "Books" },
    clothes: { emoji: "👕", color: "#7a3e9d", label: "Clothes" },
    time:    { emoji: "🤝", color: "#0f8b5f", label: "Time" },
  };

  // Which blood groups each donor group can give red cells to.
  const BLOOD_COMPAT = {
    "O-": ["everyone (universal donor)"],
    "O+": ["O+", "A+", "B+", "AB+"],
    "A-": ["A-", "A+", "AB-", "AB+"],
    "A+": ["A+", "AB+"],
    "B-": ["B-", "B+", "AB-", "AB+"],
    "B+": ["B+", "AB+"],
    "AB-": ["AB-", "AB+"],
    "AB+": ["AB+"],
  };

  const $ = (id) => document.getElementById(id);
  const state = { type: "", markers: new Map(), layer: null, userMarker: null };

  // ---------------------------------------------------------------- map
  const map = L.map("map").setView([20, 0], 2);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "© OpenStreetMap contributors",
  }).addTo(map);
  state.layer = L.layerGroup().addTo(map);

  function pinIcon(type) {
    const t = TYPES[type];
    return L.divIcon({
      className: "pin",
      html: `<span style="background:${t.color}"><i>${t.emoji}</i></span>`,
      iconSize: [34, 34],
      iconAnchor: [17, 34],
      popupAnchor: [0, -32],
    });
  }

  function el(tag, props, children) {
    const node = document.createElement(tag);
    Object.assign(node, props || {});
    (children || []).forEach((c) => node.append(c));
    return node;
  }

  function link(text, href) {
    return el("a", { textContent: text, href, target: "_blank", rel: "noopener noreferrer" });
  }

  function safeUrl(url) {
    if (!url) return "";
    if (!/^https?:\/\//i.test(url)) url = "http://" + url;
    try { return new URL(url).href; } catch (e) { return ""; }
  }

  function popupFor(p) {
    const box = el("div", { className: "popup" });
    box.append(el("h3", { textContent: p.name }));
    box.append(el("p", { textContent: p.category }));
    if (p.address) box.append(el("p", { textContent: p.address }));
    if (p.opening_hours) box.append(el("p", { textContent: "Hours: " + p.opening_hours }));
    const links = el("div", { className: "links" });
    links.append(link("Get directions", `https://www.google.com/maps/dir/?api=1&destination=${p.lat},${p.lon}`));
    if (p.phone) links.append(el("a", { textContent: "Call " + p.phone, href: "tel:" + p.phone.replace(/[^\d+]/g, "") }));
    const site = safeUrl(p.website);
    if (site) links.append(link("Website", site));
    box.append(links);
    return box;
  }

  // ------------------------------------------------------------ helpers

    function tooltipFor(p) {
    const box = el("div", { className: "tip" });
    box.append(el("strong", { textContent: p.name }));
    box.append(el("div", { textContent: p.category + " · " + p.distance_km.toFixed(1) + " km away" }));
    if (p.address) box.append(el("div", { textContent: p.address }));
    if (p.opening_hours) box.append(el("div", { textContent: "Hours: " + p.opening_hours }));
    if (p.phone) box.append(el("div", { textContent: "Phone: " + p.phone }));
    box.append(el("em", { textContent: "Click for directions" }));
    return box;
  }

  // legend (bottom-left of the map)
  const legend = L.control({ position: "bottomleft" });
  legend.onAdd = function () {
    const box = el("div", { className: "legend" });
    Object.values(TYPES).forEach((t) => {
      const row = el("div");
      const dot = el("span", { textContent: t.emoji });
      dot.style.background = t.color;
      row.append(dot, t.label);
      box.append(row);
    });
    return box;
  };
  legend.addTo(map);

  function setStatus(node, text, kind) {
    node.textContent = text;
    node.className = "status" + (kind ? " " + kind : "");
  }

  async function api(url, options) {
    const res = await fetch(url, options);
    let data = {};
    try { data = await res.json(); } catch (e) { /* non-JSON error page */ }
    if (!res.ok) throw new Error(data.error || "Something went wrong (HTTP " + res.status + ").");
    return data;
  }

  function getBrowserLocation() {
    return new Promise((resolve, reject) => {
      if (!navigator.geolocation) {
        return reject(new Error("Your browser can't share its location. Type a city or address instead."));
      }
      navigator.geolocation.getCurrentPosition(
        (pos) => resolve({ lat: pos.coords.latitude, lon: pos.coords.longitude, name: "your location" }),
        () => reject(new Error("Location access was blocked. Allow it in your browser, or type a city or address.")),
        { enableHighAccuracy: false, timeout: 15000, maximumAge: 60000 }
      );
    });
  }

  async function resolveLocation() {
    const typed = $("place").value.trim();
    if (!typed) return getBrowserLocation();
    const data = await api("/api/geocode?q=" + encodeURIComponent(typed));
    if (!data.results.length) throw new Error("We couldn't find \"" + typed + "\". Try a nearby city or a fuller address.");
    const r = data.results[0];
    return { lat: r.lat, lon: r.lon, name: r.name.split(",").slice(0, 2).join(",") };
  }

  // ------------------------------------------------------- donation type
  function selectType(type) {
    state.type = type;
    document.querySelectorAll(".type").forEach((b) =>
      b.setAttribute("aria-pressed", String(b.dataset.type === type))
    );
    $("bloodRow").hidden = type !== "blood";
    updateBloodInfo();
  }

  function updateBloodInfo() {
    const box = $("bloodInfo");
    if (state.type !== "blood") { box.hidden = true; return; }
    const group = $("bloodGroup").value;
    const targets = BLOOD_COMPAT[group].join(", ");
    box.textContent =
      group + " red cells can go to: " + targets + ". " +
      "Most centres need you to be healthy, at least 18 and about 50 kg, with a gap of a few months between donations. " +
      "Confirm with the centre.";
    box.hidden = false;
  }

  // --------------------------------------------------------- find places
  function renderPlaces(places, origin) {
    state.layer.clearLayers();
    state.markers.clear();
    const list = $("results");
    list.replaceChildren();
    const t = TYPES[state.type];

    state.userMarker = L.circleMarker([origin.lat, origin.lon], {
      radius: 8, color: "#fff", weight: 3, fillColor: "#1b2a2f", fillOpacity: 1,
    }).bindTooltip("Search centre: " + origin.name).addTo(state.layer);

    const bounds = [[origin.lat, origin.lon]];
    places.forEach((p) => {
            const marker = L.marker([p.lat, p.lon], { icon: pinIcon(state.type), alt: p.name })
        .bindTooltip(() => tooltipFor(p), { direction: "top", offset: [0, -30], className: "hover-card" })
        .bindPopup(() => popupFor(p))
        .addTo(state.layer);
      state.markers.set(p.id, marker);
      bounds.push([p.lat, p.lon]);

      const title = el("h3", {}, [
        el("span", { textContent: p.name }),
        el("span", { className: "dist", textContent: p.distance_km.toFixed(1) + " km" }),
      ]);
      if (p.best_match) title.firstChild.append(el("span", { className: "badge", textContent: "Best match" }));
      const item = el("li", { className: "result", tabIndex: 0 }, [
        title,
        el("p", { className: "meta", textContent: p.category + (p.address ? " · " + p.address : "") }),
      ]);
      item.style.setProperty("--c", t.color);
      const open = () => {
        map.setView([p.lat, p.lon], Math.max(map.getZoom(), 16));
        marker.openPopup();
      };
      item.addEventListener("click", open);
      item.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } });
      list.append(item);
    });

    map.fitBounds(bounds, { padding: [40, 40], maxZoom: 15 });
  }

  async function findPlaces() {
    const status = $("status");
    if (!state.type) return setStatus(status, "Choose what you'd like to donate first.", "error");

    const btn = $("findBtn");
    btn.disabled = true;
    setStatus(status, "Finding your location…");
    try {
      const origin = await resolveLocation();
      setStatus(status, "Searching the map for " + TYPES[state.type].label.toLowerCase() + " donation places…");
      const q = new URLSearchParams({ type: state.type, lat: origin.lat, lon: origin.lon, radius: $("radius").value });
      const data = await api("/api/places?" + q);
      renderPlaces(data.places, origin);
      if (!data.count) {
        setStatus(status, "No places found within " + $("radius").value + " km of " + origin.name + ". Try a bigger distance.", "error");
      } else {
        setStatus(status, data.count + " place" + (data.count === 1 ? "" : "s") + " near " + origin.name + ". Call ahead to confirm they accept your donation.", "ok");
      }
    } catch (err) {
      setStatus(status, err.message, "error");
    } finally {
      btn.disabled = false;
    }
  }

  // ------------------------------------------------------------- stories
  const MAX_WORDS = 100, MIN_WORDS = 10;
  const countWords = (s) => (s.trim() ? s.trim().split(/\s+/).length : 0);

  function updateWordCount() {
    const n = countWords($("story").value);
    const node = $("wordCount");
    node.textContent = n + " / " + MAX_WORDS + " words";
    node.className = "hint" + (n > MAX_WORDS ? " over" : "");
  }

  function renderStories(stories) {
    const list = $("storyList");
    list.replaceChildren();
    if (!stories.length) {
      list.append(el("li", { textContent: "No stories yet. Be the first to share one." }));
      return;
    }
    stories.forEach((s) => {
      const t = TYPES[s.type];
      const when = s.created_at ? new Date(s.created_at).toLocaleDateString() : "";
      list.append(el("li", {}, [
        el("p", { textContent: s.story }),
        el("small", { textContent: "— " + s.name + (t ? ", donated " + t.label.toLowerCase() : "") + (when ? " on " + when : "") }),
      ]));
    });
  }

  async function loadStories() {
    try { renderStories((await api("/api/stories")).stories); }
    catch (e) { $("storyList").replaceChildren(el("li", { textContent: "Stories couldn't be loaded right now." })); }
  }

  async function submitStory() {
    const msg = $("storyMsg");
    const story = $("story").value;
    const n = countWords(story);
    if (n < MIN_WORDS) return setStatus(msg, "Please write at least " + MIN_WORDS + " words.", "error");
    if (n > MAX_WORDS) return setStatus(msg, "Please keep your story to " + MAX_WORDS + " words or fewer.", "error");

    const btn = $("storyBtn");
    btn.disabled = true;
    try {
      await api("/api/stories", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ story, name: $("name").value, type: state.type }),
      });
      $("story").value = "";
      updateWordCount();
      setStatus(msg, "Thank you for sharing your story!", "ok");
      loadStories();
    } catch (err) {
      setStatus(msg, err.message, "error");
    } finally {
      btn.disabled = false;
    }
  }

  // --------------------------------------------------------------- wiring
  document.querySelectorAll(".type").forEach((b) => b.addEventListener("click", () => selectType(b.dataset.type)));
  $("bloodGroup").addEventListener("change", updateBloodInfo);
  $("findBtn").addEventListener("click", findPlaces);
  $("place").addEventListener("keydown", (e) => { if (e.key === "Enter") findPlaces(); });
  $("story").addEventListener("input", updateWordCount);
  $("storyBtn").addEventListener("click", submitStory);

  updateWordCount();
  loadStories();
})();
