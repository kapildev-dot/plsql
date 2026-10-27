/* QUIZ ARENA - frontend logic
   Scoring, timer validation and answers are verified by the Flask server. */

const $ = (id) => document.getElementById(id);

const state = {
    category: "Mixed",
    timeLimit: 15,
    timerId: null,
    deadline: 0,
    locked: true,
    nextTimer: null,
};

/* server restarted / cookie expired => game gone, go back to start cleanly */
function isSessionLost(e) {
    return e && e.status === 409 && /no active game/i.test(e.message);
}
function backToStart(msg) {
    clearInterval(state.timerId);
    clearTimeout(state.nextTimer);
    state.locked = true;
    if (msg) toast(msg);
    showScreen("start");
    loadHomeBoard();
}

/* ---------------- helpers ---------------- */
async function api(path, options = {}) {
    let res;
    try {
        res = await fetch(path, {
            headers: { "Content-Type": "application/json" },
            ...options,
            body: options.body ? JSON.stringify(options.body) : undefined,
        });
    } catch (_) {
        const err = new Error("Server se connection nahi ho paya. Flask chal raha hai?");
        err.network = true;
        throw err;
    }
    let data = {};
    try { data = await res.json(); } catch (_) { /* HTML error page etc. */ }
    if (!res.ok) {
        const err = new Error(data.error || `Request failed (${res.status})`);
        err.data = data;
        err.status = res.status;
        throw err;
    }
    return data;
}

function showScreen(name) {
    ["start", "game", "result"].forEach((s) =>
        $("screen-" + s).classList.toggle("hidden", s !== name));
}

let toastTimer = null;
function toast(msg) {
    const t = $("toast");
    t.textContent = msg;
    t.classList.remove("hidden");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.add("hidden"), 3500);
}

function cell(text, className) {
    const td = document.createElement("td");
    td.textContent = text;
    if (className) td.className = className;
    return td;
}

function renderBoard(tbody, rows, highlightId, withResult) {
    tbody.innerHTML = "";
    if (!rows.length) {
        const tr = document.createElement("tr");
        const td = cell("No scores yet (or database not connected)", "empty");
        td.colSpan = withResult ? 5 : 4;
        tr.appendChild(td);
        tbody.appendChild(tr);
        return;
    }
    rows.forEach((r) => {
        const tr = document.createElement("tr");
        if (r.id === highlightId) tr.className = "me";
        tr.appendChild(cell(r.rank));
        tr.appendChild(cell(r.name));          // textContent => safe from XSS
        tr.appendChild(cell(r.category));
        tr.appendChild(cell(r.points));
        if (withResult) {
            tr.appendChild(cell(r.status, r.status === "PASS" ? "pass-text" : "fail-text"));
        }
        tbody.appendChild(tr);
    });
}

/* ---------------- start screen ---------------- */
async function loadCategories() {
    try {
        const data = await api("/api/categories");
        state.timeLimit = data.time_limit;
        $("rule-time").textContent = data.time_limit;

        const wrap = $("category-list");
        wrap.innerHTML = "";
        data.categories.forEach((c) => {
            const btn = document.createElement("button");
            btn.type = "button";
            btn.className = "chip" + (c.name === state.category ? " active" : "");
            btn.textContent = c.name;
            const small = document.createElement("small");
            small.textContent = `${c.count} Q`;
            btn.appendChild(small);
            btn.onclick = () => {
                state.category = c.name;
                wrap.querySelectorAll(".chip").forEach((x) => x.classList.remove("active"));
                btn.classList.add("active");
            };
            wrap.appendChild(btn);
        });
    } catch (e) {
        toast("Server connection failed. Flask terminal check karo.");
    }
}

async function loadHomeBoard() {
    const data = await api("/api/leaderboard").catch(() => ({ leaderboard: [] }));
    renderBoard($("home-board"), (data.leaderboard || []).slice(0, 5), null, false);
}

async function startGame() {
    const name = $("player-name").value.trim();
    if (name.length < 2) {
        toast("Pehle apna naam enter karo (kam se kam 2 letters).");
        $("player-name").focus();
        return;
    }
    try { localStorage.setItem("quizName", name); } catch (_) {}
    const startBtn = $("btn-start");
    startBtn.disabled = true;                   // stop double clicks
    try {
        await api("/api/start", { method: "POST", body: { name, category: state.category } });
        $("hud-name").textContent = name;
        $("hud-points").textContent = "0";
        $("hud-streak").textContent = "0 \u{1F525}";
        showScreen("game");
        await loadQuestion();
    } catch (e) {
        toast(e.message);
    } finally {
        startBtn.disabled = false;
    }
}

