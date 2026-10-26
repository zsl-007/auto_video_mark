(function () {
    'use strict';
  
    // ============ 常量 ============
    const STATUS_LABELS = {
      queued: '排队中',
      prechecking: '预检中',
      compressing: '压缩中',
      segmenting: '分段中',
      static_processing: '静态处理中',
      anonymizing: '匿名化中',
      merging: '合并中',
      completed: '已完成',
      failed: '失败',
    };
    const STATUS_PROGRESS = {
      queued: 0, prechecking: 10, compressing: 18, segmenting: 25,
      static_processing: 40, anonymizing: 65, merging: 90,
      completed: 100, failed: 100,
    };
    const RUNNING_STATUSES = new Set([
      'prechecking', 'compressing', 'segmenting', 'static_processing', 'anonymizing', 'merging',
    ]);
    const INPUT_FILE_STATUS = {
      unprocessed: { label: '未处理',   cls: 'pending'   },
      processing:  { label: '处理中',   cls: 'running'   },
      success:     { label: '处理成功', cls: 'completed' },
      failed:      { label: '处理失败', cls: 'failed'    },
    };
    const EDITABLE_SETTINGS = [
      { key: 'video_compress_enabled', type: 'boolean' },
      { key: 'min_resolution',   type: 'number', min: 144, max: 2160, step: 1 },
      { key: 'segment_enabled',  type: 'boolean' },
      { key: 'segment_duration', type: 'number', min: 1, max: 7200, step: 1 },
      { key: 'static_process_enabled',   type: 'boolean' },
      { key: 'static_process_threshold', type: 'number', min: 0, max: 1, step: 0.01 },
      { key: 'static_frame_drop_enabled', type: 'boolean' },
      { key: 'anonymize_face',   type: 'boolean' },
      { key: 'anonymize_plate',  type: 'boolean' },
      { key: 'blur_method',      type: 'select', options: [
        { value: 'pixelate', label: '马赛克' },
        { value: 'gaussian', label: '高斯模糊' },
      ]},
      { key: 'merge_enabled',    type: 'boolean' },
    ];
    const SETTING_LABELS = {
      video_compress_enabled: { label: '启用视频压缩', fmt: v => v ? '开启' : '关闭' },
      min_resolution: { label: '目标分辨率', fmt: v => `${v}p` },
      segment_enabled: { label: '启用分段', fmt: v => v ? '是' : '否' },
      segment_duration: { label: '分段时长', fmt: v => `${v} 秒` },
      static_process_enabled: { label: '启用静态处理', fmt: v => v ? '开启' : '关闭' },
      static_process_threshold: { label: '静态处理阈值(静态率高于阈值的段将丢弃)', fmt: v => v },
      static_frame_drop_enabled: { label: '丢弃静态帧', fmt: v => v ? '开启（输出无音频）' : '关闭', },
      anonymize_face: { label: '人脸匿名化', fmt: v => v ? '开启' : '关闭' },
      anonymize_plate: { label: '车牌匿名化', fmt: v => v ? '开启' : '关闭' },
      blur_method: { label: '模糊方式', fmt: v => v === 'pixelate' ? '马赛克' : '高斯模糊' },
      merge_enabled: { label: '启用合并', fmt: v => v ? '是' : '否' },
      max_upload_size: { label: '最大上传', fmt: v => formatBytes(v) },
      supported_extensions: { label: '支持格式', fmt: v => (v || []).map(e => e.replace('.', '').toUpperCase()).join(' / ') },
    };
    const HIDDEN_SETTING_KEYS = new Set(['input_dir', 'output_dir']);
  
    // ============ 状态 ============
    const state = {
      tasks: [], errors: [], settings: null, summary: null,
      inputFiles: [], outputFiles: [],
      filter: { status: '', q: '', timeRange: '' },
      view: { tasks: 'detailed' },
      page: { tasks: 1, inputFiles: 1, outputFiles: 1 },
      pageSize: { tasks: 10, inputFiles: 8, outputFiles: 8 },
      ws: null, wsRetry: 0, lastErrorCount: 0,
    };
    let settingsEditing = false;
  
    // ============ DOM ============
    const $ = sel => document.querySelector(sel);
    const els = {
      statsGrid: $('#stats-grid'),
      tasksTable: $('#tasks-table'),
      tasksBody: $('#tasks-body'),
      tasksPager: $('#tasks-pager'),
      errorList: $('#error-list'),
      settingsList: $('#settings-list'),
      searchInput: $('#search-input'),
      statusFilter: $('#status-filter'),
      timeFilter: $('#time-filter'),
      taskViewToggle: $('#task-view-toggle'),
      refreshBtn: $('#refresh-btn'),
      themeBtn: $('#theme-btn'),
      wsStatus: $('#ws-status'),
      drawer: $('#drawer'),
      drawerBody: $('#drawer-body'),
      drawerClose: $('#drawer-close'),
      drawerBackdrop: $('#drawer-backdrop'),
      clearErrors: $('#clear-errors'),
      toastContainer: $('#toast-container'),
      uploadZone: $('#upload-zone'),
      fileInput: $('#file-input'),
      uploadList: $('#upload-list'),
      uploadHint: $('#upload-hint'),
      browseLink: $('#browse-link'),
      inputFilesBody: $('#input-files-body'),
      outputFilesBody: $('#output-files-body'),
      inputFilesRefresh: $('#input-files-refresh'),
      outputFilesRefresh: $('#output-files-refresh'),
      inputFilesCount: $('#input-files-count'),
      outputFilesCount: $('#output-files-count'),
      inputPager: $('#input-pager'),
      outputPager: $('#output-pager'),
      settingsEditBtn: $('#settings-edit-btn'),
    };
  
    // ============ 工具函数 ============
    function escapeHtml(s) {
      if (s == null) return '';
      return String(s)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
    }
    function parseUTC(iso) {
      if (!iso) return null;
      if (iso instanceof Date) return iso;
      if (/[zZ]|[+-]\d{2}:?\d{2}$/.test(iso)) return new Date(iso);
      return new Date(iso + 'Z');
    }
    function timeAgo(iso) {
      const d = parseUTC(iso);
      if (!d || isNaN(d)) return '-';
      const diff = (Date.now() - d.getTime()) / 1000;
      if (diff < 60) return '刚刚';
      if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
      if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
      if (diff < 604800) return `${Math.floor(diff / 86400)} 天前`;
      return d.toLocaleDateString('zh-CN');
    }
    function fmtAbsolute(iso) {
      const d = parseUTC(iso);
      if (!d || isNaN(d)) return '-';
      return d.toLocaleString('zh-CN', { hour12: false });
    }
    function fmtDuration(sec) {
      if (sec < 60) return `${Math.floor(sec)} 秒`;
      if (sec < 3600) return `${Math.floor(sec / 60)} 分 ${Math.floor(sec % 60)} 秒`;
      return `${Math.floor(sec / 3600)} 时 ${Math.floor((sec % 3600) / 60)} 分`;
    }
    function taskDuration(task) {
        // 优先用 started_at → finished_at（纯处理时间）；老数据回退到 created_at → updated_at
        const startIso = task.started_at || task.created_at;
        const endIso = task.finished_at || task.updated_at;
        const a = parseUTC(startIso);
        const b = parseUTC(endIso);
        if (!a || !b) return '-';
        return fmtDuration(Math.max(0, (b - a) / 1000));
      }
      function taskQueueWait(task) {
        if (!task.started_at) return null;
        const a = parseUTC(task.created_at);
        const b = parseUTC(task.started_at);
        if (!a || !b) return null;
        const sec = Math.max(0, (b - a) / 1000);
        return sec < 1 ? null : fmtDuration(sec);
      }    
    function basename(p) {
      if (!p) return '';
      return p.split(/[\\/]/).pop();
    }
    function statusClass(status) {
      if (status === 'completed') return 'completed';
      if (status === 'failed') return 'failed';
      if (RUNNING_STATUSES.has(status)) return 'running';
      return 'pending';
    }
    function debounce(fn, ms) {
      let t;
      return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
    }
    function showToast(msg, type = 'info', timeout = 3000) {
      const el = document.createElement('div');
      el.className = `toast ${type}`;
      el.textContent = msg;
      els.toastContainer.appendChild(el);
      setTimeout(() => {
        el.style.opacity = '0';
        el.style.transform = 'translateY(6px)';
        el.style.transition = 'opacity .25s, transform .25s';
        setTimeout(() => el.remove(), 250);
      }, timeout);
    }
    function formatBytes(bytes) {
      if (bytes == null) return '-';
      if (bytes < 1024) return `${bytes} B`;
      if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
      if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
      return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`;
    }
    function mtimeIso(mtime) {
      return new Date(mtime * 1000).toISOString();
    }
  
    // ============ 分页 ============
    function paginate(list, page, size) {
      const total = list.length;
      const totalPages = Math.max(1, Math.ceil(total / size));
      const p = Math.min(Math.max(1, page), totalPages);
      const start = (p - 1) * size;
      return { items: list.slice(start, start + size), page: p, totalPages, total };
    }
    function renderPager(el, info, onGo) {
      if (!info.total) { el.innerHTML = ''; return; }
      if (info.totalPages <= 1) {
        el.innerHTML = `<span class="pager-info">共 ${info.total} 条</span>`;
        return;
      }
      el.innerHTML = `
        <span class="pager-info">第 ${info.page} / ${info.totalPages} 页 · 共 ${info.total} 条</span>
        <button class="btn small ghost" data-page="prev" ${info.page <= 1 ? 'disabled' : ''}>‹ 上一页</button>
        <button class="btn small ghost" data-page="next" ${info.page >= info.totalPages ? 'disabled' : ''}>下一页 ›</button>
      `;
      el.querySelectorAll('button[data-page]').forEach(btn => {
        btn.onclick = () => {
          const target = btn.dataset.page === 'prev' ? info.page - 1 : info.page + 1;
          onGo(target);
        };
      });
    }
  
    // ============ API 封装 ============
    async function api(path, opts = {}) {
      const res = await fetch(path, {
        headers: { Accept: 'application/json', ...(opts.headers || {}) },
        ...opts,
      });
      if (!res.ok) {
        const text = await res.text().catch(() => '');
        throw new Error(`${res.status} ${res.statusText}${text ? ': ' + text.slice(0, 200) : ''}`);
      }
      const ct = res.headers.get('content-type') || '';
      if (!ct.includes('application/json')) return null;
      return res.json();
    }
  
    // ============ 渲染：统计卡片 ============
    function computeStats() {
      const total = state.tasks.length;
      const running = state.tasks.filter(t => RUNNING_STATUSES.has(t.status)).length;
      const completed = state.tasks.filter(t => t.status === 'completed').length;
      const failed = state.tasks.filter(t => t.status === 'failed').length;
      const dropped = state.summary ? (state.summary.segments_dropped || 0) : 0;
      return { total, running, completed, failed, dropped };
    }
    function renderStats() {
      const s = computeStats();
      const cards = [
        { icon: '📋', value: s.total, label: '总任务', cls: '' },
        { icon: '⏳', value: s.running, label: '处理中', cls: 'running' },
        { icon: '✅', value: s.completed, label: '已完成', cls: 'completed' },
        { icon: '❌', value: s.failed, label: '失败', cls: 'failed' },
        { icon: '🗑️', value: s.dropped, label: '丢弃分段', cls: 'dropped' },
      ];
      els.statsGrid.innerHTML = cards.map(c => `
        <div class="stat-card ${c.cls}">
          <div class="stat-icon">${c.icon}</div>
          <div class="stat-meta">
            <div class="stat-value">${c.value}</div>
            <div class="stat-label">${c.label}</div>
          </div>
        </div>
      `).join('');
    }
  
    // ============ 渲染：任务列表 ============
    function matchesTimeRange(createdIso, range) {
      if (!range) return true;
      const d = parseUTC(createdIso);
      if (!d || isNaN(d)) return false;
      if (range === 'today') {
        const start = new Date();
        start.setHours(0, 0, 0, 0);
        return d >= start;
      }
      const now = Date.now();
      const diff = now - d.getTime();
      if (range === 'week') return diff <= 7 * 86400000;
      if (range === 'month') return diff <= 30 * 86400000;
      return true;
    }
    function filteredTasks() {
      const f = state.filter;
      return state.tasks.filter(t => {
        if (f.status && t.status !== f.status) return false;
        if (f.q) {
          const q = f.q.toLowerCase();
          const hay = `${t.id} ${t.source_path || ''} ${t.output_path || ''}`.toLowerCase();
          if (!hay.includes(q)) return false;
        }
        if (!matchesTimeRange(t.created_at, f.timeRange)) return false;
        return true;
      });
    }
    function renderTasks() {
      const list = filteredTasks();
      const { items, page, totalPages, total } = paginate(list, state.page.tasks, state.pageSize.tasks);
      state.page.tasks = page;
  
      if (!state.tasks.length) {
        els.tasksBody.innerHTML = `<tr><td colspan="7" class="empty">暂无任务。在上方上传视频，或将文件放入 input/ 目录。</td></tr>`;
        renderPager(els.tasksPager, { page: 1, totalPages: 1, total: 0 }, () => {});
        return;
      }
      if (!items.length) {
        els.tasksBody.innerHTML = `<tr><td colspan="7" class="empty">没有匹配的任务。</td></tr>`;
        renderPager(els.tasksPager, { page: 1, totalPages: 1, total }, () => {});
        return;
      }
      els.tasksBody.innerHTML = items.map(renderTaskRow).join('');
      renderPager(els.tasksPager, { page, totalPages, total }, p => {
        state.page.tasks = p;
        renderTasks();
        els.tasksTable.scrollIntoView({ behavior: 'smooth', block: 'start' });
      });
    }
    function renderTaskRow(task) {
        const cls = statusClass(task.status);
        const progress = STATUS_PROGRESS[task.status] ?? 0;
        const outName = task.output_path ? basename(task.output_path) : '';
        const download = task.output_path
          ? `<a href="/output/${encodeURIComponent(outName)}" download
                class="file-name" title="下载输出文件"
                onclick="event.stopPropagation()">${escapeHtml(outName)}</a>`
          : '<span class="muted">-</span>';
    
        const duration = taskDuration(task);
        const queueWait = taskQueueWait(task);          // ← 只算一次
    
        return `<tr data-task-id="${escapeHtml(task.id)}" tabindex="0">
          <td>
            <span class="file-name" title="${escapeHtml(task.source_path)}">
              <span class="icon">🎞️</span>${escapeHtml(basename(task.source_path))}
            </span>
          </td>
          <td><span class="badge ${cls}"><span class="dot"></span>${escapeHtml(STATUS_LABELS[task.status] || task.status)}</span></td>
          <td>
            <div class="progress">
              <div class="progress-bar">
                <div class="progress-fill ${cls}" style="width:${progress}%"></div>
              </div>
              <span class="progress-text">${progress}%</span>
            </div>
          </td>
          <td>${download}</td>
          <td class="col-time" title="${escapeHtml(fmtAbsolute(task.created_at))}">${timeAgo(task.created_at)}</td>
          <td class="col-duration muted">
            ${duration}${queueWait ? ` <span class="muted">（排队 ${queueWait}）</span>` : ''}
          </td>
          <td><button class="btn small ghost" data-action="detail">详情</button></td>
        </tr>`;
      }
  
    // ============ 渲染：错误日志 ============
    function renderErrors() {
      if (!state.errors.length) {
        els.errorList.innerHTML = `<li class="empty">暂无错误记录。</li>`;
        return;
      }
      els.errorList.innerHTML = state.errors.slice(0, 80).map(e => `
        <li>
          <span class="error-stage">${escapeHtml(e.stage || 'unknown')}</span>
          <div class="error-body">
            <div class="error-msg">${escapeHtml(e.message || '')}</div>
            <div class="error-meta" title="${escapeHtml(fmtAbsolute(e.created_at))}">
              ${e.task_id ? '任务 ' + escapeHtml(e.task_id.slice(0, 8)) + ' · ' : ''}${timeAgo(e.created_at)}
            </div>
          </div>
        </li>
      `).join('');
    }
  
    // ============ 渲染：运行配置 ============
    function renderSettings() {
      if (!state.settings) {
        els.settingsList.innerHTML = `<dd class="empty">加载中...</dd>`;
        return;
      }
      const rows = Object.entries(state.settings)
        .filter(([k]) => !HIDDEN_SETTING_KEYS.has(k))
        .map(([k, v]) => {
          const meta = SETTING_LABELS[k];
          const label = meta ? meta.label : k;
          const val = meta ? meta.fmt(v) : String(v);
          return `<div class="row"><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(val)}</dd></div>`;
        });
      els.settingsList.innerHTML = rows.length ? rows.join('') : `<dd class="empty">暂无配置信息</dd>`;
  
      const max = state.settings.max_upload_size;
      const exts = state.settings.supported_extensions;
      let hint = '拖拽视频到此处，或点击选择文件';
      if (exts && exts.length) hint += `（支持 ${exts.map(e => e.replace('.', '').toUpperCase()).join(' / ')}）`;
      if (max) hint += `，单文件 ≤ ${formatBytes(max)}`;
      els.uploadHint.textContent = hint;
    }
  
    // ============ 渲染：Input / Output ============
    function renderInputFiles() {
      const list = state.inputFiles;
      els.inputFilesCount.textContent = list.length ? `${list.length} 个` : '';
      const { items, page, totalPages, total } = paginate(list, state.page.inputFiles, state.pageSize.inputFiles);
      state.page.inputFiles = page;
  
      if (!total) {
        els.inputFilesBody.innerHTML = `<tr><td colspan="5" class="empty">input/ 目录暂无文件</td></tr>`;
        renderPager(els.inputPager, { page: 1, totalPages: 1, total: 0 }, () => {});
        return;
      }
      els.inputFilesBody.innerHTML = items.map((f, idx) => {
        const realIdx = (page - 1) * state.pageSize.inputFiles + idx;
        const meta = INPUT_FILE_STATUS[f.status] || INPUT_FILE_STATUS.unprocessed;
        const isProcessing = f.status === 'processing';
        const errTitle = f.error_message ? ` title="${escapeHtml(f.error_message)}"` : '';
        return `<tr data-idx="${realIdx}">
          <td>
            <span class="file-name" title="${escapeHtml(f.filename)}">
              <span class="icon">🎞️</span>${escapeHtml(f.filename)}
            </span>
          </td>
          <td class="muted">${formatBytes(f.size)}</td>
          <td><span class="badge ${meta.cls}"${errTitle}><span class="dot"></span>${meta.label}</span></td>
          <td class="muted" title="${escapeHtml(fmtAbsolute(mtimeIso(f.mtime)))}">${timeAgo(mtimeIso(f.mtime))}</td>
          <td>
            <button class="btn small ghost" type="button"
                    data-action="delete-input" data-idx="${realIdx}"
                    ${isProcessing ? 'disabled title="处理中，不可删除"' : ''}>删除</button>
          </td>
        </tr>`;
      }).join('');
      renderPager(els.inputPager, { page, totalPages, total }, p => {
        state.page.inputFiles = p;
        renderInputFiles();
      });
    }
    function renderOutputFiles() {
      const list = state.outputFiles;
      els.outputFilesCount.textContent = list.length ? `${list.length} 个` : '';
      const { items, page, totalPages, total } = paginate(list, state.page.outputFiles, state.pageSize.outputFiles);
      state.page.outputFiles = page;
  
      if (!total) {
        els.outputFilesBody.innerHTML = `<tr><td colspan="4" class="empty">output/ 目录暂无文件</td></tr>`;
        renderPager(els.outputPager, { page: 1, totalPages: 1, total: 0 }, () => {});
        return;
      }
      els.outputFilesBody.innerHTML = items.map((f, idx) => {
        const realIdx = (page - 1) * state.pageSize.outputFiles + idx;
        return `<tr data-idx="${realIdx}">
          <td>
            <span class="file-name" title="${escapeHtml(f.filename)}">
              <span class="icon">📄</span>${escapeHtml(f.filename)}
            </span>
          </td>
          <td class="muted">${formatBytes(f.size)}</td>
          <td class="muted" title="${escapeHtml(fmtAbsolute(mtimeIso(f.mtime)))}">${timeAgo(mtimeIso(f.mtime))}</td>
          <td>
            <button class="btn small ghost" type="button"
                    data-action="delete-output" data-idx="${realIdx}">删除</button>
          </td>
        </tr>`;
      }).join('');
      renderPager(els.outputPager, { page, totalPages, total }, p => {
        state.page.outputFiles = p;
        renderOutputFiles();
      });
    }
  
    // ============ 渲染：Settings 编辑表单 ============
    function renderSettingsForm() {
      if (!state.settings) return;
      const s = state.settings;
      const rows = EDITABLE_SETTINGS.map(item => {
        const meta = SETTING_LABELS[item.key] || { label: item.key };
        const val = s[item.key];
        let control = '';
        if (item.type === 'boolean') {
          control = `<label class="switch">
            <input type="checkbox" data-key="${item.key}" ${val ? 'checked' : ''}>
            <span data-role="switch-label">${val ? '开启' : '关闭'}</span>
          </label>`;
        } else if (item.type === 'number') {
          const shown = Number.isFinite(Number(val)) ? Number(val) : '';
          control = `<input type="number" data-key="${item.key}"
            value="${shown}" min="${item.min}" max="${item.max}" step="${item.step}">`;
        } else if (item.type === 'select') {
          control = `<select data-key="${item.key}">
            ${item.options.map(o =>
              `<option value="${o.value}" ${String(val) === o.value ? 'selected' : ''}>${o.label}</option>`
            ).join('')}
          </select>`;
        }
        return `<div class="row"><dt>${escapeHtml(meta.label)}</dt><dd>${control}</dd></div>`;
      }).join('');
  
      els.settingsList.innerHTML = rows + `
        <div class="row row-actions">
          <button type="button" class="btn small ghost" id="settings-cancel-btn">取消</button>
          <button type="button" class="btn small primary" id="settings-save-btn">保存</button>
        </div>`;
  
      els.settingsList.querySelectorAll('.switch input[type="checkbox"]').forEach(cb => {
        cb.addEventListener('change', () => {
          const label = cb.closest('.switch').querySelector('[data-role="switch-label"]');
          if (label) label.textContent = cb.checked ? '开启' : '关闭';
        });
      });
  
      document.getElementById('settings-cancel-btn').addEventListener('click', exitSettingsEdit);
      document.getElementById('settings-save-btn').addEventListener('click', saveSettingsForm);

      // 静态处理开关联动阈值输入
      const spSwitch = els.settingsList.querySelector('[data-key="static_process_enabled"]');
      const spThreshold = els.settingsList.querySelector('[data-key="static_process_threshold"]');
      if (spSwitch && spThreshold) {
        const sync = () => {
          spThreshold.disabled = !spSwitch.checked;
          spThreshold.style.opacity = spSwitch.checked ? '1' : '.5';
        };
        spSwitch.addEventListener('change', sync);
        sync();
      }      
    }
    function enterSettingsEdit() {
      if (settingsEditing) return;
      settingsEditing = true;
      els.settingsEditBtn.textContent = '取消';
      renderSettingsForm();
    }
    function exitSettingsEdit() {
      settingsEditing = false;
      els.settingsEditBtn.textContent = '编辑';
      renderSettings();
    }
    async function saveSettingsForm() {
      const payload = {};
      let invalid = false;
      els.settingsList.querySelectorAll('[data-key]').forEach(el => {
        const key = el.dataset.key;
        if (el.type === 'checkbox') { payload[key] = el.checked; return; }
        if (el.type === 'number') {
          const raw = el.value.trim();
          const num = Number(raw);
          const min = el.min === '' ? -Infinity : Number(el.min);
          const max = el.max === '' ? Infinity : Number(el.max);
          if (raw === '' || !Number.isFinite(num) || num < min || num > max) {
            invalid = true;
            el.classList.add('invalid');
            return;
          }
          el.classList.remove('invalid');
          payload[key] = num;
          return;
        }
        payload[key] = el.value;
      });
      if (invalid) {
        showToast('存在非法取值，请检查标红的输入框', 'error');
        return;
      }
      const saveBtn = document.getElementById('settings-save-btn');
      saveBtn.disabled = true;
      saveBtn.textContent = '保存中...';
      try {
        const updated = await api('/api/settings', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        state.settings = updated;
        showToast('配置已保存，对后续新任务生效', 'success');
        exitSettingsEdit();
      } catch (e) {
        showToast(`保存失败：${e.message}`, 'error', 4000);
        saveBtn.disabled = false;
        saveBtn.textContent = '保存';
      }
    }
  
    // ============ 加载数据 ============
    async function loadInputFiles() {
      try {
        const data = await api('/api/input-files');
        state.inputFiles = Array.isArray(data) ? data : [];
      } catch (e) {
        console.error('加载 input 文件失败', e);
        state.inputFiles = [];
      }
      renderInputFiles();
    }
    async function loadOutputFiles() {
      try {
        const data = await api('/api/output-files');
        state.outputFiles = Array.isArray(data) ? data : [];
      } catch (e) {
        console.error('加载 output 文件失败', e);
        state.outputFiles = [];
      }
      renderOutputFiles();
    }
    async function loadTasks() {
      try {
        const [tasks, summary] = await Promise.all([
          api('/api/tasks'),
          api('/api/stats/summary').catch(() => null),
        ]);
        state.tasks = Array.isArray(tasks) ? tasks : [];
        state.summary = summary;
        renderStats();
        renderTasks();
      } catch (e) {
        console.error('加载任务失败', e);
        els.tasksBody.innerHTML = `<tr><td colspan="7" class="empty">加载失败：${escapeHtml(e.message)}</td></tr>`;
      }
    }
    async function loadErrors() {
      try {
        const errors = await api('/api/errors');
        const list = Array.isArray(errors) ? errors : [];
        if (state.lastErrorCount && list.length > state.lastErrorCount) {
          showToast(`新增 ${list.length - state.lastErrorCount} 条错误记录`, 'error');
        }
        state.lastErrorCount = list.length;
        state.errors = list;
        renderErrors();
      } catch (e) {
        console.error('加载错误失败', e);
      }
    }
    async function loadSettings() {
      try {
        state.settings = await api('/api/settings');
        if (!settingsEditing) renderSettings();
      } catch (e) {
        els.settingsList.innerHTML = `<dd class="empty">加载配置失败</dd>`;
      }
    }
    async function loadAll() {
      await Promise.all([
        loadTasks(), loadErrors(), loadSettings(),
        loadInputFiles(), loadOutputFiles(),
      ]);
    }
  
    // ============ 删除 ============
    async function deleteInputFile(idx, btn) {
      const f = state.inputFiles[idx];
      if (!f) return;
      if (!confirm(`确定删除 input/${f.filename}？此操作不可恢复。`)) return;
      btn.disabled = true;
      try {
        await api(`/api/input-files/${encodeURIComponent(f.filename)}`, { method: 'DELETE' });
        showToast(`已删除 ${f.filename}`, 'success');
      } catch (e) {
        showToast(`删除失败：${e.message}`, 'error', 4000);
      } finally {
        await loadInputFiles();
      }
    }
    async function deleteOutputFile(idx, btn) {
      const f = state.outputFiles[idx];
      if (!f) return;
      if (!confirm(`确定删除 output/${f.filename}？`)) return;
      btn.disabled = true;
      try {
        await api(`/api/output-files/${encodeURIComponent(f.filename)}`, { method: 'DELETE' });
        showToast(`已删除 ${f.filename}`, 'success');
      } catch (e) {
        showToast(`删除失败：${e.message}`, 'error', 4000);
      } finally {
        await loadOutputFiles();
      }
    }
  
    // ============ 任务详情抽屉 ============
    async function openDrawer(taskId) {
      const task = state.tasks.find(t => t.id === taskId);
      if (!task) return;
  
      els.drawer.classList.add('open');
      els.drawer.setAttribute('aria-hidden', 'false');
      els.drawerBackdrop.classList.add('show');
      renderDrawer(task, undefined, []);
  
      const taskErrors = state.errors.filter(e => e.task_id === taskId);
      let segments = null;
      try {
        segments = await api(`/api/tasks/${encodeURIComponent(taskId)}/segments`);
      } catch (_) {
        segments = null;
      }
      renderDrawer(task, segments, taskErrors);
    }
    function renderDrawer(task, segments, taskErrors) {
      const cls = statusClass(task.status);
      const outName = task.output_path ? basename(task.output_path) : '';

      // 耗时拆分：总 = 排队 + 处理
        const duration = taskDuration(task);
        const queueWait = taskQueueWait(task);
        const totalSpan = (() => {
        const a = parseUTC(task.created_at);
        const b = parseUTC(task.finished_at || task.updated_at);
        if (!a || !b) return null;
        return fmtDuration(Math.max(0, (b - a) / 1000));
        })();  

        const segmentsHtml = (() => {
        if (segments === undefined) return `<p class="muted">加载中...</p>`;
        if (segments === null) return `<p class="muted">当前后端未提供分段明细接口。</p>`;
        if (!Array.isArray(segments) || segments.length === 0) return `<p class="muted">暂无分段记录。</p>`;
        return `<ul class="segment-list">
          ${segments.map(s => {
            const sc = s.status === 'dropped' ? 'failed' : s.status === 'kept' ? 'completed' : 'pending';
            const label = s.status === 'dropped' ? '丢弃' : s.status === 'kept' ? '保留' : '待定';
            const ratio = s.frame_ratio != null ? `保留帧比例 ${(s.frame_ratio * 100).toFixed(1)}%` : '';
            return `<li>
              <span>#${s.index} <span class="badge ${sc}"><span class="dot"></span>${label}</span></span>
              <span class="seg-meta">${escapeHtml(ratio)} · ${escapeHtml(basename(s.path))}</span>
            </li>`;
          }).join('')}
        </ul>`;
      })();
  
      const errorsHtml = taskErrors && taskErrors.length
        ? `<ul class="error-list" style="border:1px solid var(--border);border-radius:var(--radius-sm);">
            ${taskErrors.map(e => `
              <li>
                <span class="error-stage">${escapeHtml(e.stage)}</span>
                <div class="error-body">
                  <div class="error-msg">${escapeHtml(e.message)}</div>
                  <div class="error-meta">${escapeHtml(fmtAbsolute(e.created_at))}</div>
                </div>
              </li>`).join('')}
          </ul>`
        : `<p class="muted">无错误记录。</p>`;
  
      els.drawerBody.innerHTML = `
        <dl class="kv">
          <dt>任务 ID</dt><dd class="mono">${escapeHtml(task.id)}</dd>
          <dt>状态</dt><dd><span class="badge ${cls}"><span class="dot"></span>${escapeHtml(STATUS_LABELS[task.status] || task.status)}</span></dd>
          <dt>源文件</dt><dd>${escapeHtml(task.source_path)}</dd>
          <dt>输出文件</dt><dd>${outName
            ? `<a href="/output/${encodeURIComponent(outName)}" download>${escapeHtml(outName)}</a>`
            : '<span class="muted">-</span>'}</dd>
          <dt>创建时间</dt><dd>${escapeHtml(fmtAbsolute(task.created_at))}</dd>
          <dt>更新时间</dt><dd>${escapeHtml(fmtAbsolute(task.updated_at))}</dd>
          <dt>开始处理</dt><dd>${task.started_at ? escapeHtml(fmtAbsolute(task.started_at)) : '<span class="muted">-</span>'}</dd>
          <dt>完成时间</dt><dd>${task.finished_at ? escapeHtml(fmtAbsolute(task.finished_at)) : '<span class="muted">-</span>'}</dd>
          <dt>处理耗时</dt><dd>${duration}</dd>
          ${queueWait ? `<dt>排队等待</dt><dd>${queueWait}</dd>` : ''}
          ${totalSpan ? `<dt>总耗时</dt><dd class="muted">${totalSpan}</dd>` : ''}
          <dt>重试次数</dt><dd>${task.retry_count ?? 0}</dd>
          ${task.error_message ? `<dt>错误</dt><dd style="color:var(--danger)">${escapeHtml(task.error_message)}</dd>` : ''}
        </dl>
        <div class="drawer-section"><h3>分段明细</h3>${segmentsHtml}</div>
        <div class="drawer-section"><h3>错误记录</h3>${errorsHtml}</div>
      `;
    }
    function closeDrawer() {
      els.drawer.classList.remove('open');
      els.drawer.setAttribute('aria-hidden', 'true');
      els.drawerBackdrop.classList.remove('show');
    }
  
    // ============ WebSocket ============
    function updateWsStatus(status) {
      els.wsStatus.className = `ws-status ${status}`;
      const labels = { connecting: '连接中', open: '实时', closed: '已断开', reconnecting: '重连中' };
      els.wsStatus.innerHTML = `<span class="dot"></span> ${labels[status] || status}`;
    }
    function connectWS() {
      const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
      updateWsStatus(state.wsRetry > 0 ? 'reconnecting' : 'connecting');
      let ws;
      try {
        ws = new WebSocket(`${proto}//${location.host}/ws/progress`);
      } catch (e) {
        updateWsStatus('closed');
        return scheduleReconnect();
      }
      state.ws = ws;
      ws.onopen = () => { state.wsRetry = 0; updateWsStatus('open'); };
      ws.onmessage = (ev) => {
        let msg; try { msg = JSON.parse(ev.data); } catch (_) { return; }
        handleWsMessage(msg);
      };
      ws.onclose = () => { updateWsStatus('closed'); scheduleReconnect(); };
      ws.onerror = () => { try { ws.close(); } catch (_) {} };
    }
    function scheduleReconnect() {
      state.wsRetry += 1;
      const delay = Math.min(30000, 1000 * Math.pow(2, state.wsRetry - 1));
      updateWsStatus('reconnecting');
      setTimeout(connectWS, delay);
    }
    let filesRefreshTimer = null;
    function scheduleFilesRefresh() {
      if (filesRefreshTimer) return;
      filesRefreshTimer = setTimeout(() => {
        filesRefreshTimer = null;
        loadInputFiles();
        loadOutputFiles();
      }, 800);
    }
    function handleWsMessage(msg) {
      if (!msg || !msg.task_id) { loadAll(); return; }
      const task = state.tasks.find(t => t.id === msg.task_id);
      if (msg.status === 'completed') {
        showToast(`任务完成：${task ? basename(task.source_path) : msg.task_id.slice(0, 8)}`, 'success');
      } else if (msg.status === 'failed') {
        showToast(`任务失败：${msg.error || '未知错误'}`, 'error', 5000);
      }
      loadTasks();
      scheduleFilesRefresh();
      if (msg.status === 'failed' || msg.status === 'completed') loadErrors();
      if (els.drawer.classList.contains('open') && task) {
        setTimeout(() => {
          const t = state.tasks.find(x => x.id === msg.task_id);
          if (t) renderDrawer(t, undefined, state.errors.filter(e => e.task_id === t.id));
          api(`/api/tasks/${encodeURIComponent(msg.task_id)}/segments`)
            .then(segs => {
              const t2 = state.tasks.find(x => x.id === msg.task_id);
              if (t2 && els.drawer.classList.contains('open')) {
                renderDrawer(t2, segs, state.errors.filter(e => e.task_id === t2.id));
              }
            })
            .catch(() => {});
        }, 300);
      }
    }
  
    // ============ 上传 ============
    function addUploadItem(file) {
      const item = document.createElement('li');
      item.className = 'upload-item';
      item.innerHTML = `
        <div class="upload-item-row">
          <span class="upload-name">${escapeHtml(file.name)}</span>
          <span class="upload-meta">${formatBytes(file.size)}</span>
        </div>
        <div class="progress">
          <div class="progress-bar">
            <div class="progress-fill running" style="width:0%"></div>
          </div>
          <span class="progress-text">0%</span>
        </div>
      `;
      els.uploadList.appendChild(item);
      return item;
    }
    function updateUploadItem(item, percent, status) {
      const fill = item.querySelector('.progress-fill');
      const text = item.querySelector('.progress-text');
      fill.style.width = `${percent}%`;
      fill.className = `progress-fill ${status === 'completed' ? 'completed' : status === 'failed' ? 'failed' : 'running'}`;
      text.textContent = `${Math.round(percent)}%`;
      if (status === 'completed') item.classList.add('done');
      if (status === 'failed') item.classList.add('error');
    }
    function uploadOne(file) {
      return new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        const item = addUploadItem(file);
        xhr.open('POST', '/api/upload');
        xhr.upload.onprogress = (e) => {
          if (e.lengthComputable) updateUploadItem(item, (e.loaded / e.total) * 100, 'uploading');
        };
        xhr.onload = () => {
          if (xhr.status >= 200 && xhr.status < 300) {
            updateUploadItem(item, 100, 'completed');
            let data = {};
            try { data = JSON.parse(xhr.responseText); } catch (_) {}
            showToast(`上传成功：${data.filename || file.name}`, 'success');
            setTimeout(() => { item.style.opacity = '0'; setTimeout(() => item.remove(), 300); }, 3000);
            resolve();
          } else {
            updateUploadItem(item, 0, 'failed');
            let msg = xhr.responseText;
            try { msg = JSON.parse(msg).detail || msg; } catch (_) {}
            showToast(`上传失败：${file.name} - ${msg}`, 'error', 5000);
            reject(new Error(msg));
          }
        };
        xhr.onerror = () => {
          updateUploadItem(item, 0, 'failed');
          showToast(`上传失败：${file.name} - 网络错误`, 'error', 5000);
          reject(new Error('network'));
        };
        const fd = new FormData();
        fd.append('file', file);
        xhr.send(fd);
      });
    }
    async function handleFiles(files) {
      const exts = (state.settings?.supported_extensions) || ['.mp4', '.mov', '.mkv', '.avi'];
      const max = state.settings?.max_upload_size;
      const list = Array.from(files);
      const valid = [];
      for (const f of list) {
        const suffix = '.' + f.name.split('.').pop().toLowerCase();
        if (!exts.includes(suffix)) {
          showToast(`跳过不支持的文件：${f.name}`, 'error', 4000);
          continue;
        }
        if (max && f.size > max) {
          showToast(`跳过过大文件：${f.name}（${formatBytes(f.size)}）`, 'error', 4000);
          continue;
        }
        valid.push(f);
      }
      for (const f of valid) {
        try { await uploadOne(f); } catch (_) {}
      }
      if (valid.length) setTimeout(() => { loadTasks(); loadInputFiles(); }, 1000);
    }
  
    // ============ 主题 ============
    function applyTheme(theme) {
      document.documentElement.setAttribute('data-theme', theme);
      els.themeBtn.textContent = theme === 'dark' ? '☀️' : '🌙';
      els.themeBtn.title = theme === 'dark' ? '切换到亮色' : '切换到暗色';
      localStorage.setItem('theme', theme);
    }
    function toggleTheme() {
      const cur = document.documentElement.getAttribute('data-theme') || 'light';
      applyTheme(cur === 'dark' ? 'light' : 'dark');
    }
  
    // ============ 简洁 / 详细视图切换 ============
    function applyTaskView() {
      const isCompact = state.view.tasks === 'compact';
      els.tasksTable.classList.toggle('compact', isCompact);
      els.taskViewToggle.textContent = isCompact ? '详细' : '简洁';
      els.taskViewToggle.title = isCompact ? '切换到详细视图' : '切换到简洁视图';
    }
  
    // ============ 事件绑定 ============
    function bindEvents() {
      els.refreshBtn.addEventListener('click', () => {
        loadAll().then(() => showToast('数据已刷新', 'success', 1500));
      });
      els.themeBtn.addEventListener('click', toggleTheme);
  
      els.searchInput.addEventListener('input', debounce((e) => {
        state.filter.q = e.target.value.trim();
        state.page.tasks = 1;
        renderTasks();
      }, 200));
  
      els.statusFilter.addEventListener('change', (e) => {
        state.filter.status = e.target.value;
        state.page.tasks = 1;
        renderTasks();
      });
  
      els.timeFilter.addEventListener('change', (e) => {
        state.filter.timeRange = e.target.value;
        state.page.tasks = 1;
        renderTasks();
      });
  
      els.taskViewToggle.addEventListener('click', () => {
        state.view.tasks = state.view.tasks === 'compact' ? 'detailed' : 'compact';
        applyTaskView();
      });
  
      els.clearErrors.addEventListener('click', () => {
        els.errorList.innerHTML = `<li class="empty">已清空当前视图（刷新后恢复）</li>`;
      });
  
      // 上传
      els.uploadZone.addEventListener('click', (e) => {
        if (e.target.closest('a')) return;
        els.fileInput.click();
      });
      els.browseLink.addEventListener('click', (e) => {
        e.preventDefault();
        e.stopPropagation();
        els.fileInput.click();
      });
      els.fileInput.addEventListener('change', () => {
        if (els.fileInput.files.length) handleFiles(els.fileInput.files);
        els.fileInput.value = '';
      });
      els.uploadZone.addEventListener('dragover', (e) => {
        e.preventDefault();
        els.uploadZone.classList.add('dragover');
      });
      els.uploadZone.addEventListener('dragleave', (e) => {
        if (e.target === els.uploadZone) els.uploadZone.classList.remove('dragover');
      });
      els.uploadZone.addEventListener('drop', (e) => {
        e.preventDefault();
        els.uploadZone.classList.remove('dragover');
        if (e.dataTransfer.files.length) handleFiles(e.dataTransfer.files);
      });
  
      // 任务表格
      els.tasksBody.addEventListener('click', (e) => {
        const row = e.target.closest('tr[data-task-id]');
        if (!row) return;
        if (e.target.closest('a')) return;
        openDrawer(row.getAttribute('data-task-id'));
      });
      els.tasksBody.addEventListener('keydown', (e) => {
        if (e.key !== 'Enter' && e.key !== ' ') return;
        const row = e.target.closest('tr[data-task-id]');
        if (!row) return;
        e.preventDefault();
        openDrawer(row.getAttribute('data-task-id'));
      });
  
      els.drawerClose.addEventListener('click', closeDrawer);
      els.drawerBackdrop.addEventListener('click', closeDrawer);
      document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeDrawer(); });
  
      // 文件面板
      els.inputFilesRefresh.addEventListener('click', () => {
        els.inputFilesRefresh.disabled = true;
        loadInputFiles().finally(() => { els.inputFilesRefresh.disabled = false; });
      });
      els.outputFilesRefresh.addEventListener('click', () => {
        els.outputFilesRefresh.disabled = true;
        loadOutputFiles().finally(() => { els.outputFilesRefresh.disabled = false; });
      });
      els.inputFilesBody.addEventListener('click', (e) => {
        const btn = e.target.closest('[data-action="delete-input"]');
        if (!btn || btn.disabled) return;
        deleteInputFile(Number(btn.dataset.idx), btn);
      });
      els.outputFilesBody.addEventListener('click', (e) => {
        const btn = e.target.closest('[data-action="delete-output"]');
        if (!btn || btn.disabled) return;
        deleteOutputFile(Number(btn.dataset.idx), btn);
      });
  
      // 配置编辑
      els.settingsEditBtn.addEventListener('click', () => {
        if (settingsEditing) exitSettingsEdit();
        else enterSettingsEdit();
      });
    }
  
    // ============ 启动 ============
    function init() {
      applyTheme(localStorage.getItem('theme') || 'light');
      applyTaskView();
      bindEvents();
      loadAll();
      connectWS();
      setInterval(() => { loadTasks(); loadInputFiles(); }, 20000);
      document.addEventListener('visibilitychange', () => {
        if (!document.hidden) loadAll();
      });
    }
  
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', init);
    } else {
      init();
    }
  })();