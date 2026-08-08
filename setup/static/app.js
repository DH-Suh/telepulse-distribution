'use strict';

// 단계 이름 → 진행 점 개수. s-save / s-done 은 4단계를 다 채운 상태로 둔다.
const STEPS = { 's-api': 1, 's-login': 2, 's-rooms': 3, 's-key': 4, 's-save': 4, 's-done': 4 };

let PRICING = null;   // /api/state 가 채운다. 화면에 숫자를 박지 않는다
let REPO = null;

function go(name) {
  document.querySelectorAll('main > section').forEach(s =>
    s.classList.toggle('live', s.id === name));
  const n = STEPS[name];
  document.querySelectorAll('#track i').forEach((i, idx) => i.classList.toggle('on', idx < n));
  document.getElementById('stepLabel').innerHTML =
    name === 's-done' ? '설정 완료' : `<b>${n}</b> / 4 단계`;
  window.scrollTo(0, 0);
}

/**
 * JSON API 호출. 서버가 {error, kind, field} 를 주면 Error 에 통째로 붙여 던진다.
 * 문구만 넘기면 호출부가 문구를 문자열 비교로 분기하게 되고, 문구를 고치는 순간 조용히 깨진다.
 */
async function api(path, body) {
  let res;
  try {
    res = await fetch(path, body === undefined
      ? {}
      : { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body) });
  } catch (e) {
    // 서버 프로세스가 죽은 경우. 비개발자가 스스로 살릴 수 있게 명령을 알려준다.
    serverDown();
    throw new Error('설정 프로그램과 연결이 끊겼습니다.');
  }
  const data = await res.json().catch(() => ({ error: '서버 응답을 읽지 못했습니다.' }));
  if (!res.ok) {
    const err = new Error(data.error || '알 수 없는 오류가 발생했습니다.');
    err.data = data;                    // kind / field / raw 를 그대로 보존
    throw err;
  }
  return data;
}

/** 서버가 죽었을 때 덮는 안내. 살아나면 스스로 사라진다. */
function serverDown() {
  if (document.getElementById('downBanner')) return;
  const el = document.createElement('div');
  el.id = 'downBanner';
  el.className = 'band';
  el.style.cssText = 'position:fixed;left:24px;right:24px;bottom:24px;z-index:20;' +
                     'max-width:672px;margin:0 auto';
  el.innerHTML = `<h3>설정 프로그램과 연결이 끊겼습니다</h3>
    <p>아래 창(터미널)에 이 명령을 붙여넣고 Enter 를 누르면 다시 열립니다.<br>
    <b style="font-family:monospace">python setup/server.py</b><br>
    다시 켜지면 이 안내는 저절로 사라집니다. <b>여기까지 넣은 값은 다시 넣어야 합니다.</b></p>`;
  document.body.appendChild(el);
  const beat = setInterval(async () => {
    try { await fetch('/api/state'); clearInterval(beat); location.reload(); } catch (e) { /* 계속 기다린다 */ }
  }, 3000);
}

/**
 * 필드 아래 문구를 띄운다. msg 가 없으면 지운다.
 * 문구는 .field 안에 붙인다 — 인증코드 5칸처럼 입력칸이 flex 행 안에 있을 때
 * 부모에 붙이면 문구가 칸 사이로 끼어든다.
 */
function slot(input, cls) {
  const host = input.closest('.field') || input.parentElement;
  return host.querySelector('.' + cls)
    || host.appendChild(Object.assign(document.createElement('p'), { className: cls }));
}

function showError(input, msg) {
  slot(input, 'err').textContent = msg || '';
  input.classList.toggle('bad', Boolean(msg));
  if (msg) input.classList.remove('ok');
}

function showOk(input, msg) {
  showError(input, '');
  slot(input, 'okmsg').textContent = msg || '';
  input.classList.toggle('ok', Boolean(msg));
}

/** 사용자가 넣은 방 이름이 그대로 HTML 로 들어가지 않게 한다. */
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// 펼침 안내(아코디언) — 페이지 전체에서 한 번만 묶는다. ①과 ④가 같이 쓴다.
document.querySelectorAll('.exp-h').forEach(h =>
  h.addEventListener('click', () => h.parentElement.classList.toggle('open')));

