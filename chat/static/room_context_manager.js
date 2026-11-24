/**
 * Gerenciador de estado isolado por contexto (Servidor + Canal / DM) e roteamento de WebSocket.
 * Projetado para evitar vazamento de histórico, cabeçalhos e eventos entre salas e DMs.
 */
class RoomContextManager {
  constructor(options = {}) {
    this.storageKey = options.storageKey || 'kiwi_room_contexts_v1';
    this.client = options.client || window.KiwiClient || null;
    this.chatLogEl = options.chatLogEl || document.getElementById('chat-log') || document.getElementById('chatLog');
    this.onContextChange = options.onContextChange || null;
    this.onIncomingMessage = options.onIncomingMessage || null;
    this.onUnreadChange = options.onUnreadChange || null;
    this.onConnectionStateChange = options.onConnectionStateChange || null;

    this.activeContextKey = null;
    this.contexts = this._loadFromStorage();
    this.sockets = new Map();
    this.activeSocketKey = null;
    this.reconnectTimers = new Map();
    this.reconnectAttempts = new Map();
    this.maxReconnectAttempts = options.maxReconnectAttempts || 8;
    this.maxMessagesPerContext = options.maxMessagesPerContext || 300;
    this.manualDisconnects = new Set();
  }

  static buildContextKey({ type, serverId = 'home', channelName = 'geral', targetUser = '' }) {
    if (type === 'dm') {
      const normalizedUser = String(targetUser || '').trim().toLowerCase();
      return `dm:${normalizedUser || 'unknown'}`;
    }
    const normalizedServer = String(serverId || 'home').trim().toLowerCase();
    const normalizedChannel = String(channelName || 'geral').trim().toLowerCase();
    return `srv:${normalizedServer}:ch:${normalizedChannel}`;
  }

  _sanitizeSnapshotHtml(html) {
    if (!html || typeof html !== 'string') return '';
    if (html.length > 250000) return '';
    return html;
  }

  _loadFromStorage() {
    try {
      const raw = window.localStorage.getItem(this.storageKey);
      if (!raw) return {};
      const parsed = JSON.parse(raw);
      return parsed && typeof parsed === 'object' ? parsed : {};
    } catch (_) {
      return {};
    }
  }

  _saveToStorage() {
    try {
      const compact = {};
      Object.keys(this.contexts).forEach((key) => {
        const ctx = this.contexts[key];
        if (!ctx) return;
        const safeMessages = Array.isArray(ctx.messages)
          ? ctx.messages.slice(-this.maxMessagesPerContext).map((m) => ({
              id: m.id,
              author: m.author,
              text: m.text,
              timestamp: m.timestamp,
              avatarUrl: m.avatarUrl || '',
              attachmentUrl: m.attachmentUrl && String(m.attachmentUrl).startsWith('data:') ? '' : (m.attachmentUrl || ''),
              attachmentName: m.attachmentName || '',
              attachmentType: m.attachmentType || '',
              canDelete: Boolean(m.canDelete)
            }))
          : [];

        compact[key] = {
          key: ctx.key,
          type: ctx.type || 'channel',
          serverId: ctx.serverId || 'home',
          channelName: ctx.channelName || 'geral',
          channelType: ctx.channelType || 'text',
          targetUser: ctx.targetUser || '',
          messages: safeMessages,
          snapshotHtml: this._sanitizeSnapshotHtml(ctx.snapshotHtml || ''),
          unreadCount: Number(ctx.unreadCount || 0),
          updatedAt: ctx.updatedAt || Date.now()
        };
      });
      window.localStorage.setItem(this.storageKey, JSON.stringify(compact));
    } catch (_) {}
  }

  getContext(meta) {
    const key = RoomContextManager.buildContextKey(meta);
    return this.contexts[key] || null;
  }

  getSnapshot(keyOrMeta) {
    const key = typeof keyOrMeta === 'string' ? keyOrMeta : RoomContextManager.buildContextKey(keyOrMeta);
    const ctx = this.contexts[key];
    return ctx ? (ctx.snapshotHtml || '') : '';
  }

