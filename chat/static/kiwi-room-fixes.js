/* Patch Kiwi Chat: Channels (#geral / música), Banner Save + Quick Save Button, Real Accounts/Profiles Store & Explore Search, Live Simultaneous Início Feed */
(function applyKiwiRoomFixes() {
    const PROFILES_DB_KEY = 'kiwi_profiles_db_v1';
    const USER_POSTS_KEY = 'kiwi_profile_posts_v1';
    const SAVED_BANNERS_KEY = 'kiwi_saved_banners_v1';

    function safeJsonParse(raw, fallback) {
        if (!raw) return fallback;
        try { return JSON.parse(raw); } catch (_) { return fallback; }
    }

    function readDb(key, fallback) {
        try {
            return safeJsonParse(localStorage.getItem(key), fallback);
        } catch (_) {
            return fallback;
        }
    }

    function writeDb(key, val) {
        try {
            localStorage.setItem(key, JSON.stringify(val));
        } catch (_) {}
    }

    // 1) Registrar perfis reais no banco local + sincronizar com backend (/api/users/search/ e /api/profile/<user>/)
    function registerRealProfile(profileObj) {
        if (!profileObj || typeof profileObj !== 'object') return;
        const uname = String(profileObj.username || '').trim();
        if (!uname) return;
        const key = uname.toLowerCase();
        const db = readDb(PROFILES_DB_KEY, {});
        const prev = db[key] || {};
        db[key] = Object.assign({}, prev, profileObj, {
            username: uname,
            handle: profileObj.handle || prev.handle || `@${key}`,
            updatedAt: Date.now()
        });
        writeDb(PROFILES_DB_KEY, db);
        if (typeof profilesByUsername !== 'undefined' && profilesByUsername && typeof profilesByUsername.set === 'function') {
            profilesByUsername.set(key, db[key]);
        }
    }

    // Limpa contas fictícias/seedadas (Hollow29, Maomao, LoFi_Station) do explore e usa apenas perfis reais salvos/buscados
    if (typeof exploreAccountsList !== 'undefined') {
        const filtered = (Array.isArray(exploreAccountsList) ? exploreAccountsList : []).filter(acc => {
            const u = String((acc && acc.username) || '').trim().toLowerCase();
            return u && u !== 'hollow29' && u !== 'maomao' && u !== 'lofi_station';
        });
        exploreAccountsList.length = 0;
        filtered.forEach(a => exploreAccountsList.push(a));
        writeDb('kiwi_explore_accounts_v1', exploreAccountsList);
    }

    // Registra o usuário atual no banco de perfis reais assim que disponível
    function syncCurrentUserIntoRealDb() {
        const currName = (typeof activeUsername !== 'undefined' && activeUsername)
            ? String(activeUsername).trim()
            : ((document.getElementById('my-name-label') && document.getElementById('my-name-label').textContent.trim()) || '');
        if (!currName) return;
        const prof = (typeof myProfileData !== 'undefined' && myProfileData) ? myProfileData : {};
        const posts = readDb(USER_POSTS_KEY, []);
        registerRealProfile({
            username: currName,
            handle: `@${currName.toLowerCase()}`,
            bubble: prof.bubble || '',
            pronouns: prof.pronouns || '',
            bio: prof.bio || '',
            location: prof.location || '',
            joinedAt: prof.joinedAt || '',
            avatarUrl: prof.avatarUrl || '',
            bannerUrl: prof.bannerUrl || '',
            song: prof.song || '',
            artist: prof.artist || '',
            musicUrl: prof.musicUrl || '',
            audioSrc: prof.audioSrc || '',
            coverUrl: prof.coverUrl || '',
            posts: Array.isArray(posts) ? posts : []
        });
    }

    syncCurrentUserIntoRealDb();

    // 2) Salvar Banner de forma persistente + botão rápido "Salvar Banner"
    function persistBannerEverywhere(bannerUrl) {
        const cleanUrl = String(bannerUrl || '').trim();
        if (!cleanUrl) return;
        if (typeof myProfileData !== 'undefined' && myProfileData) {
            myProfileData.bannerUrl = cleanUrl;
            if (typeof saveUserMusicToLocalCache === 'function' && typeof activeUsername !== 'undefined') {
                saveUserMusicToLocalCache(activeUsername, myProfileData);
            }
        }
        const savedBanners = readDb(SAVED_BANNERS_KEY, []);
        if (!savedBanners.includes(cleanUrl)) {
            savedBanners.unshift(cleanUrl);
            writeDb(SAVED_BANNERS_KEY, savedBanners.slice(0, 15));
        }
        syncCurrentUserIntoRealDb();
        if (typeof syncXProfileHeader === 'function') syncXProfileHeader();
        if (typeof refreshEditorLivePreview === 'function') refreshEditorLivePreview();
        if (typeof showKiwiToast === 'function') showKiwiToast('Banner salvo!');
    }

    function injectSaveBannerControls() {
        // Botão na aba/card de edição do banner
        const bannerFileInput = document.getElementById('edit-banner-file');
        const bannerThumb = document.getElementById('editor-live-banner-thumb');
        if (bannerFileInput && !bannerFileInput._kiwiBannerHooked) {
            bannerFileInput._kiwiBannerHooked = true;
            bannerFileInput.addEventListener('change', () => {
                const f = bannerFileInput.files && bannerFileInput.files[0];
                if (!f) return;
                const reader = new FileReader();
                reader.onload = () => {
                    if (typeof reader.result === 'string' && reader.result) {
                        persistBannerEverywhere(reader.result);
                    }
                };
                reader.readAsDataURL(f);
            });
        }

        // Botão rápido "+ Salvar Banner" na topbar do perfil
        const topbarRight = document.querySelector('#kiwi-profile-timeline-view .kiwi-x-topbar > div:last-child');
        if (topbarRight && !document.getElementById('kiwi-quick-save-banner-btn')) {
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.id = 'kiwi-quick-save-banner-btn';
            btn.className = 'section-mini-action-btn';
            btn.title = 'Salvar banner atual';
            btn.textContent = '💾 Salvar Banner';
            btn.addEventListener('click', () => {
                const currentBanner = (typeof myProfileData !== 'undefined' && myProfileData && myProfileData.bannerUrl)
                    || (bannerThumb && bannerThumb.style.backgroundImage ? bannerThumb.style.backgroundImage.replace(/^url\(["']?|["']?\)$/g, '') : '');
                if (currentBanner) {
                    persistBannerEverywhere(currentBanner);
                } else if (bannerFileInput) {
                    bannerFileInput.click();
                }
            });
            topbarRight.insertBefore(btn, topbarRight.firstChild);
        }
    }

    injectSaveBannerControls();

    // 3) Garantir envio de mensagens nos canais (#geral, música e demais canais) sem travar se WS não estiver aberto
    const origSendChat = (typeof sendChatMessage === 'function') ? sendChatMessage : null;
    if (origSendChat) {
        window.sendChatMessage = async function patchedSendChatMessage() {
            const msgInput = document.getElementById('chat-message-input');
            const txt = msgInput ? msgInput.value.trim() : '';
            if (!txt && !window.__kiwiPendingAttachment) return;

            const chName = (typeof currentChannelName !== 'undefined' && currentChannelName) ? currentChannelName : 'geral';
            const srvId = (typeof currentServerId !== 'undefined' && currentServerId) ? currentServerId : 'home';
            const senderName = (typeof activeUsername !== 'undefined' && activeUsername)
                ? activeUsername
                : ((document.getElementById('my-name-label') && document.getElementById('my-name-label').textContent.trim()) || 'Usuário');

            // Garante que o chat-log esteja visível ao enviar mensagem em canal
            const logEl = document.getElementById('chat-log');
            if (logEl && logEl.style.display === 'none') {
                logEl.style.display = 'flex';
            }

            await origSendChat();

            // Fallback extra caso o input ainda tenha texto por algum bloqueio anterior
            const leftover = msgInput ? msgInput.value.trim() : '';
            if (leftover && typeof appendMessage === 'function') {
                const nowTime = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
                appendMessage({
                    id: 'ch-' + Date.now() + '-' + Math.random().toString(36).slice(2, 6),
                    sender: senderName,
                    message: leftover,
                    timestamp: nowTime,
                    canDelete: true
                });
                if (window.KiwiContextManager) {
                    window.KiwiContextManager.appendMessageToContext(
                        { type: 'channel', serverId: srvId, channelName: chName },
                        { author: senderName, text: leftover, timestamp: nowTime }
                    );
                }
                msgInput.value = '';
            }
        };

        const submitBtn = document.getElementById('chat-message-submit');
        const msgInputEl = document.getElementById('chat-message-input');
        if (submitBtn) submitBtn.onclick = () => window.sendChatMessage();
        if (msgInputEl) {
            msgInputEl.onkeyup = (e) => {
                if (e.key === 'Enter') window.sendChatMessage();
            };
        }
    }

    // 4) Buscar perfis reais no banco (local + /api/users/search/ + /api/profile/<username>/) no Explorar
    async function searchRealProfilesFromBackend(query) {
        const q = String(query || '').trim();
        const db = readDb(PROFILES_DB_KEY, {});
        const localMatches = Object.values(db).filter(p => {
            if (!p || !p.username) return false;
            if (!q) return true;
            const ql = q.toLowerCase();
            return String(p.username).toLowerCase().includes(ql) ||
                   String(p.handle || '').toLowerCase().includes(ql) ||
                   String(p.bio || '').toLowerCase().includes(ql);
        });

        if (!q || window.location.protocol === 'file:') return localMatches;

        try {
            const res = await fetch(`/api/users/search/?q=${encodeURIComponent(q)}`, { credentials: 'same-origin' });
            if (res.ok) {
                const data = await res.json().catch(() => ({}));
                const users = Array.isArray(data.users) ? data.users : (Array.isArray(data) ? data : []);
                users.forEach(u => {
                    const uname = typeof u === 'string' ? u : (u && u.username);
                    if (uname) {
                        registerRealProfile(typeof u === 'object' ? u : { username: uname });
                    }
                });
            }
        } catch (_) {}

        try {
            const profRes = await fetch(`/api/profile/${encodeURIComponent(q)}/`, { credentials: 'same-origin' });
            if (profRes.ok) {
                const pData = await profRes.json().catch(() => null);
                const norm = (pData && pData.profile) ? pData.profile : pData;
                if (norm && norm.username) {
                    registerRealProfile(norm);
                }
            }
        } catch (_) {}

        const updatedDb = readDb(PROFILES_DB_KEY, {});
        const ql = q.toLowerCase();
        return Object.values(updatedDb).filter(p => {
            if (!p || !p.username) return false;
            return String(p.username).toLowerCase().includes(ql) ||
                   String(p.handle || '').toLowerCase().includes(ql) ||
                   String(p.bio || '').toLowerCase().includes(ql);
        });
    }

    window.KiwiSearchRealProfiles = searchRealProfilesFromBackend;

    // 5) Sincronização simultânea do feed "Início" com novas postagens (sem recriar feed do zero quando já montado)
    function syncInicioFeedSimultaneously() {
        const logEl = document.getElementById('chat-log');
        if (!logEl || typeof currentView === 'undefined' || currentView !== 'inicio') return;
        syncCurrentUserIntoRealDb();
        if (typeof renderSimpleNavFeed === 'function') {
            renderSimpleNavFeed('inicio');
        }
    }

    window.addEventListener('storage', (ev) => {
        if (ev.key === USER_POSTS_KEY || ev.key === PROFILES_DB_KEY || ev.key === 'kiwi_explore_accounts_v1') {
            syncInicioFeedSimultaneously();
        }
    });

    const postForm = document.getElementById('create-post-form');
    if (postForm && !postForm._kiwiSyncHooked) {
        postForm._kiwiSyncHooked = true;
        postForm.addEventListener('submit', () => {
            setTimeout(() => {
                syncCurrentUserIntoRealDb();
                syncInicioFeedSimultaneously();
            }, 60);
        });
    }
})();