// ── ① 텔레그램 API 값 ────────────────────────────────────────────────
{
  const id = document.getElementById('apiId');
  const hash = document.getElementById('apiHash');
  const next = document.getElementById('apiNext');

  // 형식은 서버가 판정한다 — validate.py 한 곳에만 규칙을 둔다.
  // 여기서는 "비어 있지 않으면 버튼을 켠다" 만 한다.
  const sync = () => { next.disabled = !(id.value.trim() && hash.value.trim()); };
  [id, hash].forEach(el => {
    el.addEventListener('input', () => { showError(el, ''); sync(); });
    el.addEventListener('keydown', e => { if (e.key === 'Enter' && !next.disabled) next.click(); });
  });

  next.addEventListener('click', async () => {
    next.disabled = true;
    try {
      await api('/api/telegram/credentials', { api_id: id.value, api_hash: hash.value });
      showOk(id, '형식 확인됨');
      showOk(hash, '형식 확인됨');
      go('s-login');
    } catch (e) {
      // 서버가 어느 칸이 틀렸는지 field 로 알려준다. 문구로 짐작하지 않는다 —
      // 두 오류 문구에 모두 '숫자'가 들어 있어 문자열 비교로는 갈라지지 않는다.
      const target = e.data?.field === 'api_hash' ? hash : id;
      showError(target, e.message);
      target.focus();
    } finally {
      sync();
    }
  });
}

// ── ② 텔레그램 로그인 ────────────────────────────────────────────────
{
  const phone = document.getElementById('phone');
  const codes = [...document.querySelectorAll('#loginCode .code input')];
  const pwField = document.getElementById('pwField');
  const pw = document.getElementById('pw');
  const sendBtn = document.getElementById('sendCode');
  const signBtn = document.getElementById('signIn');
  const note = document.getElementById('codeNote');

  const codeValue = () => codes.map(c => c.value).join('');
  const showPanel = which => {
    document.getElementById('loginPhone').hidden = which !== 'phone';
    document.getElementById('loginCode').hidden = which !== 'code';
  };

  // 한 칸 채우면 다음 칸으로, 지우면 앞 칸으로. 붙여넣기는 통째로 나눠 담는다.
  codes.forEach((c, i) => {
    c.addEventListener('input', () => {
      showError(codes[0], '');
      if (c.value && i < codes.length - 1) codes[i + 1].focus();
    });
    c.addEventListener('keydown', e => {
      if (e.key === 'Backspace' && !c.value && i > 0) codes[i - 1].focus();
      if (e.key === 'Enter' && codeValue().length === codes.length) signBtn.click();
    });
    c.addEventListener('paste', e => {
      const digits = (e.clipboardData.getData('text').match(/\d/g) || []);
      if (!digits.length) return;
      e.preventDefault();
      codes.forEach((x, j) => { x.value = digits[j] || ''; });
      codes[Math.min(digits.length, codes.length) - 1].focus();
    });
  });

  phone.addEventListener('input', () => showError(phone, ''));
  phone.addEventListener('keydown', e => { if (e.key === 'Enter') sendBtn.click(); });
  pw.addEventListener('keydown', e => { if (e.key === 'Enter') signBtn.click(); });

  document.getElementById('loginBack').addEventListener('click', () => go('s-api'));

  sendBtn.addEventListener('click', async () => {
    sendBtn.disabled = true;
    sendBtn.textContent = '코드를 보내는 중…';
    try {
      await api('/api/telegram/send-code', { phone: phone.value });
      showPanel('code');
      note.textContent = '코드는 잠시 뒤 만료됩니다';
      codes.forEach(c => { c.value = ''; });
      codes[0].focus();
    } catch (e) {
      // 열쇠가 틀린 것은 ①의 문제인데 사용자는 ② 화면에 있다.
      // 텔레그램은 전화번호보다 열쇠를 먼저 검사하므로 번호가 맞아도 여기서 걸린다.
      // "1단계로 돌아가세요"라고 적어두고 가만히 있으면 갇힌다. 직접 데려다준다.
      if (e.data?.kind === 'bad_api') {
        go('s-api');
        const id = document.getElementById('apiId');
        showError(id, e.message);
        showError(document.getElementById('apiHash'), '');
        id.focus();
        id.select();
      } else {
        showError(phone, e.message);
      }
    } finally {
      sendBtn.disabled = false;
      sendBtn.textContent = '인증코드 받기';
    }
  });

  document.getElementById('editPhone').addEventListener('click', () => showPanel('phone'));

  document.getElementById('resend').addEventListener('click', async e => {
    e.target.disabled = true;
    try {
      await api('/api/telegram/resend', {});
      codes.forEach(c => { c.value = ''; });
      showError(codes[0], '');
      note.textContent = '새 코드를 보냈습니다';
      codes[0].focus();
    } catch (err) {
      showError(codes[0], err.message);
    } finally {
      e.target.disabled = false;
    }
  });

  signBtn.addEventListener('click', async () => {
    signBtn.disabled = true;
    signBtn.textContent = '확인 중…';
    try {
      await api('/api/telegram/sign-in', { code: codeValue(), password: pw.value });
      go('s-rooms');
      // loadRooms 는 Task 8 에서 만든다. 없을 때 그냥 부르면 여기서 예외가 나고
      // 아래 catch 에 걸려 "로그인 성공인데 오류 문구가 뜨는" 상태가 된다.
      if (typeof loadRooms === 'function') loadRooms();
    } catch (e) {
      // kind 로 분기한다. 문구로 판정하면 문구를 고치는 순간 조용히 깨진다.
      if (e.data?.kind === 'needs_2fa') {
        pwField.hidden = false;
        pw.focus();
        showError(codes[0], '');
        note.textContent = '2단계 인증 비밀번호를 넣고 다시 확인을 누르세요';
      } else {
        showError(codes[0], e.message);
      }
    } finally {
      signBtn.disabled = false;
      signBtn.textContent = '확인';
    }
  });
}

