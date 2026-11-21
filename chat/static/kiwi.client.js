/**
 * kiwi_client.js
 * Cliente base para:
 *  1) Hidratar e atualizar o perfil via <script id="kiwi-profile-data"> e POST /api/profile/update/
 *  2) Conectar aos WebSockets (/ws/chat/... e /ws/dm/...) sem desalinhar nomes de grupo ou campos
 */
(function (global) {
  "use strict";

  function getCookie(name) {
    if (!document.cookie) return "";
    const parts = document.cookie.split(";");
    for (let i = 0; i < parts.length; i++) {
      const part = parts[i].trim();
      if (part.startsWith(name + "=")) {
        return decodeURIComponent(part.slice(name.length + 1));
      }
    }
    return "";
  }

  function getCsrfToken() {
    const input = document.querySelector('input[name="csrfmiddlewaretoken"]');
    if (input && input.value) return input.value;
    return getCookie("csrftoken");
  }

  function readInitialProfileData(scriptId = "kiwi-profile-data") {
    const el = document.getElementById(scriptId);
    if (!el) return {};
    try {
      const raw = (el.textContent || "{}").trim();
      return JSON.parse(raw || "{}");
    } catch (err) {
      console.warn("Falha ao ler perfil inicial:", err);
      return {};
    }
  }

  function normalizeProfilePayload(raw = {}, prev = {}) {
    const pickStr = (keys, fallback = "") => {
      for (const k of keys) {
        if (Object.prototype.hasOwnProperty.call(raw, k) && raw[k] !== undefined && raw[k] !== null) {
          return String(raw[k]);
        }
      }
      return fallback;
    };

    const pickBool = (keys, fallback = false) => {
      for (const k of keys) {
        if (Object.prototype.hasOwnProperty.call(raw, k) && raw[k] !== undefined && raw[k] !== null) {
          return Boolean(raw[k]);
        }
      }
      return fallback;
    };

    return {
      username: pickStr(["username"], prev.username || ""),
      bubble: pickStr(["bubble"], prev.bubble || ""),
      pronouns: pickStr(["pronouns"], prev.pronouns || ""),
      bio: pickStr(["bio"], prev.bio || ""),
      song: pickStr(["song", "spotify_song"], prev.song || ""),
      artist: pickStr(["artist", "spotify_artist"], prev.artist || ""),
      musicUrl: pickStr(["musicUrl", "music_url"], prev.musicUrl || ""),
      audioSrc: pickStr(["audioSrc", "music_audio_url"], prev.audioSrc || ""),
      coverUrl: pickStr(["coverUrl", "music_cover_url"], prev.coverUrl || ""),
      accentColor: pickStr(["accentColor", "accent_color"], prev.accentColor || "#ff9de2"),
      avatarUrl: pickStr(["avatarUrl", "avatar_url"], prev.avatarUrl || ""),
      bannerUrl: pickStr(["bannerUrl", "banner_url"], prev.bannerUrl || ""),
      frameEffect: pickStr(["frame_effect"], prev.frameEffect || "none"),
      profileGlow: pickBool(["profileGlow", "profile_glow"], Boolean(prev.profileGlow)),
      avatarPulse: pickBool(["avatarPulse", "avatar_pulse"], Boolean(prev.avatarPulse)),
      nameStyle: pickStr(["nameStyle", "name_style"], prev.nameStyle || ""),
      isAdmin: pickBool(["is_admin", "isAdmin"], Boolean(prev.isAdmin)),
      isAkiiraAdmin: pickBool(["is_akiira_admin", "isAkiiraAdmin"], Boolean(prev.isAkiiraAdmin)),
    };
  }

  async function submitProfileUpdate(fieldsOrFormData, url = "/api/profile/update/") {
    let formData;
    if (fieldsOrFormData instanceof FormData) {
      formData = fieldsOrFormData;
    } else {
      formData = new FormData();
      Object.entries(fieldsOrFormData || {}).forEach(([key, val]) => {
        if (val === undefined || val === null) return;
        if (val instanceof File || val instanceof Blob) {
          formData.append(key, val);
        } else if (typeof val === "boolean") {
          formData.append(key, val ? "true" : "false");
        } else {
          formData.append(key, String(val));
        }
      });
    }

    const csrfToken = getCsrfToken();
    const res = await fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: csrfToken ? { "X-CSRFToken": csrfToken } : {},
      body: formData,
    });

    let data = {};
    try {
      data = await res.json();
    } catch (_) {
      data = { status: "error", message: `Resposta inválida (${res.status})` };
    }

    if (!res.ok || data.status !== "success") {
      const err = new Error(data.message || `Erro HTTP ${res.status}`);
      err.status = res.status;
      err.data = data;
      throw err;
    }

    return data;
  }

  function buildWsUrl(pathWithQuery) {
    const proto = global.location.protocol === "https:" ? "wss:" : "ws:";
    const cleanPath = pathWithQuery.startsWith("/") ? pathWithQuery : `/${pathWithQuery}`;
    return `${proto}//${global.location.host}${cleanPath}`;
  }

  function createChatSocket({ roomSlug = "geral", server = "home", channel = "geral", onEvent }) {
    const params = new URLSearchParams({ server: String(server), channel: String(channel) });
    const ws = new WebSocket(buildWsUrl(`/ws/chat/${encodeURIComponent(roomSlug)}/?${params.toString()}`));

    ws.onmessage = (ev) => {
      try {
        const parsed = JSON.parse(ev.data);
        if (typeof onEvent === "function") onEvent(parsed);
      } catch (_) {}
    };

    return {
      socket: ws,
      sendMessage(payload) {
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify(Object.assign({ type: "chat_message" }, payload)));
        }
      },
      deleteMessage(id) {
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: "delete_message", id }));
        }
      },
      close() {
        ws.close();
      },
    };
  }

  function createDmSocket({ username, onEvent }) {
    const ws = new WebSocket(buildWsUrl(`/ws/dm/${encodeURIComponent(username)}/`));

    ws.onmessage = (ev) => {
      try {
        const parsed = JSON.parse(ev.data);
        if (typeof onEvent === "function") onEvent(parsed);
      } catch (_) {}
    };

    return {
      socket: ws,
      sendDm(payload) {
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify(Object.assign({ type: "dm_message" }, payload)));
        }
      },
      deleteDm(id) {
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: "delete_dm", id }));
        }
      },
      close() {
        ws.close();
      },
    };
  }

  global.KiwiClient = {
    getCsrfToken,
    readInitialProfileData,
    normalizeProfilePayload,
    submitProfileUpdate,
    buildWsUrl,
    createChatSocket,
    createDmSocket,
  };
})(window);