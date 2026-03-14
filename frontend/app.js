// ---- Config ----
const BACKEND_URL = window.BACKEND_URL || 'http://localhost:8000';
const WS_URL = BACKEND_URL.replace(/^http/, 'ws');

// ---- State ----
let state = {
  sessionId: null,
  playerId: null,
  isHost: false,
  playerName: '',
  ws: null,
  lastGameState: null,
  timerInterval: null,
  timerEndsAt: null,
  fatalError: false,
};

// ---- DOM Helpers ----
const $ = id => document.getElementById(id);
const el = (tag, cls, html) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (html !== undefined) e.innerHTML = html;
  return e;
};

function showScreen(name) {
  document.querySelectorAll('.screen').forEach(s => s.classList.remove('active'));
  $(`screen-${name}`).classList.add('active');
}

function showToast(msg, duration = 2200) {
  let t = document.querySelector('.toast');
  if (!t) { t = el('div', 'toast'); document.body.appendChild(t); }
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), duration);
}

function setError(msg) { $('entry-error').textContent = msg; }

// ---- Timer ----
function startClientTimer(seconds) {
  clearInterval(state.timerInterval);
  state.timerEndsAt = Date.now() + seconds * 1000;
  updateTimerDisplay();
  state.timerInterval = setInterval(updateTimerDisplay, 250);
}

function stopClientTimer() {
  clearInterval(state.timerInterval);
  state.timerInterval = null;
  state.timerEndsAt = null;
  const el = $('guess-timer');
  if (el) el.textContent = '';
}

function updateTimerDisplay() {
  const el = $('guess-timer');
  if (!el || !state.timerEndsAt) return;
  const remaining = Math.max(0, Math.ceil((state.timerEndsAt - Date.now()) / 1000));
  el.textContent = remaining + 's';
  el.className = 'guess-timer' + (remaining <= 10 ? ' urgent' : '');
  if (remaining === 0) stopClientTimer();
}

// ---- Entry Screen ----
document.querySelectorAll('.tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    tab.classList.add('active');
    $(`tab-${tab.dataset.tab}`).classList.add('active');
  });
});

async function rollQuestion() {
  try {
    const r = await fetch(`${BACKEND_URL}/questions/random`);
    const d = await r.json();
    $('create-question').value = d.question;
  } catch { $('create-question').placeholder = 'Could not load questions'; }
}

$('roll-question').addEventListener('click', rollQuestion);
rollQuestion();

// URL session code pre-fill
const urlParams = new URLSearchParams(window.location.search);
const sessionFromUrl = urlParams.get('session');
if (sessionFromUrl) {
  $('join-code').value = sessionFromUrl.toUpperCase();
  document.querySelector('.tab[data-tab="join"]').click();
  // Attempt to show rejoin options if session is in-progress
  checkForRejoin(sessionFromUrl.toUpperCase());
}

// ---- Rejoin flow ----
async function checkForRejoin(sessionId) {
  try {
    const r = await fetch(`${BACKEND_URL}/sessions/${sessionId}/inactive_players`);
    if (!r.ok) {
      // Session doesn't exist or other error — show a warning on the join tab
      if (r.status === 404) {
        setError('Session not found — it may have expired if the server restarted.');
      }
      return;
    }
    const d = await r.json();
    if (d.inactive_players && d.inactive_players.length > 0) {
      showRejoinOptions(sessionId, d.inactive_players);
    }
  } catch { /* ignore network errors */ }
}

