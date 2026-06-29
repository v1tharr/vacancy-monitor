// запуск парсера
async function runParser() {
  const status = document.getElementById('run-status');
  status.className = 'run-status';
  status.textContent = 'Запускаю проверку...';

  try {
    const resp = await fetch('/run', { method: 'POST' });
    const data = await resp.json();
    if (data.status === 'started') {
      status.textContent = 'Парсер запущен — уведомление придёт в Telegram по завершении.';
      // перезагружаем страницу чтобы появилась кнопка "Остановить"
      setTimeout(() => location.reload(), 800);
    } else {
      status.textContent = `Ошибка: ${data.error}`;
      status.classList.add('status-error');
    }
  } catch (e) {
    status.textContent = `Ошибка: ${e.message}`;
    status.classList.add('status-error');
  }
}

// остановка парсера
async function stopParser() {
  const status = document.getElementById('run-status');
  status.className = 'run-status';
  status.textContent = 'Останавливаю...';

  try {
    const resp = await fetch('/stop', { method: 'POST' });
    const data = await resp.json();
    if (data.status === 'stopped') {
      status.textContent = 'Парсер остановлен.';
      setTimeout(() => location.reload(), 800);
    } else {
      status.textContent = `Ошибка: ${data.error}`;
      status.classList.add('status-error');
    }
  } catch (e) {
    status.textContent = `Ошибка: ${e.message}`;
    status.classList.add('status-error');
  }
}

// опрашиваем статус пока парсер работает, перезагружаем когда завершился
function pollStatus() {
  const interval = setInterval(async () => {
    try {
      const resp = await fetch('/status');
      const data = await resp.json();
      if (!data.running) {
        clearInterval(interval);
        setTimeout(() => location.reload(), 500);
      }
    } catch (e) {
      clearInterval(interval);
    }
  }, 2000);
}

// если парсер работал когда открыли страницу - запускаем polling
if (document.querySelector('.running-indicator')) {
  setTimeout(pollStatus, 1000);
}

// вкладки
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById(`tab-${btn.dataset.tab}`).classList.add('active');
  });
});

// очистка истории / лога
async function clearData(type) {
  const labels = { state: 'историю вакансий', log: 'лог' };
  if (!confirm(`Очистить ${labels[type]}? Это действие нельзя отменить.`)) return;

  const statusEl = document.getElementById('danger-status');
  statusEl.className = 'run-status';
  // отступ сверху чтобы не прилипало к кнопкам
  statusEl.style.marginTop = '20px';

  try {
    const resp = await fetch(`/clear/${type}`, { method: 'POST' });
    const data = await resp.json();
    if (data.status === 'ok') {
      const pastTense = type === 'log' ? 'Лог очищен.' : 'История вакансий очищена.';
      statusEl.textContent = pastTense;
    } else {
      statusEl.textContent = `Ошибка: ${data.error}`;
      statusEl.classList.add('status-error');
    }
  } catch (e) {
    statusEl.textContent = `Ошибка: ${e.message}`;
    statusEl.classList.add('status-error');
  }
}

// подсветка строк лога
function colorizeLog(lines) {
  return lines.map(line => {
    const esc = line
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
    if (esc.includes('[ERROR]') || esc.includes('ошибка'))
      return `<span class="line-error">${esc}</span>`;
    if (esc.includes('[WARNING]') || esc.includes('таймаут'))
      return `<span class="line-warning">${esc}</span>`;
    if (esc.includes('НОВАЯ') || esc.includes('ОБНОВИЛАСЬ'))
      return `<span class="line-new">${esc}</span>`;
    return `<span class="line-info">${esc}</span>`;
  }).join('\n');
}

// загрузка лога
async function refreshLog() {
  const output = document.getElementById('log-output');
  const count  = document.getElementById('lines-count');
  const scroll = document.getElementById('autoscroll');
  if (!output) return;

  try {
    const resp = await fetch(`/log/data?lines=${count ? count.value : 100}`);
    const data = await resp.json();
    output.innerHTML = colorizeLog(data.lines);
    if (scroll && scroll.checked) {
      output.parentElement.scrollTop = output.parentElement.scrollHeight;
    }
  } catch (e) {
    output.textContent = `Ошибка загрузки лога: ${e.message}`;
  }
}

if (document.getElementById('log-output')) {
  refreshLog();
  setInterval(refreshLog, 3000);
}