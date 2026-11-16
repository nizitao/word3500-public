// SPDX-FileCopyrightText: 2026 word3500 contributors
// SPDX-License-Identifier: GPL-3.0

// admin.js — 管理员用户管理界面（CRUD + 搜索 + 权限控制）

// 鉴权
if (typeof requireLogin !== 'function' || !requireLogin()) { /* 已跳转 */ }
if (typeof onSessionExpired === 'function') {
  onSessionExpired(() => { logout(); });
}

// 检查管理员权限
const currentUser = typeof getCurrentUser === 'function' ? getCurrentUser() : null;
if (!currentUser || currentUser.role !== 'admin') {
  alert('无权访问：仅管理员可进入此页面');
  location.href = '/';
}

// ---- 状态 ----
let users = [];
let filtered = [];
const fetcher = typeof authFetch === 'function' ? authFetch : fetch;

// ---- 用户栏 ----
(function () {
  const bar = document.getElementById('userBar');
  const name = document.getElementById('userName');
  const logoutBtn = document.getElementById('logoutBtn');
  if (bar && currentUser) {
    name.textContent = currentUser.display_name || currentUser.username;
    bar.style.display = 'inline-flex';
  }
  if (logoutBtn) {
    logoutBtn.onclick = () => { if (confirm('确定退出？')) logout(); };
  }
})();

// ---- 加载用户列表 ----
async function loadUsers() {
  try {
    const res = await fetcher('/api/auth/users');
    if (!res.ok) throw new Error('加载失败');
    const data = await res.json();
    users = data.users || [];
    applyFilter();
  } catch (e) {
    document.getElementById('userBody').innerHTML =
      '<tr><td colspan="7" style="text-align:center;color:#dc2626">加载失败，请确认管理员权限</td></tr>';
  }
}

function applyFilter() {
  const kw = (document.getElementById('searchInput').value || '').toLowerCase();
  filtered = kw ? users.filter(u =>
    u.username.toLowerCase().includes(kw) ||
    (u.display_name || '').toLowerCase().includes(kw) ||
    (u.email || '').toLowerCase().includes(kw)
  ) : [...users];
  renderTable();
}

function renderTable() {
  const tbody = document.getElementById('userBody');
  tbody.innerHTML = '';
  if (!filtered.length) {
    tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:#94a3b8">暂无用户</td></tr>';
    document.getElementById('tableInfo').textContent = '';
    return;
  }
  document.getElementById('tableInfo').textContent = `共 ${filtered.length} 个用户`;
  filtered.forEach(u => {
    const tr = document.createElement('tr');
    const roleBadge = u.role === 'admin'
      ? '<span class="badge badge-admin">管理员</span>'
      : '<span class="badge badge-user">普通用户</span>';
    const statusBadge = u.enabled
      ? '<span class="badge badge-on">启用</span>'
      : '<span class="badge badge-off">禁用</span>';
    const isSelf = u.id === (currentUser && currentUser.id);
    tr.innerHTML = `
      <td>${u.id}</td>
      <td><strong>${esc(u.username)}</strong>${u.display_name ? `<br><span style="font-size:11px;color:#94a3b8">${esc(u.display_name)}</span>` : ''}</td>
      <td style="font-size:12px">${esc((u.email && u.email.trim()) || '-')}</td>
      <td>${roleBadge}</td>
      <td>${statusBadge}</td>
      <td class="action-cell">
        <button class="btn-sm" data-action="edit" data-id="${u.id}" title="编辑">编辑</button>
        ${!isSelf ? `<button class="btn-sm btn-sm-danger" data-action="delete" data-id="${u.id}" title="删除">删除</button>` : ''}
      </td>`;
    tbody.appendChild(tr);
  });
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s || '';
  return d.innerHTML;
}

// ---- 事件委托 ----
document.getElementById('userBody').addEventListener('click', e => {
  const btn = e.target.closest('button');
  if (!btn) return;
  const action = btn.dataset.action;
  const id = parseInt(btn.dataset.id);
  if (action === 'edit') openEdit(id);
  if (action === 'delete') openConfirm(id);
});

document.getElementById('searchInput').addEventListener('input', applyFilter);

// ---- 新增用户 ----
document.getElementById('addUserBtn').onclick = () => openEdit(null);