function showRejoinOptions(sessionId, inactivePlayers) {
  const joinTab = $('tab-join');
  // Remove any existing rejoin box
  const existing = $('rejoin-box');
  if (existing) existing.remove();

  const box = document.createElement('div');
  box.id = 'rejoin-box';
  box.className = 'rejoin-box';
  box.innerHTML = `
    <div class="rejoin-title">👋 Reconnecting?</div>
    <p class="rejoin-subtitle">These players are disconnected. Are you one of them?</p>
    <div class="rejoin-list">
      ${inactivePlayers.map(p =>
        `<button class="rejoin-btn" data-pid="${p.id}" data-name="${escHtml(p.name)}">${escHtml(p.name)}</button>`
      ).join('')}
      <button class="rejoin-btn rejoin-new" data-pid="">I'm someone new</button>
    </div>
  `;
  joinTab.insertBefore(box, joinTab.firstChild);

  box.querySelectorAll('.rejoin-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      const pid = btn.dataset.pid;
      if (!pid) {
        // New player — remove box, proceed normally
        box.remove();
        return;
      }
      // Rejoin as this player
      setError('');
      try {
        const r = await fetch(`${BACKEND_URL}/sessions/${sessionId}/rejoin/${pid}`, { method: 'POST' });
        if (!r.ok) { setError('Could not rejoin.'); return; }
        const d = await r.json();
        state.sessionId = d.session_id;
        state.playerId = d.player_id;
        state.isHost = d.is_host;
        state.playerName = d.name;
        startGame();
      } catch { setError('Could not rejoin. Try joining as a new player.'); }
    });
  });
}

// Create session
$('btn-create').addEventListener('click', async () => {
  const name = $('create-name').value.trim();
  const question = $('create-question').value.trim();
  const hostIsPlayer = $('create-host-is-player').checked;
  const timerVal = parseInt($('create-timer').value) || 0;
  const excludeRevealed = $('create-exclude-revealed').checked;
  if (!name) { setError('Please enter your name.'); return; }
  if (!question) { setError('Please enter or roll a question.'); return; }
  setError('');
  $('btn-create').disabled = true;
  try {
    const r = await fetch(`${BACKEND_URL}/sessions`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        host_name: name, question, host_is_player: hostIsPlayer,
        guess_timer_seconds: timerVal,
        exclude_revealed_from_guessing: excludeRevealed,
      }),
    });
    const d = await r.json();
    state.sessionId = d.session_id;
    state.playerId = d.player_id;
    state.isHost = true;
    state.playerName = name;
    startGame();
  } catch {
    setError('Could not create session. Is the backend running?');
    $('btn-create').disabled = false;
  }
});

// Join session
$('btn-join').addEventListener('click', async () => {
  const code = $('join-code').value.trim().toUpperCase();
  const name = $('join-name').value.trim();
  if (!code) { setError('Please enter the session code.'); return; }
  if (!name) { setError('Please enter your name.'); return; }
  setError('');
  $('btn-join').disabled = true;
  try {
    const r = await fetch(`${BACKEND_URL}/sessions/${code}/join`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    });
    if (!r.ok) {
      let detail = 'Could not join session.';
      try { detail = (await r.json()).detail || detail; } catch {}
      if (r.status === 404) detail = 'Session not found — check the code or ask the host to share the link again.';
      setError(detail);
      $('btn-join').disabled = false;
      return;
    }
    const d = await r.json();
    state.sessionId = d.session_id;
    state.playerId = d.player_id;
    state.isHost = false;
    state.playerName = d.name; // use server-assigned name (may have #2 suffix)
    startGame();
  } catch {
    setError('Could not join session. Check the code and try again.');
    $('btn-join').disabled = false;
  }
});

// ---- WebSocket ----
function startGame() {
  state.fatalError = false;
  showScreen('game');
  $('header-session-id').textContent = state.sessionId;
  $('header-player-name').textContent = state.playerName;

  $('header-session-id').addEventListener('click', () => {
    const url = `${window.location.origin}${window.location.pathname}?session=${state.sessionId}`;
    navigator.clipboard.writeText(url).then(() => showToast('Join link copied!'));
  });

  connectWS();
}