// ── ③ 방 선택 ────────────────────────────────────────────────────────
let ROOMS = [];          // 서버가 준 원본
let FIXED_KRW = 0;       // 방을 빼도 안 줄어드는 출력 고정비
let STATE_RESCAN = false;
// 방 체크만 고치러 들어온 상태. 이때는 ④(OpenAI 키)를 건너뛴다 — 키는 이미
// 금고에 있고, 방 목록만 바꾸는 데 다시 받을 이유가 없다.
let ROOMS_ONLY = false;
const PICKED = new Set();

const won = v => Math.round(v / 100) * 100;                 // 100원 단위로 보여준다
const fmt = v => won(v).toLocaleString('ko-KR');

/** 글자 수는 자릿수가 커서 그대로 쓰면 표가 안 읽힌다. 1만 넘으면 '만' 단위로 줄인다. */
function fmtChars(n) {
  n = n || 0;
  if (n >= 10000) return (n / 10000).toFixed(1).replace(/\.0$/, '') + '만자';
  return n.toLocaleString('ko-KR') + '자';
}

const CHECK_SVG = `<svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="#fff"
  stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 6.4l2.6 2.6L10 3.6"/></svg>`;

async function loadRooms() {
  const list = document.getElementById('roomList');
  list.innerHTML = '<div class="more">방 목록을 불러오는 중입니다…<br>' +
    '방마다 어제 글을 세느라 1~3분 걸립니다. 창을 닫지 마세요.</div>';
  try {
    const data = await api('/api/rooms');
    ROOMS = data.rooms;
    FIXED_KRW = data.fixed_krw;
    // 기본은 전부 꺼짐. 재방문이라 이미 켜둔 방이 있으면 그것만 살린다.
    // 자동 추천을 하지 않는다 — 방 품질 자동 판정은 이미 시도하고 폐기한 지표다.
    PICKED.clear();
    ROOMS.filter(r => r.enabled === true).forEach(r => PICKED.add(r.id));
    document.getElementById('roomTotal').textContent = ROOMS.length;
    document.getElementById('rescan').hidden = !STATE_RESCAN;
    renderRooms();
  } catch (e) {
    list.innerHTML = `<div class="more">${escapeHtml(e.message)}</div>`;
  }
}

