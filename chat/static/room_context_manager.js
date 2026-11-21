/**
 * Gerenciador de estado isolado por contexto (Servidor + Canal / DM) e roteamento de WebSocket.
 * Projetado para evitar vazamento de histórico, cabeçalhos e eventos entre salas e DMs.
 */
class RoomContextManager {
  constructor(options = {}) {
    this.storageKey = options.storageKey || 'kiwi_room_contexts_v1';
    this.client = options.client || window.KiwiClient || null;
    this.chatLogEl = options.chatLogEl || document.getElementById('chatLog');
    this.onContextChange = options.onContextChange || null;
    this.onIncomingMessage = options.onIncomingMessage || null;

    this.activeContextKey = null;
    this.contexts = this._loadFromStorage();
    this.sockets = new Map();
    this.activeSocketKey = null;
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
      window.localStorage.setItem(this.storageKey, JSON.stringify(this.contexts));
    } catch (_) {
      // Ignora falhas de quota/privacidade do navegador
    }
  }

  /**
   * Garante que o registro do contexto exista e retorna o objeto.
   */
  ensureContext(meta) {
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
      ctx.snapshotHtml = this.chatLogEl.innerHTML;
    }
    ctx.updatedAt = Date.now();
    this._saveToStorage();
  }

  /**
   * Alterna para um novo contexto (servidor/canal ou DM), restaura o histórico isolado
   * e ajusta o socket ativo sem misturar mensagens de outras salas.
   */
  switchContext(meta, { renderFn = null, connectSocket = true } = {}) {
    if (this.activeContextKey) {
      this.saveCurrentContext();
    }

    const ctx = this.ensureContext(meta);
    ctx.unreadCount = 0;
    this.activeContextKey = ctx.key;
    this._saveToStorage();

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
  pushMessage(meta, messagePayload, { renderSingleMessageFn = null } = {}) {
    const ctx = this.ensureContext(meta);
    const entry = {
      id: messagePayload.id || `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      author: messagePayload.author || 'Usuário',
      text: messagePayload.text || '',
      timestamp: messagePayload.timestamp || new Date().toISOString(),
      raw: messagePayload
    };

    ctx.messages.push(entry);
    ctx.updatedAt = Date.now();

    const isActive = this.activeContextKey === ctx.key;
    if (!isActive) {
      ctx.unreadCount = (ctx.unreadCount || 0) + 1;
    } else if (this.chatLogEl && typeof renderSingleMessageFn === 'function') {
      renderSingleMessageFn(entry, ctx, this.chatLogEl);
      this.chatLogEl.scrollTop = this.chatLogEl.scrollHeight;
    }

    if (isActive && this.chatLogEl) {
      ctx.snapshotHtml = this.chatLogEl.innerHTML;
    }

    this._saveToStorage();
    return { entry, ctx, isActive };
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
  }

  /**
   * Conecta ou reutiliza o WebSocket correspondente ao contexto e filtra eventos
   * para que mensagens de outras salas não invadam a tela atual.
   */
  _connectContextSocket(ctx) {
    if (!this.client) return;

    // Fecha socket anterior se for diferente do contexto atual
    if (this.activeSocketKey && this.activeSocketKey !== ctx.key) {
      const prevSocket = this.sockets.get(this.activeSocketKey);
      if (prevSocket && typeof prevSocket.close === 'function') {
        try {
          prevSocket.close();
        } catch (_) {}
      }
      this.sockets.delete(this.activeSocketKey);
    }

    this.activeSocketKey = ctx.key;

    let socket = null;
    if (ctx.type === 'dm' && typeof this.client.createDmSocket === 'function') {
      socket = this.client.createDmSocket(ctx.targetUser);
    } else if (typeof this.client.createChatSocket === 'function') {
      socket = this.client.createChatSocket(ctx.channelName, ctx.serverId);
    }

    if (!socket) return;
    this.sockets.set(ctx.key, socket);

    socket.onmessage = (event) => {
      let data = null;
      try {
        data = typeof event.data === 'string' ? JSON.parse(event.data) : event.data;
      } catch (_) {
        data = { text: String(event.data || '') };
      }

      const incomingMeta = {
        type: ctx.type,
        serverId: data.server_id || data.serverId || ctx.serverId,
        channelName: data.channel || data.channel_name || data.channelName || ctx.channelName,
        targetUser: data.target_user || data.targetUser || data.sender || ctx.targetUser
      };

      const targetKey = RoomContextManager.buildContextKey(incomingMeta);
      const targetCtx = this.contexts[targetKey] ? incomingMeta : {
        type: ctx.type,
        serverId: ctx.serverId,
        channelName: ctx.channelName,
        targetUser: ctx.targetUser
      };

      const result = this.pushMessage(targetCtx, {
        id: data.id,
        author: data.author || data.sender || data.username || 'Usuário',
        text: data.text || data.message || '',
        timestamp: data.timestamp || new Date().toISOString(),
        raw: data
      });

      if (typeof this.onIncomingMessage === 'function') {
        this.onIncomingMessage(data, result);
      }
    };
  }
}

window.RoomContextManager = RoomContextManager;