function connectWS() {
  const url = `${WS_URL}/ws/${state.sessionId}/${state.playerId}`;
  state.ws = new WebSocket(url);
  state.ws.onopen = () => console.log('WS connected');
  state.ws.onmessage = e => handleServerMsg(JSON.parse(e.data));
  state.ws.onclose = () => {
    if (state.fatalError) return; // server sent a fatal error, don't reconnect
    console.log('WS closed, reconnecting in 2s...');
    setTimeout(connectWS, 2000);
  };
  state.ws.onerror = err => console.error('WS error', err);
}

function send(obj) {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    state.ws.send(JSON.stringify(obj));
  }
}

function handleServerMsg(msg) {
  if (msg.type === 'state') {
    state.lastGameState = msg;
    // If we're leaving guessing, stop timer
    if (msg.state !== 'guessing') stopClientTimer();
    renderGameState(msg);
  } else if (msg.type === 'timer_start') {
    startClientTimer(msg.seconds);
  } else if (msg.type === 'all_submitted') {
    showToast('Everyone has submitted! 🎉');
  } else if (msg.type === 'error') {
    // Fatal server error (e.g. player not found after server restart)
    // Stop reconnect loop and show the error on screen
    state.fatalError = true;
    showScreen('entry');
    setError(msg.message || 'Connection error. Please rejoin.');
  }
}

// ---- Render Game State ----
function renderGameState(gs) {
  $('header-question').textContent = `"${gs.question}"`;
  const main = $('game-main');
  if (gs.state !== 'answering') main.dataset.phase = '';

  switch (gs.state) {
    case 'lobby':     renderLobby(main, gs);     break;
    case 'answering': renderAnswering(main, gs);  break;
    case 'reveal':    renderReveal(main, gs);     break;
    case 'guessing':  renderGuessing(main, gs);   break;
    case 'guessed':   renderGuessed(main, gs);    break;
    case 'revealed':  renderRevealed(main, gs);   break;
    case 'stats':     renderStats(main, gs);      break;
    default: main.innerHTML = `<p>Unknown state: ${gs.state}</p>`;
  }
}

// ---- LOBBY ----
function renderLobby(main, gs) {
  const joinUrl = `${window.location.origin}${window.location.pathname}?session=${gs.session_id}`;
  const isHost = gs.host_id === state.playerId;

  let html = `
    <h2 class="phase-title">Waiting for players</h2>
    <p class="phase-subtitle">Share the link below. The host starts the game when everyone is in.</p>
    <div class="share-box">
      <div>
        <div class="share-text">Invite link</div>
        <div class="share-link">${joinUrl}</div>
      </div>
      <button class="btn-copy" onclick="navigator.clipboard.writeText('${joinUrl}').then(()=>showToast('Copied!'))">Copy</button>
    </div>
  `;

  if (isHost) {
    html += `
      <div class="host-question-edit">
        <label>Question for this session</label>
        <div class="host-q-row" style="margin-top:0.5rem">
          <input id="host-q-input" type="text" value="${escHtml(gs.question)}" maxlength="200" />
          <button class="btn-icon" id="host-roll-q" title="Roll new question">⟳</button>
          <button class="btn-secondary" id="host-save-q">Set</button>
        </div>
      </div>
      <div class="host-settings">
        <div class="settings-row">
          <label class="settings-label">Guess timer (seconds, 0 = no timer)</label>
          <input id="host-timer" type="number" min="0" max="300" value="${gs.guess_timer_seconds}" style="width:70px;text-align:center" />
        </div>
        <div class="settings-row">
          <label class="settings-label">Exclude revealed players from future guesses</label>
          <label class="switch">
            <input id="host-exclude-revealed" type="checkbox" ${!!gs.exclude_revealed_from_guessing ? 'checked' : ''} />
            <span class="slider"></span>
          </label>
        </div>
      </div>
    `;
  }

  html += `<h3 style="font-size:0.78rem;text-transform:uppercase;letter-spacing:0.08em;color:var(--text-dim);margin-bottom:0.75rem;">Players (${gs.players.length})</h3>`;
  html += `<div class="player-grid">`;
  for (const p of gs.players) {
    const youBadge = p.id === state.playerId ? ` <span class="badge badge-you">You</span>` : '';
    const hostBadge = p.is_host ? ` <span class="badge badge-host">Host</span>` : '';
    html += `<div class="player-chip ${p.is_host ? 'is-host' : ''}"><span class="dot"></span>${escHtml(p.name)}${youBadge}${hostBadge}</div>`;
  }
  html += `</div>`;

  if (isHost) {
    const canStart = gs.players.length >= 2;
    html += `
      <div class="action-row">
        <button class="btn-action" id="btn-start" ${canStart ? '' : 'disabled'}>Start game →</button>
        ${!canStart ? '<span style="font-size:0.8rem;color:var(--text-muted)">Need at least 2 players</span>' : ''}
      </div>
    `;
  } else {
    html += `<p style="color:var(--text-dim);font-size:0.88rem;">Waiting for the host to start…</p>`;
  }

  main.innerHTML = html;

  if (isHost) {
    $('btn-start')?.addEventListener('click', () => send({ action: 'start_answering' }));

    $('host-save-q')?.addEventListener('click', () => {
      const q = $('host-q-input').value.trim();
      if (q) send({ action: 'update_question', question: q });
    });
    $('host-roll-q')?.addEventListener('click', async () => {
      const r = await fetch(`${BACKEND_URL}/questions/random`);
      const d = await r.json();
      $('host-q-input').value = d.question;
      send({ action: 'update_question', question: d.question });
    });

    const sendSettings = () => send({
      action: 'update_settings',
      guess_timer_seconds: parseInt($('host-timer').value) || 0,
      exclude_revealed_from_guessing: $('host-exclude-revealed').checked,
    });
    $('host-timer')?.addEventListener('change', sendSettings);
    $('host-exclude-revealed')?.addEventListener('change', sendSettings);
  }
}