function renderRooms() {
  const q = document.getElementById('roomSearch').value.trim().toLowerCase();
  const sort = document.getElementById('roomSort').value;
  const shown = ROOMS
    .filter(r => !q || r.name.toLowerCase().includes(q))
    .sort((a, b) => sort === 'name' ? a.name.localeCompare(b.name, 'ko')
                  : sort === 'cost' ? b.krw - a.krw
                  : (b.msgs_24h || 0) - (a.msgs_24h || 0));

  document.getElementById('roomList').innerHTML = shown.map(r => `
    <div class="row ${PICKED.has(r.id) ? 'on' : ''}" data-id="${r.id}">
      <span class="box">${CHECK_SVG}</span>
      <div class="name"><b>${escapeHtml(r.name)}${
        r.chatty ? '<span class="tag">대화 위주</span>' : ''}${
        r.is_new ? '<span class="tag new">새 방</span>' : ''}</b>
        <small>${r.type === 'channel' ? '채널' : '그룹'}${
          r.members ? ' · ' + r.members.toLocaleString('ko-KR') + '명' : ''}</small></div>
      <span class="msg num">${r.msgs_24h ?? 0}건</span>
      <span class="chars num">${fmtChars(r.chars_24h)}</span>
      <span class="cost num">${fmt(r.krw)}원</span>
    </div>`).join('') || '<div class="more">검색 결과가 없습니다</div>';

  document.querySelectorAll('#roomList .row').forEach(row =>
    row.addEventListener('click', () => {
      const id = Number(row.dataset.id);
      if (PICKED.has(id)) PICKED.delete(id); else PICKED.add(id);
      row.classList.toggle('on');
      renderBand();
    }));
  renderBand();
}

function renderBand() {
  const picked = ROOMS.filter(r => PICKED.has(r.id));
  const msgs = picked.reduce((s, r) => s + (r.msgs_24h || 0), 0);
  const chars = picked.reduce((s, r) => s + (r.chars_24h || 0), 0);
  // 고정비는 한 방이라도 켰을 때만 더한다. 0개면 0원이어야 말이 된다.
  const total = picked.reduce((s, r) => s + r.krw, 0) + (picked.length ? FIXED_KRW : 0);
  document.getElementById('selCount').textContent = picked.length;
  document.getElementById('selMsgs').textContent = msgs.toLocaleString('ko-KR');
  document.getElementById('selChars').textContent = fmtChars(chars);
  document.getElementById('selCost').textContent = `${fmt(total)}원`;
  // 미터는 절대 한도가 아니라 상대 감각용. 월 5만원을 가득 찬 것으로 잡는다.
  document.getElementById('meter').style.width = Math.min(100, total / 50000 * 100) + '%';
  document.getElementById('roomsNext').disabled = picked.length === 0;
}


document.getElementById('roomSearch').addEventListener('input', renderRooms);
document.getElementById('roomSort').addEventListener('change', renderRooms);
document.getElementById('roomsBack').addEventListener('click', () => go('s-login'));

document.getElementById('roomsNext').addEventListener('click', async e => {
  e.target.disabled = true;
  try {
    await api('/api/rooms/select', { ids: [...PICKED] });
    if (ROOMS_ONLY) {
      // 방 체크만 고치러 온 경우. 키를 다시 받지 않고 바로 저장한다.
      go('s-save');
      runSave();
      return;
    }
    const total = ROOMS.filter(r => PICKED.has(r.id)).reduce((s, r) => s + r.krw, 0) + FIXED_KRW;
    document.getElementById('keyLede').innerHTML =
      '요약을 만드는 AI 사용료를 결제할 키입니다. 앞에서 고른 ' +
      `<b>${PICKED.size}개 방</b> 기준으로 <b>월 약 ${fmt(total)}원</b>이 ` +
      '본인 카드로 청구됩니다.';
    go('s-key');
  } finally {
    e.target.disabled = false;
    renderBand();
  }
});

