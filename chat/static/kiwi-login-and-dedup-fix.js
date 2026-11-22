/* 1) Resolver o nome da conta a partir do login/sessão (sem forçar AkiiraPW em outras contas) */
(function resolveLoginAccountName() {
    const LOGIN_NAME_KEYS = [
        'kiwi_login_username',
        'kiwi_logged_in_user',
        'kiwi_user_identity_v2',
        'kiwi_username'
    ];

    function readJsonTag(id) {
        const el = document.getElementById(id);
        if (!el) return null;
        try {
            return JSON.parse(el.textContent);
        } catch (_) {
            return null;
        }
    }

    function getLoginUsername() {
        // 1. Dados injetados pelo Django no template (#kiwi-username-data / #kiwi-profile-data)
        const injectedUser = readJsonTag('kiwi-username-data');
        if (typeof injectedUser === 'string' && injectedUser.trim()) {
            return injectedUser.trim();
        }
        const injectedProfile = readJsonTag('kiwi-profile-data');
        if (injectedProfile && typeof injectedProfile === 'object') {
            const pUser = injectedProfile.username || (injectedProfile.profile && injectedProfile.profile.username);
            if (typeof pUser === 'string' && pUser.trim()) {
                return pUser.trim();
            }
        }

        // 2. Campo de usuário de login na página (se houver)
        const loginInput = document.querySelector(
            'input[name="username"], input[name="login_username"], #login-username, #id_username'
        );
        if (loginInput && typeof loginInput.value === 'string' && loginInput.value.trim()) {
            return loginInput.value.trim();
        }

        // 3. Storage salvo no login
        for (const key of LOGIN_NAME_KEYS) {
            try {
                const raw = localStorage.getItem(key) || sessionStorage.getItem(key) || '';
                if (!raw.trim()) continue;
                try {
                    const parsed = JSON.parse(raw);
                    if (typeof parsed === 'string' && parsed.trim()) return parsed.trim();
                    if (parsed && typeof parsed.username === 'string' && parsed.username.trim()) {
                        return parsed.username.trim();
                    }
                } catch (_) {
                    return raw.trim();
                }
            } catch (_) {}
        }

        return '';
    }

    const accountUsername = getLoginUsername();
    if (accountUsername) {
        try {
            localStorage.setItem('kiwi_user_identity_v2', accountUsername);
        } catch (_) {}

        if (typeof activeUsername !== 'undefined') {
            activeUsername = accountUsername;
        }
        if (typeof myProfileData !== 'undefined' && myProfileData) {
            myProfileData.username = accountUsername;
            // Só aplica traços exclusivos da AkiiraPW se a conta for de fato AkiiraPW
            if (accountUsername.toLowerCase() !== 'akiirapw') {
                myProfileData.goldGlowFrame = false;
                myProfileData.profileGlow = false;
                myProfileData.goldProfileBg = false;
            }
        }

        const myNameLabel = document.getElementById('my-name-label');
        if (myNameLabel) myNameLabel.textContent = accountUsername;

        const topName = document.getElementById('kiwi-x-top-name');
        if (topName) topName.textContent = accountUsername;

        const handleEl = document.getElementById('kiwi-x-handle');
        if (handleEl) handleEl.textContent = '@' + accountUsername.toLowerCase();

        if (typeof updateMySidebarUI === 'function') updateMySidebarUI();
        if (typeof syncXProfileHeader === 'function') syncXProfileHeader();
    }
})();

/* 2) Deduplicação sem depender do relógio/timestamp do computador */
(function patchMessageDeduplicationWithoutClock() {
    if (typeof window.KiwiContextManager === 'undefined' || !window.KiwiContextManager) return;

    const ctxMgr = window.KiwiContextManager;
    const seenIds = new Set();

    function buildMessageFingerprint(entry) {
        if (!entry || typeof entry !== 'object') return '';
        if (entry.id) return 'id:' + String(entry.id).trim();
        const author = String(entry.author || entry.sender || entry.username || '').trim().toLowerCase();
        const text = String(entry.text || entry.message || '').trim();
        const attach = String(entry.attachmentName || entry.attachment_name || entry.attachmentUrl || entry.attachment_url || '').trim();
        return `msg:${author}|${text}|${attach}`;
    }

    const origAppend = ctxMgr.appendMessageToContext.bind(ctxMgr);
    ctxMgr.appendMessageToContext = function(meta, entry, opts) {
        const fp = buildMessageFingerprint(entry);
        const ctx = typeof meta === 'object' && meta && meta.key ? meta : ctxMgr.ensureContext(meta);

        const alreadyInCtx = ctx && Array.isArray(ctx.messages) && ctx.messages.some((m) => {
            if (!m) return false;
            if (entry && entry.id && m.id && String(m.id) === String(entry.id)) return true;
            const sameAuthor = String(m.author || '').trim().toLowerCase() === String((entry && (entry.author || entry.sender)) || '').trim().toLowerCase();
            const sameText = String(m.text || '').trim() === String((entry && (entry.text || entry.message)) || '').trim();
            const sameAttach = String(m.attachmentName || m.attachmentUrl || '').trim() === String((entry && (entry.attachmentName || entry.attachmentUrl)) || '').trim();
            return sameAuthor && sameText && sameAttach && (sameText || sameAttach);
        });

        if (alreadyInCtx || (fp && fp.startsWith('id:') && seenIds.has(fp))) {
            return entry;
        }
        if (fp) seenIds.add(fp);
        return origAppend(meta, entry, opts);
    };
})();