// ---- ANSWERING ----
function renderAnswering(main, gs) {
  const isHost = gs.host_id === state.playerId;
  const allPlayers = gs.players;
  const totalAnswering = allPlayers.length;
  const submittedCount = allPlayers.filter(p => p.submitted).length;
  const allSubmitted = submittedCount === totalAnswering;
  const alreadyRendered = main.dataset.phase === 'answering';

  if (!alreadyRendered) {
    main.dataset.phase = 'answering';

    let html = `
      <h2 class="phase-title">Submit your answer</h2>
      <p class="phase-subtitle" id="answer-subtitle">${submittedCount} of ${totalAnswering} submitted</p>
    `;

    if (!gs.i_submitted) {
      html += `
        <div class="answer-box" id="answer-input-section">
          <label style="text-transform:none;font-size:1rem;font-weight:700;color:var(--text);letter-spacing:0">"${escHtml(gs.question)}"</label>
          <textarea id="answer-input" placeholder="Write your answer here…" maxlength="400"></textarea>
          <div class="action-row" style="margin-top:0">
            <button class="btn-action" id="btn-submit-answer">Submit answer</button>
          </div>
        </div>
      `;
    } else {
      html += buildSubmittedSection(gs);
    }

    html += `<div class="waiting-bar"><h3>Submissions</h3><div class="progress-list" id="answer-progress"></div></div>`;
    if (isHost) {
      html += `<hr class="divider" /><div class="action-row" id="host-answer-actions"></div>`;
    }

    main.innerHTML = html;
    attachAnswerListeners(gs);
  }

  // Always update dynamic parts without touching textarea
  const subtitle = $('answer-subtitle');
  if (subtitle) subtitle.textContent = `${submittedCount} of ${totalAnswering} submitted`;

  // Swap textarea → submitted message when player's answer just went through
  if (gs.i_submitted && $('answer-input-section') && !$('answer-editing-section')) {
    $('answer-input-section').outerHTML = buildSubmittedSection(gs);
    attachSubmittedListeners(gs);
  }
  // Swap editing section → submitted message after a re-submit
  if (gs.i_submitted && $('answer-editing-section')) {
    $('answer-editing-section').outerHTML = buildSubmittedSection(gs);
    attachSubmittedListeners(gs);
  }

  const progressEl = $('answer-progress');
  if (progressEl) {
    progressEl.innerHTML = allPlayers.map(p => {
      const youTag = p.id === state.playerId ? ' <span class="badge badge-you">You</span>' : '';
      return `<div class="progress-item"><div class="check ${p.submitted ? 'done' : ''}">✓</div><span>${escHtml(p.name)}${youTag}</span></div>`;
    }).join('');
  }

  const hostActions = $('host-answer-actions');
  if (isHost && hostActions) {
    hostActions.innerHTML = `
      <button class="btn-action" id="btn-reveal-answers" ${allSubmitted ? '' : 'disabled'}>Reveal all answers</button>
      ${!allSubmitted ? `<button class="btn-secondary" id="btn-force-reveal">Force reveal (${submittedCount}/${totalAnswering})</button>` : ''}
    `;
    $('btn-reveal-answers')?.addEventListener('click', () => send({ action: 'reveal_answers' }));
    $('btn-force-reveal')?.addEventListener('click', () => send({ action: 'reveal_answers' }));
  }
}