// ── ④ OpenAI 키 ──────────────────────────────────────────────────────
{
  const key = document.getElementById('openaiKey');
  const save = document.getElementById('keySave');

  key.addEventListener('input', () => {
    showError(key, '');
    save.disabled = !key.value.trim();
  });
  key.addEventListener('keydown', e => { if (e.key === 'Enter' && !save.disabled) save.click(); });
  document.getElementById('keyBack').addEventListener('click', () => go('s-rooms'));

  save.addEventListener('click', async () => {
    save.disabled = true;
    save.textContent = '확인 중…';
    try {
      await api('/api/openai/verify', { key: key.value });
      showOk(key, '확인됨');
      go('s-save');
      // runSave 는 Task 11 에서 만든다. 없을 때 그냥 부르면 여기서 예외가 나고
      // 아래 catch 에 걸려 "확인 성공인데 오류 문구가 뜨는" 상태가 된다.
      if (typeof runSave === 'function') runSave();
    } catch (e) {
      showError(key, e.message);
      // 카드 미등록이면 발급 안내를 다시 펼친다 — 거기에 Billing 순서가 적혀 있다
      if (e.data?.kind === 'billing') {
        document.querySelector('#s-key .exp').classList.add('open');
      }
    } finally {
      save.disabled = false;
      save.textContent = '저장하고 시작하기';
    }
  });
}

// ── 저장 진행 ────────────────────────────────────────────────────────
let SAVE_TIMER = null;
let LAST_ERROR = '';

const ICONS = {
  done: `<span class="ic done"><svg width="12" height="12" viewBox="0 0 12 12" fill="none"
    stroke="#fff" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
    <path d="M2 6.4l2.6 2.6L10 3.6"/></svg></span>`,
  run:  `<span class="ic run"></span>`,
  wait: `<span class="ic wait"></span>`,
  fail: `<span class="ic fail"><svg width="12" height="12" viewBox="0 0 12 12" fill="none"
    stroke="#fff" stroke-width="2.2" stroke-linecap="round"><path d="M3 3l6 6M9 3l-6 6"/></svg></span>`,
};

async function runSave() {
  await api('/api/save/start', {});
  if (SAVE_TIMER) clearInterval(SAVE_TIMER);
  SAVE_TIMER = setInterval(pollSave, 1000);
  pollSave();
}

async function pollSave() {
  const st = await api('/api/save/status');

  document.getElementById('saveList').innerHTML = st.steps.map(s => `
    <li>${ICONS[s.state]}
      <span class="lbl" ${s.state === 'fail' ? 'style="color:var(--red);font-weight:600"' : ''}>
        ${s.label}${s.sub ? `<span class="sub">${escapeHtml(s.sub)}</span>` : ''}</span></li>`).join('');

  const dev = st.device;
  document.getElementById('deviceCard').hidden = !dev;
  if (dev) {
    document.getElementById('deviceCode').textContent = dev.code;
    document.getElementById('deviceLink').href = dev.url;
  }

  document.getElementById('saveBtns').hidden = !st.failed;
  document.getElementById('manualCard').hidden =
    !(st.failed && st.steps.some(s => s.key === 'workflow' && s.state === 'fail'));

  if (st.error) LAST_ERROR = st.error.raw || st.error.message;

  if (st.failed) {
    clearInterval(SAVE_TIMER);
    document.querySelector('#s-save h1').textContent = '저장을 끝내지 못했습니다';
    document.getElementById('saveNote').textContent =
      '다시 시도하면 끝난 항목은 건너뛰고 이어서 진행합니다';
  } else if (st.done) {
    clearInterval(SAVE_TIMER);
    // finishDone 은 Task 12 에서 만든다. 없을 때 그냥 부르면 여기서 예외가 나
    // 저장이 다 끝났는데도 화면이 멈춘 것처럼 보인다.
    if (typeof finishDone === 'function') finishDone();
  }
}

