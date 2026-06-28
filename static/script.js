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
      pollStatus();
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
      setTimeout(() => location.reload(), 1000);
    } else {
      status.textContent = `Ошибка: ${data.error}`;
    }
  } catch (e) {
    status.textContent = `Ошибка: ${e.message}`;
  }
}

// опрашиваем статус пока парсер работает, потом перезагружаем страницу
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

// если парсер уже работал когда открыли страницу - тоже запускаем polling
if (document.querySelector('.running-indicator')) {
  // небольшая задержка чтобы не конфликтовать с начальной загрузкой
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

  const status = document.getElementById('danger-status');
  status.className = 'run-status';

  try {
    const resp = await fetch(`/clear/${type}`, { method: 'POST' });
    const data = await resp.json();
    if (data.status === 'ok') {
      status.textContent = `${labels[type][0].toUpperCase() + labels[type].slice(1)} очищен${type === 'log' ? '' : 'а'}.`;
    } else {
      status.textContent = `Ошибка: ${data.error}`;
      status.classList.add('status-error');
    }
  } catch (e) {
    status.textContent = `Ошибка: ${e.message}`;
    status.classList.add('status-error');
  }
}

// подсветка строк лога
function colorizeLog(lines) {
  return lines.map(line => {
    const escaped = line.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    if (escaped.includes('[ERROR]') || escaped.includes('ошибка'))
      return `<span class="line-error">${escaped}</span>`;
    if (escaped.includes('[WARNING]') || escaped.includes('таймаут'))
      return `<span class="line-warning">${escaped}</span>`;
    if (escaped.includes('НОВАЯ') || escaped.includes('ОБНОВИЛАСЬ'))
      return `<span class="line-new">${escaped}</span>`;
    return `<span class="line-info">${escaped}</span>`;
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