function buildSubmittedSection(gs) {
  return `
    <div id="answer-submitted-section" style="margin-bottom:1.5rem">
      <span class="submitted-badge">✓ Answer submitted</span>
      <p style="margin-top:0.75rem;color:var(--text-dim);font-size:0.88rem;">Waiting for everyone else…</p>
      <button class="btn-secondary" id="btn-change-answer" style="margin-top:0.75rem">Edit my answer</button>
    </div>
  `;
}

function buildEditSection(gs) {
  const currentText = gs.my_answer || state.lastGameState?.my_answer || '';
  return `
    <div id="answer-editing-section" class="answer-box" style="margin-bottom:1.5rem">
      <label style="text-transform:none;font-size:1rem;font-weight:700;color:var(--text);letter-spacing:0">"${escHtml(gs.question)}"</label>
      <textarea id="answer-input" maxlength="400">${escHtml(currentText)}</textarea>
      <div class="action-row" style="margin-top:0">
        <button class="btn-action" id="btn-submit-answer">Update answer</button>
        <button class="btn-secondary" id="btn-cancel-edit">Cancel</button>
      </div>
    </div>
  `;
}

function attachSubmitListener() {
  $('btn-submit-answer')?.addEventListener('click', () => {
    const text = $('answer-input')?.value.trim();
    if (!text) return;
    $('btn-submit-answer').disabled = true;
    $('btn-submit-answer').textContent = 'Submitting…';
    send({ action: 'submit_answer', text });
  });
}

function attachAnswerListeners(gs) {
  attachSubmitListener();
}

function attachSubmittedListeners(gs) {
  $('btn-change-answer')?.addEventListener('click', () => {
    const section = $('answer-submitted-section');
    if (!section) return;
    section.outerHTML = buildEditSection(gs);
    attachSubmitListener();
    $('btn-cancel-edit')?.addEventListener('click', () => {
      const editSection = $('answer-editing-section');
      if (editSection) {
        editSection.outerHTML = buildSubmittedSection(gs);
        attachSubmittedListeners(gs);
      }
    });
  });
}


// ---- REVEAL ----
function renderReveal(main, gs) {
  const isHost = gs.host_id === state.playerId;

  let html = `
    <h2 class="phase-title">All answers in!</h2>
    <p class="phase-subtitle">Read through these — then we'll guess who wrote what.</p>
    <div class="answers-grid">
  `;

  gs.answers.forEach((a, i) => {
    html += `
      <div class="answer-card" style="animation-delay:${i * 0.08}s">
        <div class="card-num">#${i + 1}</div>
        ${escHtml(a.text)}
      </div>
    `;
  });

  html += `</div>`;

  if (isHost) {
    html += `<div class="action-row"><button class="btn-action" id="btn-start-guessing">Start guessing →</button></div>`;
  } else {
    html += `<p style="color:var(--text-dim);font-size:0.88rem;">Waiting for the host to start the guessing round…</p>`;
  }

  main.innerHTML = html;
  $('btn-start-guessing')?.addEventListener('click', () => send({ action: 'start_guessing' }));
}