document.getElementById('retrySave').addEventListener('click', () => {
  document.querySelector('#s-save h1').textContent = '설정을 저장하고 있습니다';
  document.getElementById('saveBtns').hidden = true;
  runSave();
});

document.getElementById('copyErr').addEventListener('click', () =>
  navigator.clipboard.writeText(LAST_ERROR));

// ── 완료 · 즉시 테스트 · 재방문 ──────────────────────────────────────
function finishDone() {
  const picked = ROOMS.filter(r => PICKED.has(r.id));
  const total = picked.reduce((s, r) => s + r.krw, 0) + FIXED_KRW;
  // 정확한 시각을 약속하지 않는다 — 실측 2시간 38분 지연(2026-08-07, DISTRIBUTION 5절).
  // 08:05 을 적으면 그 시각에 안 온 사람이 고장으로 여기고 문의한다.
  document.getElementById('doneWhen').textContent = '내일 오전 중';
  document.getElementById('doneCount').textContent = `${picked.length}개`;
  document.getElementById('doneCost').textContent = `월 ${fmt(total)}원`;
  go('s-done');
}

document.getElementById('testNow').addEventListener('click', async e => {
  e.target.disabled = true;
  e.target.textContent = '실행을 시작하는 중…';
  try {
    await api('/api/dispatch', {});
    e.target.textContent = '실행 중 — 3분쯤 뒤 텔레그램을 확인하세요';
  } catch (err) {
    e.target.textContent = err.message;
    e.target.disabled = false;
  }
});

document.getElementById('testLater').addEventListener('click', () => {
  document.getElementById('testNow').disabled = true;
});

// 재방문 — 저장된 rooms.json 만으로 체크를 고친다. 텔레그램 로그인이 필요 없다.
document.getElementById('editRooms').addEventListener('click', async () => {
  const data = await api('/api/rooms/saved');
  ROOMS = data.rooms;
  FIXED_KRW = data.fixed_krw;
  PICKED.clear();
  ROOMS.filter(r => r.enabled === true).forEach(r => PICKED.add(r.id));
  document.getElementById('roomTotal').textContent = ROOMS.length;
  STATE_RESCAN = true;
  ROOMS_ONLY = true;                                   // ④ 를 건너뛴다
  document.getElementById('rescan').hidden = false;    // 여기서만 노출
  renderRooms();
  go('s-rooms');
});

// 새 방 찾기 — 이때만 ②의 로그인 흐름을 다시 탄다.
// ① 부터 다시 시작하는 이유: GitHub Secrets 는 쓰기 전용이라 저장해 둔 API 값과
// 세션 문자열을 되읽을 수 없다. 편의를 위해 어딘가에 캐시하지 않는다.
// 여기서는 ROOMS_ONLY 를 끈다. ① 부터 다시 걷는 길이라 텔레그램 값도 키도 새로 받는다 —
// 키를 바꾸고 싶을 때 쓸 수 있는 유일한 경로이기도 하다.
document.getElementById('rescan').addEventListener('click', () => {
  STATE_RESCAN = true;
  ROOMS_ONLY = false;
  go('s-api');
});

(async function init() {
  const st = await api('/api/state');
  PRICING = st.pricing;
  REPO = st.repo;
  if (st.has_saved_rooms) {                 // 재방문
    document.querySelector('#s-done h1').textContent = '이미 설정돼 있습니다';
    document.getElementById('doneWhen').textContent = '매일 오전 중';
    document.getElementById('doneCount').textContent = `${st.saved_room_count}개`;
    document.getElementById('doneCost').textContent = '—';
    // '지금 한 번 만들어보기'는 방금 저장을 마친 사람에게만 의미가 있다.
    // 재방문 때는 GitHub 토큰이 메모리에 없어 반드시 실패한다 — 800원을 예고해 놓고
    // 눌렀더니 오류가 뜨는 것이 가장 나쁘다. 아예 내보이지 않는다.
    document.getElementById('testBand').hidden = true;
    go('s-done');
  }
})();
