(function () {
  const panel = document.getElementById('kiwiMembersPanel');
  if (!panel) return;

  const container = document.getElementById('kiwiMembersGroupsContainer');
  const toggleBtn = document.getElementById('toggleMembersDrawerBtn');
  const closeMobileBtn = document.getElementById('closeMembersMobileBtn');
  const inviteBoxWrap = document.getElementById('kiwiInviteBoxWrap');
  const openModalBtn = document.getElementById('openAddMemberModalBtn');
  const modal = document.getElementById('kiwiAddMemberModal');
  const closeModalBtn = document.getElementById('closeAddMemberModalBtn');
  const cancelModalBtn = document.getElementById('cancelAddMemberBtn');
  const addForm = document.getElementById('kiwiAddMemberForm');
  const usernameInput = document.getElementById('addMemberUsernameInput');
  const feedbackEl = document.getElementById('addMemberFeedback');

  let state = {
    serverId: window.ACTIVE_SERVER_ID || null,
    currentUserId: window.CURRENT_USER_ID || null,
    canManage: false,
    isOwner: false,
    members: [],
  };

  function getCsrfToken() {
    const match = document.cookie.match(/(?:^|; )csrftoken=([^;]*)/);
    return match ? decodeURIComponent(match[1]) : '';
  }

  function escapeHtml(str) {
    return String(str || '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function isMobileViewport() {
    return window.innerWidth <= 820;
  }

  function togglePanel() {
    if (isMobileViewport()) {
      panel.classList.toggle('is-open-mobile');
    } else {
      panel.classList.toggle('is-hidden-desktop');
      if (toggleBtn) {
        toggleBtn.classList.toggle('active', !panel.classList.contains('is-hidden-desktop'));
      }
    }
  }

  if (toggleBtn) toggleBtn.addEventListener('click', togglePanel);
  if (closeMobileBtn) {
    closeMobileBtn.addEventListener('click', () => panel.classList.remove('is-open-mobile'));
  }

  function openAddModal() {
    if (!modal) return;
    feedbackEl.textContent = '';
    usernameInput.value = '';
    modal.hidden = false;
    setTimeout(() => usernameInput.focus(), 40);
  }

  function closeAddModal() {
    if (modal) modal.hidden = true;
  }

  if (openModalBtn) openModalBtn.addEventListener('click', openAddModal);
  if (closeModalBtn) closeModalBtn.addEventListener('click', closeAddModal);
  if (cancelModalBtn) cancelModalBtn.addEventListener('click', closeAddModal);

  function renderMembers() {
    if (inviteBoxWrap) {
      inviteBoxWrap.style.display = state.canManage ? 'block' : 'none';
    }

    if (!state.members.length) {
      container.innerHTML = '<div class="kiwi-members-loading">Nenhum membro encontrado.</div>';
      return;
    }

    const groupsOrder = [
      { key: 'owner', title: 'Owner' },
      { key: 'admin', title: 'Moderador' },
      { key: 'member', title: 'Membros' },
    ];

    let html = '';
    for (const group of groupsOrder) {
      const list = state.members.filter((m) => m.role === group.key);
      if (!list.length) continue;

      html += `<section class="kiwi-role-group">`;
      html += `<div class="kiwi-role-header">${escapeHtml(group.title)} — ${list.length}</div>`;
      html += `<div class="kiwi-role-group-cards">`;

      for (const m of list) {
        const initial = escapeHtml((m.username || '?')[0].toUpperCase());
        const avatarHtml = m.avatar_url
          ? `<img class="kiwi-member-avatar" src="${escapeHtml(m.avatar_url)}" alt="${escapeHtml(m.username)}">`
          : `<div class="kiwi-member-avatar">${initial}</div>`;

        const bannerHtml = m.banner_url
          ? `<div class="kiwi-member-card-banner" style="background-image:url('${escapeHtml(m.banner_url)}')"></div>`
          : '';

        const badgeHtml = m.role !== 'member'
          ? `<span class="kiwi-member-badge">${escapeHtml(m.role_label)}</span>`
          : '';

        const canRemove = state.canManage && !m.is_owner && Number(m.id) !== Number(state.currentUserId);
        const canPromote = state.isOwner && !m.is_owner;

        let actionsHtml = '';
        if (canPromote || canRemove) {
          actionsHtml += `<div class="kiwi-member-actions">`;
          if (canPromote) {
            const nextRole = m.role === 'admin' ? 'member' : 'admin';
            const label = m.role === 'admin' ? '↓' : '★';
            const title = m.role === 'admin' ? 'Rebaixar para Membro' : 'Promover a Moderador';
            actionsHtml += `<button type="button" class="kiwi-member-action-btn" data-action="role" data-user-id="${m.id}" data-next-role="${nextRole}" title="${title}">${label}</button>`;
          }
          if (canRemove) {
            actionsHtml += `<button type="button" class="kiwi-member-action-btn danger" data-action="remove" data-user-id="${m.id}" data-username="${escapeHtml(m.username)}" title="Remover do servidor">✕</button>`;
          }
          actionsHtml += `</div>`;
        }

        html += `
          <div class="kiwi-member-card" data-member-id="${m.id}">
            ${bannerHtml}
            <div class="kiwi-member-avatar-wrap">
              ${avatarHtml}
              <span class="kiwi-member-status-dot"></span>
            </div>
            <div class="kiwi-member-info">
              <div class="kiwi-member-name-row">
                <span class="kiwi-member-name" style="color:${escapeHtml(m.accent_color || '#f2f3f5')}">${escapeHtml(m.username)}</span>
                ${badgeHtml}
              </div>
              ${m.activity ? `<div class="kiwi-member-activity">${escapeHtml(m.activity)}</div>` : ''}
            </div>
            ${actionsHtml}
          </div>
        `;
      }

      html += `</div></section>`;
    }

    container.innerHTML = html;
  }

  async function loadServerMembers(serverId) {
    if (!serverId) return;
    state.serverId = serverId;
    try {
      const res = await fetch(`/api/servers/${serverId}/members/`, {
        headers: { 'X-Requested-With': 'XMLHttpRequest' },
      });
      const data = await res.json();
      if (!res.ok || !data.ok) return;
      state.canManage = Boolean(data.can_manage);
      state.isOwner = Boolean(data.is_owner);
      state.members = data.members || [];
      renderMembers();
    } catch (err) {
      console.error('Erro ao carregar membros do servidor:', err);
    }
  }

  if (addForm) {
    addForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      if (!state.serverId) return;
      const username = usernameInput.value.trim();
      if (!username) return;

      feedbackEl.textContent = '';
      try {
        const res = await fetch(`/api/servers/${state.serverId}/members/add/`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': getCsrfToken(),
          },
          body: JSON.stringify({ username }),
        });
        const data = await res.json();
        if (!res.ok || !data.ok) {
          feedbackEl.textContent = data.error || 'Não foi possível adicionar.';
          return;
        }
        closeAddModal();
        await loadServerMembers(state.serverId);
      } catch (err) {
        feedbackEl.textContent = 'Erro de conexão ao adicionar membro.';
      }
    });
  }

  container.addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-action]');
    if (!btn || !state.serverId) return;
    e.stopPropagation();

    const action = btn.dataset.action;
    const userId = btn.dataset.userId;

    if (action === 'remove') {
      const username = btn.dataset.username || 'este membro';
      if (!confirm(`Remover @${username} do servidor?`)) return;

      const res = await fetch(`/api/servers/${state.serverId}/members/${userId}/remove/`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-CSRFToken': getCsrfToken(),
        },
      });
      if (res.ok) {
        await loadServerMembers(state.serverId);
      }
    } else if (action === 'role') {
      const nextRole = btn.dataset.nextRole;
      const res = await fetch(`/api/servers/${state.serverId}/members/${userId}/role/`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-CSRFToken': getCsrfToken(),
        },
        body: JSON.stringify({ role: nextRole }),
      });
      if (res.ok) {
        await loadServerMembers(state.serverId);
      }
    }
  });

  // Exponha funções globais para integrar com a troca de servidor e o WebSocket existente
  window.KiwiServerMembers = {
    load: loadServerMembers,
    handleWsEvent(data) {
      if (!data || data.type !== 'server_member_update') return;
      if (Number(data.server_id) !== Number(state.serverId)) return;

      if (data.action === 'removed' && Number(data.user_id) === Number(state.currentUserId)) {
        alert('Você foi removido deste servidor.');
        window.location.href = '/';
        return;
      }
      loadServerMembers(state.serverId);
    },
  };

  if (state.serverId) {
    loadServerMembers(state.serverId);
  }
})();