// ---- GUESSING ----
function renderGuessing(main, gs) {
  const isHost = gs.host_id === state.playerId;
  const iAmAuthor = gs.i_am_author;
  const iGuessed = gs.i_guessed;
  const eligibleGuessers = gs.eligible_guessers || [];
  const guessedSoFar = gs.guessed_so_far || [];
  const pendingCount = eligibleGuessers.length - guessedSoFar.length;
  const sidebar = gs.answer_sidebar || [];

  let html = `<div class="guessing-layout">`;

  // ---- Left: answer sidebar ----
  html += `<div class="answer-sidebar">
    <div class="sidebar-title">Answers</div>`;
  sidebar.forEach((a, i) => {
    html += `<div class="sidebar-answer ${a.status}">${escHtml(a.text)}</div>`;
  });
  html += `</div>`;

  // ---- Right: guessing panel ----
  html += `<div class="guessing-panel">`;

  html += `
    <div class="guess-header-row">
      <div>
        <h2 class="phase-title" style="margin-bottom:0.1rem">Who wrote this?</h2>
        <p class="phase-subtitle" style="margin-bottom:0">Answer ${gs.answer_index + 1} of ${gs.total_answers}</p>
      </div>
      <div id="guess-timer" class="guess-timer"></div>
    </div>
    <div class="guess-stage">
      <div class="guess-answer-text">"${escHtml(gs.current_answer.text)}"</div>
  `;

  if (iAmAuthor) {
    html += `<div class="author-notice">✦ This is your answer — sit tight while others guess!</div>`;
  } else if (iGuessed) {
    html += `<div class="submitted-badge">✓ Guess submitted</div>
             <p style="margin-top:0.75rem;color:var(--text-dim);font-size:0.88rem;">${pendingCount} guess${pendingCount !== 1 ? 'es' : ''} remaining</p>`;
  } else {
    html += `<div class="guess-prompt">Pick the author</div><div class="player-guess-grid">`;
    for (const p of gs.players) {
      const unvotable = p.votable === false;
      html += `<button class="guess-btn${unvotable ? ' unvotable' : ''}" data-pid="${p.id}" ${unvotable ? 'disabled title="Already revealed"' : ''}>${escHtml(p.name)}</button>`;
    }
    html += `</div>`;
  }

  // Static player list (no ticks — hides author identity)
  html += `<div class="guesser-waiting">
    <div style="font-size:0.75rem;text-transform:uppercase;letter-spacing:0.08em;color:var(--text-muted);margin-bottom:0.5rem;">
      ${pendingCount > 0 ? `${pendingCount} guess${pendingCount !== 1 ? 'es' : ''} remaining` : 'All guesses in'}
    </div>`;
  for (const p of gs.players) {
    const youTag = p.id === state.playerId ? ' <span class="badge badge-you">You</span>' : '';
    html += `<div class="guesser-row"><div style="width:7px;height:7px;border-radius:50%;background:var(--text-muted);flex-shrink:0"></div>${escHtml(p.name)}${youTag}</div>`;
  }
  html += `</div>`;

  html += `</div>`; // close guess-stage

  if (isHost) {
    html += `<div class="action-row"><button class="btn-secondary" id="btn-force-guessing">Force advance</button></div>`;
  }

  html += `</div>`; // close guessing-panel
  html += `</div>`; // close guessing-layout

  main.innerHTML = html;

  // Re-sync timer: if we already have an end time, just update the display.
  // Fallback: if the state carries timer seconds and we have no active timer
  // (e.g. fresh page load / reconnect where timer_start was missed), start one.
  if (state.timerEndsAt) {
    updateTimerDisplay();
  } else if (gs.guess_timer_seconds > 0) {
    // timer_start event may not have arrived yet or was missed — start from state
    startClientTimer(gs.guess_timer_seconds);
  }

  document.querySelectorAll('.guess-btn:not(.unvotable)').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.guess-btn').forEach(b => b.classList.remove('selected'));
      btn.classList.add('selected');
      send({ action: 'submit_guess', guessed_player_id: btn.dataset.pid });
      document.querySelectorAll('.guess-btn').forEach(b => b.disabled = true);
    });
  });

  $('btn-force-guessing')?.addEventListener('click', () => send({ action: 'force_advance_guessing' }));
}