  saveSnapshot(keyOrMeta, html) {
    const key = typeof keyOrMeta === 'string' ? keyOrMeta : RoomContextManager.buildContextKey(keyOrMeta);
    const ctx = this.contexts[key] || this.ensureContext(
      typeof keyOrMeta === 'string' ? { type: 'channel', serverId: 'home', channelName: 'geral' } : keyOrMeta
    );
    ctx.snapshotHtml = this._sanitizeSnapshotHtml(html);
    ctx.updatedAt = Date.now();
    this._saveToStorage();
  }

  ensureContext(meta = {}) {
    const key = RoomContextManager.buildContextKey(meta);
    if (!this.contexts[key]) {
      this.contexts[key] = {
        key,
        type: meta.type || 'channel',
        serverId: meta.serverId || 'home',
        channelName: meta.channelName || 'geral',
        channelType: meta.channelType || 'text',
        targetUser: meta.targetUser || '',
        messages: [],
        snapshotHtml: '',
        unreadCount: 0,
        updatedAt: Date.now()
      };
      this._saveToStorage();
    } else {
      if (meta.channelType) this.contexts[key].channelType = meta.channelType;
      if (meta.targetUser) this.contexts[key].targetUser = meta.targetUser;
    }
    return this.contexts[key];
  }

  saveCurrentContext() {
    if (!this.activeContextKey || !this.contexts[this.activeContextKey]) return;
    const ctx = this.contexts[this.activeContextKey];
    if (this.chatLogEl) {
      ctx.snapshotHtml = this._sanitizeSnapshotHtml(this.chatLogEl.innerHTML);
    }
    ctx.updatedAt = Date.now();
    this._saveToStorage();
  }

  switchContext(meta, { renderFn = null, connectSocket = true } = {}) {
    if (this.activeContextKey && this.activeContextKey !== RoomContextManager.buildContextKey(meta)) {
      this.saveCurrentContext();
    }

    const ctx = this.ensureContext(meta);
    const previousUnread = Number(ctx.unreadCount || 0);
    ctx.unreadCount = 0;
    this.activeContextKey = ctx.key;
    this._saveToStorage();

    if (previousUnread > 0 && typeof this.onUnreadChange === 'function') {
      this.onUnreadChange(ctx, 0);
    }

    if (this.chatLogEl) {
      if (typeof renderFn === 'function') {
        this.chatLogEl.innerHTML = '';
        renderFn(ctx, this.chatLogEl);
      } else if (ctx.snapshotHtml) {
        this.chatLogEl.innerHTML = ctx.snapshotHtml;
      } else {
        this.chatLogEl.innerHTML = '';
      }
      this.chatLogEl.scrollTop = this.chatLogEl.scrollHeight;
    }

    if (connectSocket) {
      this._connectContextSocket(ctx);
    }

    if (typeof this.onContextChange === 'function') {
      this.onContextChange(ctx);
    }

    return ctx;
  }

  _isDuplicateMessage(ctx, messagePayload, entryId) {
    const incomingAuthor = String(messagePayload.author || messagePayload.sender || messagePayload.username || '').trim().toLowerCase();
    const incomingText = String(messagePayload.text || messagePayload.message || '').trim();
    const incomingAttach = String(messagePayload.attachmentName || messagePayload.attachment_name || messagePayload.attachmentUrl || messagePayload.attachment_url || '').trim();

    return ctx.messages.some((m) => {
      if (!m) return false;
      if (entryId && m.id && String(m.id) === String(entryId)) return true;
      if (messagePayload.client_id && m.raw && m.raw.client_id && String(m.raw.client_id) === String(messagePayload.client_id)) {
        return true;
      }
      const sameAuthor = String(m.author || '').trim().toLowerCase() === incomingAuthor;
      const sameText = String(m.text || '').trim() === incomingText;
      const sameAttach = String(m.attachmentName || m.attachmentUrl || '').trim() === incomingAttach;
      return sameAuthor && sameText && sameAttach && (incomingText || incomingAttach);
    });
  }