function openEdit(id) {
  const modal = document.getElementById('editModal');
  document.getElementById('editError').textContent = '';
  document.getElementById('editError').className = 'error-msg';
  if (id) {
    document.getElementById('modalTitle').textContent = '编辑用户';
    document.getElementById('editId').value = id;
    document.getElementById('editPassword').placeholder = '留空则不修改';
    document.getElementById('editPassword').required = false;
    const u = users.find(x => x.id === id);
    if (u) {
      document.getElementById('editUsername').value = u.username;
      document.getElementById('editUsername').disabled = true;
      document.getElementById('editDisplayName').value = u.display_name || '';
      document.getElementById('editEmail').value = u.email || '';
      document.getElementById('editRole').value = u.role;
      document.getElementById('editPassword').value = '';
    }
  } else {
    document.getElementById('modalTitle').textContent = '新增用户';
    document.getElementById('editId').value = '';
    document.getElementById('editUsername').disabled = false;
    document.getElementById('editUsername').value = '';
    document.getElementById('editPassword').value = '';
    document.getElementById('editPassword').placeholder = '至少 6 位';
    document.getElementById('editPassword').required = true;
    document.getElementById('editDisplayName').value = '';
    document.getElementById('editEmail').value = '';
    document.getElementById('editRole').value = 'user';
  }
  modal.style.display = 'flex';
}

document.getElementById('cancelBtn').onclick = () => {
  document.getElementById('editModal').style.display = 'none';
};

document.getElementById('editForm').onsubmit = async (e) => {
  e.preventDefault();
  const id = document.getElementById('editId').value;
  const username = document.getElementById('editUsername').value.trim();
  const password = document.getElementById('editPassword').value;
  const displayName = document.getElementById('editDisplayName').value.trim();
  const email = document.getElementById('editEmail').value.trim();
  const role = document.getElementById('editRole').value;
  const errEl = document.getElementById('editError');

  if (id) {
    // 编辑
    if (password && password.length < 6) {
      errEl.textContent = '密码至少 6 位';
      errEl.className = 'error-msg visible';
      return;
    }
    const body = { action: 'update', id: parseInt(id), display_name: displayName, email, role };
    if (password) body.new_password = password;
    try {
      const res = await fetcher('/api/auth/users', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      const data = await res.json();
      if (!res.ok) throw new Error(data.message || '更新失败');
      // 后端 update 已在同一请求里处理 new_password，不再重复发 reset_password，
      // 避免两次请求出现「资料已更新但密码未重置」的中间态。
      document.getElementById('editModal').style.display = 'none';
      await loadUsers();
    } catch (err) {
      errEl.textContent = err.message;
      errEl.className = 'error-msg visible';
    }
  } else {
    // 新增
    if (!username || !password) {
      errEl.textContent = '用户名和密码不能为空';
      errEl.className = 'error-msg visible';
      return;
    }
    if (password.length < 6) {
      errEl.textContent = '密码至少 6 位';
      errEl.className = 'error-msg visible';
      return;
    }
    try {
      const body = { action: 'create', username, password, display_name: displayName, email, role };
      const res = await fetcher('/api/auth/users', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      const data = await res.json();
      if (!res.ok) throw new Error(data.message || '创建失败');
      document.getElementById('editModal').style.display = 'none';
      await loadUsers();
    } catch (err) {
      errEl.textContent = err.message;
      errEl.className = 'error-msg visible';
    }
  }
};

// ---- 删除确认 ----
let deleteTargetId = null;

function openConfirm(id) {
  deleteTargetId = id;
  const u = users.find(x => x.id === id);
  document.getElementById('confirmMsg').textContent =
    `确定要删除用户「${u ? u.username : id}」吗？此操作不可撤销。`;
  document.getElementById('confirmModal').style.display = 'flex';
}

document.getElementById('confirmNo').onclick = () => {
  document.getElementById('confirmModal').style.display = 'none';
  deleteTargetId = null;
};

document.getElementById('confirmYes').onclick = async () => {
  if (!deleteTargetId) return;
  try {
    const res = await fetcher('/api/auth/users', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'delete', id: deleteTargetId }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.message || '删除失败');
  } catch (e) {
    alert('删除失败: ' + e.message);
  }
  document.getElementById('confirmModal').style.display = 'none';
  deleteTargetId = null;
  await loadUsers();
};

// ---- 点击遮罩关闭弹窗 ----
document.getElementById('editModal').addEventListener('click', function(e) {
  if (e.target === this) this.style.display = 'none';
});
document.getElementById('confirmModal').addEventListener('click', function(e) {
  if (e.target === this) this.style.display = 'none';
});

// ---- 启动 ----
loadUsers();