// ---- GUESSED ----
function renderGuessed(main, gs) {
  const isHost = gs.host_id === state.playerId;
  const dist = gs.guess_distribution || [];
  const sidebar = gs.answer_sidebar || [];

  let html = `<div class="guessing-layout">`;

  html += `<div class="answer-sidebar">
    <div class="sidebar-title">Answers</div>`;
  sidebar.forEach(a => {
    html += `<div class="sidebar-answer ${a.status}">${escHtml(a.text)}</div>`;
  });
  html += `</div>`;

  html += `<div class="guessing-panel">
    <h2 class="phase-title">Guesses are in!</h2>
    <p class="phase-subtitle">Here's the vote tally — host will reveal who wrote it.</p>
    <div style="margin-bottom:1rem;font-size:0.88rem;color:var(--text-dim)">Answer: <strong style="color:var(--text)">"${escHtml(gs.current_answer.text)}"</strong></div>
    <div class="distribution-grid">`;

  for (const d of [...dist].sort((a, b) => b.count - a.count)) {
    html += `
      <div class="dist-card">
        <div class="dist-name">${escHtml(d.name)}</div>
        <div class="dist-guessers" style="color:var(--text-muted);font-style:italic">votes</div>
        <div class="dist-count">${d.count}</div>
      </div>`;
  }

  html += `</div>`;

  if (isHost) {
    html += `<div class="action-row"><button class="btn-action" id="btn-reveal-author">Reveal author →</button></div>`;
  } else {
    html += `<p style="color:var(--text-dim);font-size:0.88rem;">Waiting for the host to reveal the author…</p>`;
  }

  html += `</div></div>`;

  main.innerHTML = html;
  $('btn-reveal-author')?.addEventListener('click', () => send({ action: 'reveal_author' }));
}