  pushMessage(meta, messagePayload = {}, { renderSingleMessageFn = null } = {}) {
    const ctx = this.ensureContext(meta);
    const entryId = messagePayload.id || messagePayload.client_id || `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;

    if (this._isDuplicateMessage(ctx, messagePayload, entryId)) {
      const existing = ctx.messages.find((m) => m && String(m.id) === String(entryId)) || null;
      return { entry: existing, ctx, isActive: this.activeContextKey === ctx.key, duplicate: true };
    }

    const entry = {
      id: entryId,
      author: messagePayload.author || messagePayload.sender || 'Usuário',
      text: messagePayload.text || messagePayload.message || '',
      timestamp: messagePayload.timestamp || new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
      avatarUrl: messagePayload.avatarUrl || '',
      attachmentUrl: messagePayload.attachmentUrl || '',
      attachmentName: messagePayload.attachmentName || '',
      attachmentType: messagePayload.attachmentType || '',
      canDelete: Boolean(messagePayload.canDelete),
      raw: messagePayload
    };

    ctx.messages.push(entry);
    if (ctx.messages.length > this.maxMessagesPerContext) {
      ctx.messages = ctx.messages.slice(-this.maxMessagesPerContext);
    }
    ctx.updatedAt = Date.now();

    const isActive = this.activeContextKey === ctx.key;
    if (!isActive) {
      ctx.unreadCount = Number(ctx.unreadCount || 0) + 1;
      if (typeof this.onUnreadChange === 'function') {
        this.onUnreadChange(ctx, ctx.unreadCount);
      }
    } else if (this.chatLogEl && typeof renderSingleMessageFn === 'function') {
      renderSingleMessageFn(entry, ctx, this.chatLogEl);
      this.chatLogEl.scrollTop = this.chatLogEl.scrollHeight;
    }

    if (isActive && this.chatLogEl) {
      ctx.snapshotHtml = this._sanitizeSnapshotHtml(this.chatLogEl.innerHTML);
    }

    this._saveToStorage();
    return { entry, ctx, isActive, duplicate: false };
  }

  removeMessage(meta, messageId) {
    const key = RoomContextManager.buildContextKey(meta);
    const ctx = this.contexts[key];
    if (!ctx || !Array.isArray(ctx.messages)) return false;
    const beforeLen = ctx.messages.length;
    ctx.messages = ctx.messages.filter((m) => String(m.id) !== String(messageId));
    if (this.activeContextKey === key && this.chatLogEl) {
      ctx.snapshotHtml = this._sanitizeSnapshotHtml(this.chatLogEl.innerHTML);
    }
    this._saveToStorage();
    return ctx.messages.length !== beforeLen;
  }

  clearContext(meta) {
    const key = RoomContextManager.buildContextKey(meta);
    if (!this.contexts[key]) return;
    this.contexts[key].messages = [];
    this.contexts[key].snapshotHtml = '';
    this.contexts[key].unreadCount = 0;
    if (this.activeContextKey === key && this.chatLogEl) {
      this.chatLogEl.innerHTML = '';
    }
    this._saveToStorage();
    if (typeof this.onUnreadChange === 'function') {
      this.onUnreadChange(this.contexts[key], 0);
    }
  }

  sendPayload(payload, meta = null) {
    const targetKey = meta ? RoomContextManager.buildContextKey(meta) : this.activeContextKey;
    if (!targetKey) return false;
    const entry = this.sockets.get(targetKey);
    const rawSocket = entry && entry.socket ? entry.socket : entry;
    if (!rawSocket || rawSocket.readyState !== WebSocket.OPEN) return false;
    try {
      rawSocket.send(typeof payload === 'string' ? payload : JSON.stringify(payload));
      return true;
    } catch (_) {
      return false;
    }
  }

  _clearReconnectTimer(key) {
    const timer = this.reconnectTimers.get(key);
    if (timer) {
      clearTimeout(timer);
      this.reconnectTimers.delete(key);
    }
  }

  _scheduleReconnect(ctx) {
    if (!ctx || this.activeContextKey !== ctx.key) return;
    if (this.manualDisconnects.has(ctx.key)) return;

    const attempts = Number(this.reconnectAttempts.get(ctx.key) || 0);
    if (attempts >= this.maxReconnectAttempts) {
      if (typeof this.onConnectionStateChange === 'function') {
        this.onConnectionStateChange('failed', ctx);
      }
      return;
    }

    this._clearReconnectTimer(ctx.key);
    const delayMs = Math.min(10000, 600 * Math.pow(1.6, attempts));
    this.reconnectAttempts.set(ctx.key, attempts + 1);

    if (typeof this.onConnectionStateChange === 'function') {
      this.onConnectionStateChange('reconnecting', ctx);
    }

    const timer = setTimeout(() => {
      this.reconnectTimers.delete(ctx.key);
      if (this.activeContextKey === ctx.key && !this.manualDisconnects.has(ctx.key)) {
        this._connectContextSocket(ctx, { isReconnect: true });
      }
    }, delayMs);

    this.reconnectTimers.set(ctx.key, timer);
  }

  _closeSocketByKey(key, { manual = true } = {}) {
    if (!key) return;
    if (manual) {
      this.manualDisconnects.add(key);
    }
    this._clearReconnectTimer(key);
    const entry = this.sockets.get(key);
    const sock = entry && entry.socket ? entry.socket : entry;
    if (sock) {
      sock.onopen = null;
      sock.onmessage = null;
      sock.onerror = null;
      sock.onclose = null;
      if (entry && typeof entry.close === 'function') {
        try { entry.close(); } catch (_) {}
      } else if (typeof sock.close === 'function') {
        try { sock.close(); } catch (_) {}
      }
      this.sockets.delete(key);
    }
  }

  _handleIncomingData(ctx, rawData) {
    let data = null;
    try {
      data = typeof rawData === 'string' ? JSON.parse(rawData) : rawData;
    } catch (_) {
      data = { text: String(rawData || '') };
    }

    if (!data || typeof data !== 'object') return;

    const incomingType = data.type === 'dm' || data.type === 'dm_message' || ctx.type === 'dm' ? 'dm' : 'channel';
    const incomingMeta = {
      type: incomingType,
      serverId: data.server_id || data.serverId || ctx.serverId,
      channelName: data.channel || data.channel_name || data.channelName || ctx.channelName,
      targetUser: data.target_user || data.targetUser || data.sender || ctx.targetUser
    };

    const targetKey = RoomContextManager.buildContextKey(incomingMeta);
    const effectiveMeta = this.contexts[targetKey]
      ? incomingMeta
      : {
          type: ctx.type,
          serverId: ctx.serverId,
          channelName: ctx.channelName,
          targetUser: ctx.targetUser
        };

    const authorName = data.author || data.sender || data.username || 'Usuário';
    const textVal = data.text || data.message || '';
    const attUrl = data.attachmentUrl || data.attachment_url || '';
    const attName = data.attachmentName || data.attachment_name || '';
    const attType = data.attachmentType || data.attachment_type || '';

    if (!textVal && !attUrl && !data.id) {
      if (typeof this.onIncomingMessage === 'function') {
        this.onIncomingMessage(data, { entry: null, ctx: this.ensureContext(effectiveMeta), isActive: this.activeContextKey === targetKey });
      }
      return;
    }

    const result = this.pushMessage(effectiveMeta, {
      id: data.id,
      client_id: data.client_id,
      author: authorName,
      text: textVal,
      timestamp: data.timestamp || new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
      avatarUrl: data.avatarUrl || data.avatar_url || '',
      attachmentUrl: attUrl,
      attachmentName: attName,
      attachmentType: attType,
      canDelete: Boolean(data.canDelete),
      raw: data
    });

    if (typeof this.onIncomingMessage === 'function') {
      this.onIncomingMessage(data, result);
    }
  }

  _buildFallbackSocket(ctx) {
    if (typeof this.client?.buildWsUrl !== 'function') return null;
    if (ctx.type === 'dm') {
      return new WebSocket(this.client.buildWsUrl(`/ws/dm/${encodeURIComponent(ctx.targetUser)}/`));
    }
    const params = new URLSearchParams({ server: String(ctx.serverId || 'home'), channel: String(ctx.channelName || 'geral') });
    return new WebSocket(this.client.buildWsUrl(`/ws/chat/${encodeURIComponent(ctx.channelName || 'geral')}/?${params.toString()}`));
  }

  _connectContextSocket(ctx, { isReconnect = false } = {}) {
    if (!ctx) return;

    if (this.activeSocketKey && this.activeSocketKey !== ctx.key) {
      this._closeSocketByKey(this.activeSocketKey, { manual: true });
    }

    this.manualDisconnects.delete(ctx.key);
    this.activeSocketKey = ctx.key;

    const existing = this.sockets.get(ctx.key);
    const existingWs = existing && existing.socket ? existing.socket : existing;
    if (existingWs && (existingWs.readyState === WebSocket.OPEN || existingWs.readyState === WebSocket.CONNECTING) && !isReconnect) {
      return;
    }

    this._closeSocketByKey(ctx.key, { manual: false });

    let wrapperOrSocket = null;
    try {
      if (ctx.type === 'dm' && typeof this.client?.createDmSocket === 'function') {
        wrapperOrSocket = this.client.createDmSocket({
          username: ctx.targetUser,
          onEvent: (ev) => this._handleIncomingData(ctx, ev)
        });
      } else if (typeof this.client?.createChatSocket === 'function') {
        wrapperOrSocket = this.client.createChatSocket({
          roomSlug: ctx.channelName,
          server: ctx.serverId,
          channel: ctx.channelName,
          onEvent: (ev) => this._handleIncomingData(ctx, ev)
        });
      } else {
        wrapperOrSocket = this._buildFallbackSocket(ctx);
      }
    } catch (_) {
      wrapperOrSocket = this._buildFallbackSocket(ctx);
    }

    const rawSocket = wrapperOrSocket && wrapperOrSocket.socket ? wrapperOrSocket.socket : wrapperOrSocket;
    if (!rawSocket) {
      this._scheduleReconnect(ctx);
      return;
    }

    this.sockets.set(ctx.key, wrapperOrSocket);

    if (typeof this.onConnectionStateChange === 'function') {
      this.onConnectionStateChange(isReconnect ? 'reconnecting' : 'connecting', ctx);
    }

    rawSocket.onopen = () => {
      if (this.activeContextKey !== ctx.key) return;
      this.reconnectAttempts.set(ctx.key, 0);
      if (typeof this.onConnectionStateChange === 'function') {
        this.onConnectionStateChange('connected', ctx);
      }
    };

    rawSocket.onmessage = (event) => {
      this._handleIncomingData(ctx, event.data);
    };

    rawSocket.onerror = () => {
      if (this.activeContextKey !== ctx.key) return;
      if (typeof this.onConnectionStateChange === 'function') {
        this.onConnectionStateChange('error', ctx);
      }
    };

    rawSocket.onclose = () => {
      this.sockets.delete(ctx.key);
      if (this.activeContextKey === ctx.key && !this.manualDisconnects.has(ctx.key)) {
        this._scheduleReconnect(ctx);
      }
    };
  }

  destroy() {
    this.reconnectTimers.forEach((timer) => clearTimeout(timer));
    this.reconnectTimers.clear();
    Array.from(this.sockets.keys()).forEach((key) => this._closeSocketByKey(key, { manual: true }));
  }
}

window.RoomContextManager = RoomContextManager;