/* ---------------- game screen ---------------- */
async function loadQuestion(attempt = 0) {
    try {
        const q = await api("/api/question");
        if (q.finished) return finishGame();
        renderQuestion(q);
    } catch (e) {
        if (isSessionLost(e)) return backToStart("Game session khatam ho gayi. Dobara start karo.");
        if (attempt < 3) {                      // temporary glitch: retry, don't freeze
            toast(e.message);
            state.nextTimer = setTimeout(() => loadQuestion(attempt + 1), 1500);
        } else {
            backToStart("Question load nahi hua. Dobara start karo.");
        }
    }
}

function renderQuestion(q) {
    $("hud-name").textContent = q.name;
    $("q-number").textContent = `Question ${q.number} / ${q.total}`;
    $("progress-fill").style.width = `${((q.number - 1) / q.total) * 100}%`;
    $("q-category").textContent = q.category;
    const diff = $("q-diff");
    diff.textContent = q.difficulty;
    diff.className = "tag " + q.difficulty;
    $("q-title").textContent = q.question;
    $("hud-points").textContent = q.points;
    $("hud-streak").textContent = `${q.streak} \u{1F525}`;
    $("feedback").textContent = "";
    $("feedback").className = "feedback";

    const wrap = $("options");
    wrap.innerHTML = "";
    q.options.forEach((text, i) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "opt";
        const key = document.createElement("span");
        key.className = "key";
        key.textContent = "ABCD"[i];
        const label = document.createElement("span");
        label.textContent = text;
        btn.append(key, label);
        if (q.hidden.includes(i)) { btn.classList.add("eliminated"); btn.disabled = true; }
        btn.onclick = () => answer(i);
        wrap.appendChild(btn);
    });

    $("btn-fifty").disabled = !q.fifty_available;
    state.locked = false;
    startTimer(q.time_left, q.time_limit);
}

function startTimer(timeLeft, limit) {
    clearInterval(state.timerId);
    state.deadline = performance.now() + timeLeft * 1000;
    const tick = () => {
        const left = Math.max(0, (state.deadline - performance.now()) / 1000);
        $("timer-val").textContent = Math.ceil(left);
        $("timer-fill").style.width = `${(left / limit) * 100}%`;
        $("timer-fill").classList.toggle("danger", left <= 5);
        if (left <= 0) {
            clearInterval(state.timerId);
            answer(null);                       // time over
        }
    };
    state.timerId = setInterval(tick, 100);
    tick();
}

async function answer(index) {
    if (state.locked) return;
    state.locked = true;
    clearInterval(state.timerId);

    const buttons = [...document.querySelectorAll(".opt")];
    buttons.forEach((b) => (b.disabled = true));
    $("btn-fifty").disabled = true;

    try {
        const r = await api("/api/answer", { method: "POST", body: { selected: index } });

        if (index !== null && !r.correct && buttons[index]) buttons[index].classList.add("wrong");
        if (buttons[r.correct_index]) buttons[r.correct_index].classList.add("correct");

        $("hud-points").textContent = r.points;
        $("hud-streak").textContent = `${r.streak} \u{1F525}`;

        const fb = $("feedback");
        if (r.correct) {
            const b = r.breakdown;
            fb.textContent = `+${r.earned} pts  (base ${b.base} + speed ${b.time} + streak ${b.streak})`;
            fb.className = "feedback good";
        } else {
            fb.textContent = r.timed_out ? "\u23F0 Time's up!" : "\u274C Wrong answer";
            fb.className = "feedback bad";
        }

        state.nextTimer = setTimeout(() => loadQuestion(), r.correct ? 1200 : 1800);
    } catch (e) {
        if (isSessionLost(e)) return backToStart("Game session khatam ho gayi. Dobara start karo.");
        toast(e.message);
        state.nextTimer = setTimeout(() => loadQuestion(), 1500);   // re-sync with server, never stay stuck
    }
}

