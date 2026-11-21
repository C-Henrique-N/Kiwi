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

  /**
   * Gera uma chave única para canal de servidor ou DM.
   * Exemplos:
   * - Servidor + canal: "srv:home:ch:geral"
   * - DM: "dm:hollow"
   */
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
    // Evita persistir data URIs gigantes no localStorage
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
    } catch (_) {
      // Ignora falhas de quota/privacidade do navegador
    }
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

  /**
   * Garante que o registro do contexto exista e retorna o objeto.
   */
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

  /**
   * Salva o estado atual do DOM/mensagens antes de sair do contexto ativo.
   */
  saveCurrentContext() {
    if (!this.activeContextKey || !this.contexts[this.activeContextKey]) return;
    const ctx = this.contexts[this.activeContextKey];
    if (this.chatLogEl) {
      ctx.snapshotHtml = this._sanitizeSnapshotHtml(this.chatLogEl.innerHTML);
    }
    ctx.updatedAt = Date.now();
    this._saveToStorage();
  }

  /**
   * Alterna para um novo contexto (servidor/canal ou DM), restaura o histórico isolado
   * e ajusta o socket ativo sem misturar mensagens de outras salas.
   */
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

  /**
   * Registra uma mensagem no contexto alvo.
   * Só renderiza diretamente no chatLog se o contexto da mensagem for o contexto ativo.
   */
  pushMessage(meta, messagePayload = {}, { renderSingleMessageFn = null } = {}) {
    const ctx = this.ensureContext(meta);
    const entryId = messagePayload.id || `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;

    const duplicate = ctx.messages.some((m) => String(m.id) === String(entryId));
    if (duplicate) {
      return { entry: ctx.messages.find((m) => String(m.id) === String(entryId)), ctx, isActive: this.activeContextKey === ctx.key, duplicate: true };
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

  /**
   * Remove uma mensagem específica de um contexto pelo ID.
   */
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

  /**
   * Limpa o histórico apenas de um contexto específico.
   */
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

  /**
   * Envia payload JSON pelo socket do contexto ativo (ou de um contexto específico).
   */
  sendPayload(payload, meta = null) {
    const targetKey = meta ? RoomContextManager.buildContextKey(meta) : this.activeContextKey;
    if (!targetKey) return false;
    const sock = this.sockets.get(targetKey);
    if (!sock || sock.readyState !== WebSocket.OPEN) return false;
    try {
      sock.send(typeof payload === 'string' ? payload : JSON.stringify(payload));
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
    const sock = this.sockets.get(key);
    if (sock) {
      sock.onopen = null;
      sock.onmessage = null;
      sock.onerror = null;
      sock.onclose = null;
      if (typeof sock.close === 'function') {
        try {
          sock.close();
        } catch (_) {}
      }
      this.sockets.delete(key);
    }
  }

  /**
   * Conecta ou reutiliza o WebSocket correspondente ao contexto e filtra eventos
   * para que mensagens de outras salas não invadam a tela atual.
   */
  _connectContextSocket(ctx, { isReconnect = false } = {}) {
    if (!this.client || !ctx) return;

    // Fecha socket anterior se for diferente do contexto atual
    if (this.activeSocketKey && this.activeSocketKey !== ctx.key) {
      this._closeSocketByKey(this.activeSocketKey, { manual: true });
    }

    this.manualDisconnects.delete(ctx.key);
    this.activeSocketKey = ctx.key;

    const existing = this.sockets.get(ctx.key);
    if (existing && (existing.readyState === WebSocket.OPEN || existing.readyState === WebSocket.CONNECTING) && !isReconnect) {
      return;
    }

    this._closeSocketByKey(ctx.key, { manual: false });

    let socket = null;
    try {
      if (ctx.type === 'dm' && typeof this.client.createDmSocket === 'function') {
        socket = this.client.createDmSocket(ctx.targetUser);
      } else if (typeof this.client.createChatSocket === 'function') {
        socket = this.client.createChatSocket(ctx.channelName, ctx.serverId);
      }
    } catch (_) {
      socket = null;
    }

    if (!socket) {
      this._scheduleReconnect(ctx);
      return;
    }

    this.sockets.set(ctx.key, socket);

    if (typeof this.onConnectionStateChange === 'function') {
      this.onConnectionStateChange(isReconnect ? 'reconnecting' : 'connecting', ctx);
    }

    socket.onopen = () => {
      if (this.activeContextKey !== ctx.key) return;
      this.reconnectAttempts.set(ctx.key, 0);
      if (typeof this.onConnectionStateChange === 'function') {
        this.onConnectionStateChange('connected', ctx);
      }
    };

    socket.onmessage = (event) => {
      let data = null;
      try {
        data = typeof event.data === 'string' ? JSON.parse(event.data) : event.data;
      } catch (_) {
        data = { text: String(event.data || '') };
      }

      if (!data || typeof data !== 'object') return;

      const incomingType = data.type === 'dm' || ctx.type === 'dm' ? 'dm' : 'channel';
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
    };

    socket.onerror = () => {
      if (this.activeContextKey !== ctx.key) return;
      if (typeof this.onConnectionStateChange === 'function') {
        this.onConnectionStateChange('error', ctx);
      }
    };

    socket.onclose = () => {
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