// ---- REVEALED ----
function renderRevealed(main, gs) {
  const isHost = gs.host_id === state.playerId;
  const author = gs.true_author;
  const dist = gs.guess_distribution || [];
  const isLastAnswer = gs.answer_index >= gs.total_answers - 1;
  const guesserResults = gs.guesser_results || {};
  const myResult = guesserResults[state.playerId];
  const sidebar = gs.answer_sidebar || [];

  let myResultBanner = '';
  if (myResult === true) {
    myResultBanner = `<div class="result-banner result-correct">✓ You guessed correctly!</div>`;
  } else if (myResult === false) {
    myResultBanner = `<div class="result-banner result-wrong">✗ Better luck next time!</div>`;
  } else if (author && author.id === state.playerId) {
    myResultBanner = `<div class="result-banner result-author">✦ This was your answer</div>`;
  }

  let html = `<div class="guessing-layout">`;

  html += `<div class="answer-sidebar">
    <div class="sidebar-title">Answers</div>`;
  sidebar.forEach(a => {
    html += `<div class="sidebar-answer ${a.status}">${escHtml(a.text)}</div>`;
  });
  html += `</div>`;

  html += `<div class="guessing-panel">
    <h2 class="phase-title">Author revealed!</h2>
    <p class="phase-subtitle">Answer ${gs.answer_index + 1} of ${gs.total_answers}</p>
    <div style="margin-bottom:1rem;font-size:0.88rem;color:var(--text-dim)">Answer: <strong style="color:var(--text)">"${escHtml(gs.current_answer.text)}"</strong></div>
    <div class="author-reveal-box">
      <div class="author-reveal-icon">✦</div>
      <div class="author-reveal-text">
        <h3>${escHtml(author?.name || '?')}</h3>
        <p>wrote this answer</p>
      </div>
    </div>
    ${myResultBanner}
    <div class="distribution-grid">`;

  for (const d of [...dist].sort((a, b) => b.count - a.count)) {
    const isAuthor = author && d.name === author.name;
    let guesserChips = '—';
    if (d.guessers && d.guessers.length) {
      guesserChips = d.guessers.map(g => {
        const cls = g.correct ? 'guesser-chip correct' : 'guesser-chip wrong';
        return `<span class="${cls}">${escHtml(g.name)}</span>`;
      }).join('');
    }
    html += `
      <div class="dist-card ${isAuthor ? 'is-author' : ''}">
        <div class="dist-name">${escHtml(d.name)}${isAuthor ? ' ✦' : ''}</div>
        <div class="dist-guessers">${guesserChips}</div>
        <div class="dist-count">${d.count}</div>
      </div>`;
  }

  html += `</div>`;

  if (isHost) {
    html += `<div class="action-row"><button class="btn-action" id="btn-next-answer">${isLastAnswer ? 'Show stats →' : 'Next answer →'}</button></div>`;
  } else {
    html += `<p style="color:var(--text-dim);font-size:0.88rem;">Waiting for the host to continue…</p>`;
  }

  html += `</div></div>`;

  main.innerHTML = html;
  $('btn-next-answer')?.addEventListener('click', () => send({ action: 'next_answer' }));
}

// ---- STATS ----
function renderStats(main, gs) {
  let html = `
    <h2 class="phase-title">That's a wrap! 🎉</h2>
    <p class="phase-subtitle">Here's how everyone did.</p>
    <div class="stats-grid">
  `;

  html += `<div class="stat-card"><h3>🎭 Most convincing</h3>`;
  if (gs.most_fooling?.length) {
    gs.most_fooling.forEach((r, i) => {
      html += `<div class="stat-row"><div class="stat-rank">${i+1}</div><div class="stat-name">${escHtml(r.name)}</div><div class="stat-val">${r.count} fooled</div></div>`;
    });
  } else { html += `<p style="color:var(--text-muted);font-size:0.85rem">No data yet</p>`; }
  html += `</div>`;

  html += `<div class="stat-card gold"><h3>🎯 Best detective</h3>`;
  if (gs.best_guessers?.length) {
    gs.best_guessers.forEach((r, i) => {
      html += `<div class="stat-row"><div class="stat-rank">${i+1}</div><div class="stat-name">${escHtml(r.name)}</div><div class="stat-val">${r.count} correct</div></div>`;
    });
  } else { html += `<p style="color:var(--text-muted);font-size:0.85rem">No data yet</p>`; }
  html += `</div>`;

  html += `<div class="stat-card" style="grid-column:1/-1"><h3>🤔 Hardest to identify</h3>`;
  if (gs.hardest_answers?.length) {
    gs.hardest_answers.forEach((a, i) => {
      html += `
        <div class="stat-row">
          <div class="stat-rank">${i+1}</div>
          <div style="flex:1">
            <div class="stat-name">${escHtml(a.author)}</div>
            <div class="hardest-text">"${escHtml(a.text)}"</div>
          </div>
          <div class="stat-val">${a.pct_correct}% guessed</div>
        </div>`;
    });
  } else { html += `<p style="color:var(--text-muted);font-size:0.85rem">No data yet</p>`; }
  html += `</div>`;

  html += `</div>`;
  html += `<div class="action-row"><button class="btn-secondary" onclick="window.location.href=window.location.pathname">← New session</button></div>`;

  main.innerHTML = html;
}

// ---- Utils ----
function escHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}