async function useFifty() {
    if (state.locked || $("btn-fifty").disabled) return;
    $("btn-fifty").disabled = true;
    try {
        const r = await api("/api/fifty", { method: "POST" });
        const buttons = document.querySelectorAll(".opt");
        r.hidden.forEach((i) => {
            buttons[i].classList.add("eliminated");
            buttons[i].disabled = true;
        });
    } catch (e) {
        if (isSessionLost(e)) return backToStart("Game session khatam ho gayi. Dobara start karo.");
        if (e.status !== 409) $("btn-fifty").disabled = false;   // allow retry on network error only
        toast(e.message);
    }
}

/* ---------------- result screen ---------------- */
async function finishGame() {
    clearInterval(state.timerId);
    $("progress-fill").style.width = "100%";
    showScreen("result");

    $("result-title").textContent = "Saving to Oracle...";
    $("result-title").className = "result-title";
    $("result-sub").textContent = "";
    ["db-status", "db-grade", "db-msg", "db-rank"].forEach((id) => {
        $(id).textContent = "-";
        $(id).className = "";
    });
    $("db-error").classList.add("hidden");
    $("btn-retry").classList.add("hidden");

    try {
        const r = await api("/api/finish", { method: "POST" });
        renderResult(r.summary, r.db, r.leaderboard, null);
    } catch (e) {
        if (e.data && e.data.summary) {
            renderResult(e.data.summary, null, [], e.message);
        } else if (isSessionLost(e)) {
            backToStart("Game session khatam ho gayi. Dobara start karo.");
        } else {                                 // network down etc: show retry instead of freezing
            $("result-title").textContent = "Result save nahi hua";
            $("result-title").className = "result-title fail";
            $("db-error").textContent = e.message;
            $("db-error").classList.remove("hidden");
            $("btn-retry").classList.remove("hidden");
        }
    }
}

function renderResult(s, db, board, errorMsg) {
    const passed = db ? db.status === "PASS" : s.accuracy >= 60;   // PL/SQL result wins
    const title = $("result-title");
    title.textContent = passed ? "\u{1F3C6} You Passed!" : "\u{1F4AA} Keep Practicing!";
    title.className = "result-title " + (passed ? "pass" : "fail");
    $("result-sub").textContent = `${s.name} \u2022 ${s.category} mode`;

    $("st-correct").textContent = `${s.correct}/${s.total}`;
    $("st-points").textContent = s.points;
    $("st-acc").textContent = `${s.accuracy}%`;
    $("st-streak").textContent = s.best_streak;
    $("st-time").textContent = `${s.time_spent}s`;

    if (db) {
        const st = $("db-status");
        st.textContent = db.status;
        st.className = db.status === "PASS" ? "pass-text" : "fail-text";
        $("db-grade").textContent = db.grade;
        $("db-msg").textContent = db.message;
        $("db-rank").textContent = db.rank ? `#${db.rank}` : "-";
    }
    if (errorMsg) {
        const el = $("db-error");
        el.textContent = errorMsg;
        el.classList.remove("hidden");
        $("btn-retry").classList.remove("hidden");
    }
    renderBoard($("result-board"), board, db ? db.id : null, true);
}

function playAgain() {
    clearTimeout(state.nextTimer);
    showScreen("start");
    loadHomeBoard();
}

/* ---------------- resume after page refresh ---------------- */
async function tryResume() {
    try {
        const q = await api("/api/question");
        if (q.finished) {
            finishGame();
        } else {
            showScreen("game");
            renderQuestion(q);
        }
    } catch (_) { /* no active game: stay on the start screen */ }
}

/* ---------------- events ---------------- */
$("btn-start").onclick = startGame;
$("btn-fifty").onclick = useFifty;
$("btn-again").onclick = playAgain;
$("btn-retry").onclick = finishGame;
$("player-name").addEventListener("keydown", (e) => { if (e.key === "Enter") startGame(); });

document.addEventListener("keydown", (e) => {
    if ($("screen-game").classList.contains("hidden") || state.locked) return;
    const map = { "1": 0, "2": 1, "3": 2, "4": 3, a: 0, b: 1, c: 2, d: 3 };
    const idx = map[e.key.toLowerCase()];
    const btn = idx !== undefined ? document.querySelectorAll(".opt")[idx] : null;
    if (btn && !btn.disabled) answer(idx);
});

try { $("player-name").value = localStorage.getItem("quizName") || ""; } catch (_) {}
loadCategories();
loadHomeBoard();
tryResume();