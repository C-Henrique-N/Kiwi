/* Patch definitivo: Botão Salvar no Editar Perfil, remoção de contas/posts falsos e busca/exibição do perfil real no Explorar & Feed */
(function kiwiCompleteFix() {
    const FAKE_USERS = new Set(['hollow29', 'maomao', 'lofi_station']);
    const FAKE_POST_IDS = new Set(['ext-hollow-1', 'ext-maomao-1', 'ext-lofi-1']);

    function isFakeAccount(acc) {
        const u = String((acc && (acc.username || acc.author)) || '').trim().toLowerCase();
        const h = String((acc && acc.handle) || '').trim().toLowerCase().replace(/^@/, '');
        return FAKE_USERS.has(u) || FAKE_USERS.has(h);
    }

    function isFakePost(post) {
        if (!post) return false;
        if (post.id && FAKE_POST_IDS.has(String(post.id))) return true;
        return isFakeAccount(post);
    }

    function cleanStorageKey(key, predicate) {
        try {
            const raw = localStorage.getItem(key);
            if (!raw) return;
            const parsed = JSON.parse(raw);
            if (Array.isArray(parsed)) {
                const cleaned = parsed.filter(item => !predicate(item));
                localStorage.setItem(key, JSON.stringify(cleaned));
            }
        } catch (_) {}
    }

    // 1) Limpar contas e postagens falsas de arrays globais e do localStorage
    cleanStorageKey('kiwi_explore_accounts_v1', isFakeAccount);
    cleanStorageKey('kiwi_profile_posts_v1', isFakePost);

    if (typeof exploreAccountsList !== 'undefined' && Array.isArray(exploreAccountsList)) {
        for (let i = exploreAccountsList.length - 1; i >= 0; i--) {
            if (isFakeAccount(exploreAccountsList[i])) {
                exploreAccountsList.splice(i, 1);
            }
        }
    }

    if (typeof userProfilePosts !== 'undefined' && Array.isArray(userProfilePosts)) {
        for (let i = userProfilePosts.length - 1; i >= 0; i--) {
            if (isFakePost(userProfilePosts[i])) {
                userProfilePosts.splice(i, 1);
            }
        }
    }

    // 2) Adicionar botão "Salvar" visível e direto no modal/formulário de Editar Perfil Estético
    function ensureSaveButtonInEditModal() {
        const form = document.getElementById('edit-profile-form');
        const modal = document.getElementById('edit-profile-modal')
            || (form && form.closest('[id*="edit-profile"], [class*="edit-profile"], .modal, .kiwi-modal'))
            || document.body;

        if (!form) return;

        // Evita duplicar se já existir
        if (document.getElementById('kiwi-direct-save-profile-btn')) return;

        const saveBtn = document.createElement('button');
        saveBtn.type = 'button';
        saveBtn.id = 'kiwi-direct-save-profile-btn';
        saveBtn.textContent = '💾 Salvar Perfil & Banner';
        saveBtn.style.cssText = [
            'display:inline-flex',
            'align-items:center',
            'justify-content:center',
            'gap:6px',
            'padding:10px 18px',
            'border-radius:10px',
            'border:none',
            'background:linear-gradient(135deg,#7c3aed,#9333ea)',
            'color:#ffffff',
            'font-weight:800',
            'font-size:13px',
            'cursor:pointer',
            'box-shadow:0 4px 14px rgba(124,58,237,0.35)'
        ].join(';');

        const triggerSave = (e) => {
            if (e) e.preventDefault();
            // Se houver banner selecionado no input file, lê antes de disparar o submit
            const bannerInput = document.getElementById('edit-banner-file');
            const finishSubmit = () => {
                if (typeof form.requestSubmit === 'function') {
                    form.requestSubmit();
                } else {
                    form.dispatchEvent(new Event('submit', { cancelable: true, bubbles: true }));
                }
                setTimeout(() => {
                    syncCurrentProfileIntoExplore();
                    if (typeof renderExploreResults === 'function') renderExploreResults();
                }, 120);
            };

            if (bannerInput && bannerInput.files && bannerInput.files[0]) {
                const reader = new FileReader();
                reader.onload = () => {
                    if (typeof reader.result === 'string' && typeof myProfileData !== 'undefined' && myProfileData) {
                        myProfileData.bannerUrl = reader.result;
                        try {
                            localStorage.setItem('kiwi_saved_banner_v1', reader.result);
                        } catch (_) {}
                    }
                    finishSubmit();
                };
                reader.readAsDataURL(bannerInput.files[0]);
            } else {
                finishSubmit();
            }
        };

        saveBtn.addEventListener('click', triggerSave);

        // Coloca um botão no topo do modal (ao lado do título/fechar) e outro no final do formulário
        const modalHeader = modal.querySelector('.modal-header, .kiwi-modal-header, [class*="modal-header"]');
        if (modalHeader && !document.getElementById('kiwi-header-save-profile-btn')) {
            const topSaveBtn = saveBtn.cloneNode(true);
            topSaveBtn.id = 'kiwi-header-save-profile-btn';
            topSaveBtn.style.padding = '7px 14px';
            topSaveBtn.style.fontSize = '12px';
            topSaveBtn.addEventListener('click', triggerSave);
            modalHeader.appendChild(topSaveBtn);
        }

        const submitInsideForm = form.querySelector('button[type="submit"], input[type="submit"]');
        if (!submitInsideForm) {
            form.appendChild(saveBtn);
        }
    }

    // 3) Garantir que o perfil real do usuário atual (e suas postagens) sempre apareçam no Explorar e nunca sejam filtrados
    function syncCurrentProfileIntoExplore() {
        const uname = (typeof activeUsername !== 'undefined' && activeUsername)
            ? String(activeUsername).trim()
            : ((document.getElementById('my-name-label') && document.getElementById('my-name-label').textContent.trim()) || 'AkiiraPW');
        if (!uname) return;

        const handle = '@' + uname.toLowerCase().replace(/^@/, '');
        const prof = (typeof myProfileData !== 'undefined' && myProfileData) ? myProfileData : {};
        const posts = (typeof userProfilePosts !== 'undefined' && Array.isArray(userProfilePosts))
            ? userProfilePosts.filter(p => !isFakePost(p))
            : [];

        const realEntry = {
            username: uname,
            handle: handle,
            bubble: prof.bubble || '',
            pronouns: prof.pronouns || '',
            bio: prof.bio || '',
            location: prof.location || '',
            joinedAt: prof.joinedAt || '',
            avatarUrl: prof.avatarUrl || '',
            bannerUrl: prof.bannerUrl || '',
            is_admin: Boolean(prof.is_admin || uname.toLowerCase() === 'akiirapw'),
            posts: posts
        };

        if (typeof exploreAccountsList !== 'undefined' && Array.isArray(exploreAccountsList)) {
            const idx = exploreAccountsList.findIndex(a => String((a && a.username) || '').toLowerCase() === uname.toLowerCase());
            if (idx >= 0) {
                exploreAccountsList[idx] = Object.assign({}, exploreAccountsList[idx], realEntry);
            } else {
                exploreAccountsList.unshift(realEntry);
            }
            try {
                localStorage.setItem('kiwi_explore_accounts_v1', JSON.stringify(exploreAccountsList.filter(a => !isFakeAccount(a))));
            } catch (_) {}
        }

        if (typeof profilesByUsername !== 'undefined' && profilesByUsername && typeof profilesByUsername.set === 'function') {
            profilesByUsername.set(uname.toLowerCase(), realEntry);
        }
    }

    // Garante que a busca no Explorar considere o perfil atual mesmo quando o usuário digita "@akiirapw" ou "AkiiraPW"
    if (typeof window.renderExploreResults === 'function') {
        const origRenderExplore = window.renderExploreResults;
        window.renderExploreResults = function patchedRenderExploreResults() {
            syncCurrentProfileIntoExplore();
            if (typeof exploreAccountsList !== 'undefined' && Array.isArray(exploreAccountsList)) {
                for (let i = exploreAccountsList.length - 1; i >= 0; i--) {
                    if (isFakeAccount(exploreAccountsList[i])) exploreAccountsList.splice(i, 1);
                }
            }
            return origRenderExplore.apply(this, arguments);
        };
    }

    ensureSaveButtonInEditModal();
    syncCurrentProfileIntoExplore();

    // Observa abertura do modal de edição para garantir que o botão Salvar esteja presente
    const obs = new MutationObserver(() => {
        ensureSaveButtonInEditModal();
        syncCurrentProfileIntoExplore();
    });
    obs.observe(document.body, { childList: true, subtree: true });
})();

    // 4) Aba "Início": perfil no topo + postagens logo abaixo
    function collectInicioFeedPosts() {
        const collected = [];
        const seenIds = new Set();

        const pushUniquePost = (post, fallbackAuthor) => {
            if (!post || isFakePost(post)) return;
            const author = post.author || post.username || fallbackAuthor || 'AkiiraPW';
            const postId = post.id || `${author}-${post.text || ''}-${post.time || ''}`;
            if (seenIds.has(String(postId))) return;
            seenIds.add(String(postId));
            collected.push(Object.assign({}, post, {
                id: post.id || postId,
                author
            }));
        };

        if (typeof userPosts !== 'undefined' && Array.isArray(userPosts)) {
            userPosts.forEach(p => pushUniquePost(p, 'AkiiraPW'));
        }
        if (typeof window.userPosts !== 'undefined' && Array.isArray(window.userPosts)) {
            window.userPosts.forEach(p => pushUniquePost(p, 'AkiiraPW'));
        }
        if (typeof userProfilePosts !== 'undefined' && Array.isArray(userProfilePosts)) {
            userProfilePosts.forEach(p => pushUniquePost(p, 'AkiiraPW'));
        }
        if (typeof exploreAccountsList !== 'undefined' && Array.isArray(exploreAccountsList)) {
            exploreAccountsList.forEach(acc => {
                if (!acc || isFakeAccount(acc) || !Array.isArray(acc.posts)) return;
                acc.posts.forEach(p => pushUniquePost(p, acc.username));
            });
        }

        return collected;
    }

    function renderInicioTabFeed() {
        const chatLog = document.getElementById('chat-log');
        if (!chatLog) return;

        const uname = (typeof activeUsername !== 'undefined' && activeUsername)
            ? String(activeUsername).trim()
            : 'AkiiraPW';
        const prof = (typeof myProfileData !== 'undefined' && myProfileData) ? myProfileData : {};
        const handle = prof.handle || ('@' + uname.toLowerCase().replace(/^@/, ''));
        const posts = collectInicioFeedPosts();

        chatLog.innerHTML = '';

        // 1. Card do teu perfil no topo
        const topProfileCard = document.createElement('section');
        topProfileCard.className = 'social-list-item';
        topProfileCard.style.margin = '12px 16px';
        topProfileCard.style.padding = '12px 14px';
        topProfileCard.innerHTML = `
            <div class="social-avatar">${String(uname).charAt(0).toUpperCase()}</div>
            <div style="flex:1;min-width:0;">
                <div style="font-weight:800;font-size:15px;">${uname} <span style="font-weight:500;font-size:12px;opacity:0.75;">${handle}</span></div>
                ${prof.bio ? `<div style="font-size:12px;opacity:0.85;margin-top:2px;">${prof.bio}</div>` : ''}
            </div>
        `;
        chatLog.appendChild(topProfileCard);

        // 2. Postagens logo abaixo
        if (!posts.length) {
            const emptyEl = document.createElement('div');
            emptyEl.style.padding = '24px 16px';
            emptyEl.style.textAlign = 'center';
            emptyEl.style.opacity = '0.75';
            emptyEl.textContent = 'Nenhuma postagem no feed ainda.';
            chatLog.appendChild(emptyEl);
            return;
        }

        posts.forEach(post => {
            const card = document.createElement('article');
            card.className = 'social-list-item';
            card.style.margin = '8px 16px';
            card.style.alignItems = 'flex-start';
            const author = post.author || uname;
            card.innerHTML = `
                <div class="social-avatar">${String(author).charAt(0).toUpperCase()}</div>
                <div style="flex:1;min-width:0;">
                    <div style="font-weight:700;font-size:13px;">${author} <span style="font-weight:400;font-size:11px;opacity:0.7;">@${String(author).toLowerCase()} · ${post.time || 'agora'}</span></div>
                    <div style="font-size:13px;margin-top:4px;white-space:pre-line;word-break:break-word;">${post.text || ''}</div>
                </div>
            `;
            if (typeof window.openPostDetailTab === 'function' && post.id) {
                card.addEventListener('click', () => window.openPostDetailTab(post.id, 'inicio'));
            }
            chatLog.appendChild(card);
        });
    }

    document.addEventListener('click', (ev) => {
        const btn = ev.target && ev.target.closest
            ? ev.target.closest('[data-nav-tab="inicio"], #nav-tab-inicio')
            : null;
        if (!btn) return;
        setTimeout(renderInicioTabFeed, 30);
    });