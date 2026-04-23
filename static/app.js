let ws;
let myName = "";
let myRoom = "";
let countdownInterval;
let pingInterval;
let lastKnownState = null;

// Sound Engine
function playSound(id) {
    let audio = document.getElementById(id);
    audio.currentTime = 0;
    audio.play().catch(e => console.log("Audio blocked by browser."));
}

// --- 1. The Submission Sanitizer ---
function sanitizeInput(inputId, isPrompt = false) {
    let el = document.getElementById(inputId);
    let val = el.value;
    
    // Prompts skip the regex entirely. (XSS is blocked natively via innerText)
    if (isPrompt) {
        if (!val.trim()) {
            el.classList.add("error");
            showToast("Prompt cannot be empty.");
            return null;
        }
        el.classList.remove("error");
        return val.trim();
    }

    // Game Words (Traps/Decoys/Guesses) must be strictly alphanumeric for the math engine.
    if (!/^[a-zA-Z0-9\s]*$/.test(val)) {
        el.classList.add("error");
        showToast("Invalid characters! Letters and numbers only.");
        return null;
    }
    el.classList.remove("error");
    return val.trim();
}

// --- 2. The Real-Time Typing Filter ---
function enforceInputRule(el, ruleType) {
    let pattern = (ruleType === "strict") ? /[^a-zA-Z0-9]/g : /[^a-zA-Z0-9\s]/g;
    const cleaned = el.value.replace(pattern, "");
    if (cleaned !== el.value) {
        el.value = cleaned;
        el.classList.add("error");
        showToast("Invalid character removed.");
        setTimeout(() => el.classList.remove("error"), 800);
    }
}

// Attach filters ONLY to game words. The Prompt field is left completely unrestricted.
["dealerTrap", "dealerDecoy", "midRoundDecoy", "safeWord", "bountyWord", "playerName"].forEach(id => {
    document.getElementById(id).addEventListener("input", (e) => enforceInputRule(e.target, "words"));
});
document.getElementById("roomCode").addEventListener("input", (e) => {
    enforceInputRule(e.target, "strict");
    e.target.value = e.target.value.toUpperCase();
});

// --- 3. Keyboard "Enter" listeners ---
document.getElementById("dealerPrompt").addEventListener("keypress", function(e) { if (e.key === "Enter") sendTrap(); });
document.getElementById("dealerTrap").addEventListener("keypress", function(e) { if (e.key === "Enter") sendTrap(); });
document.getElementById("dealerDecoy").addEventListener("keypress", function(e) { if (e.key === "Enter") sendTrap(); });
document.getElementById("safeWord").addEventListener("keypress", function(e) { if (e.key === "Enter") sendSafeWord(); });
document.getElementById("bountyWord").addEventListener("keypress", function(e) { if (e.key === "Enter") sendBounty(); });
document.getElementById("midRoundDecoy").addEventListener("keypress", function(e) { if (e.key === "Enter") updateDecoy(); });

function switchPanel(panelId) {
    document.querySelectorAll('.panel').forEach(p => p.classList.remove('active-panel'));
    document.getElementById(panelId).classList.add('active-panel');
}

// Dismiss the initial instructions screen
function closeInstructions() {
    document.getElementById("instructions").style.display = "none";
    document.getElementById("playerName").focus();
}

function showToast(msg, type = "error") {
    const toast = document.getElementById("globalToast");
    if (!toast) return;
    
    toast.innerText = msg;
    // Set the class to show it, and assign the color based on the type
    toast.className = `show ${type}`;
    
    // Auto-hide after 3 seconds
    setTimeout(() => {
        toast.classList.remove("show");
    }, 3000);
}

function squashWord(word) {
    if (!word) return "";
    return word.toLowerCase().replace(/[^a-z0-9]/g, "");
}

function levenshtein(s1, s2) {
    if (s1.length < s2.length) return levenshtein(s2, s1);
    if (s2.length === 0) return s1.length;
    
    let previousRow = Array.from({length: s2.length + 1}, (_, i) => i);
    for (let i = 0; i < s1.length; i++) {
        let currentRow = [i + 1];
        for (let j = 0; j < s2.length; j++) {
            let insertions = previousRow[j + 1] + 1;
            let deletions = currentRow[j] + 1;
            let substitutions = previousRow[j] + (s1[i] !== s2[j] ? 1 : 0);
            currentRow.push(Math.min(insertions, deletions, substitutions));
        }
        previousRow = currentRow;
    }
    return previousRow[previousRow.length - 1];
}

function isMatch(word1, word2) {
    let w1 = squashWord(word1);
    let w2 = squashWord(word2);
    if (!w1 || !w2) return false;
    if (w1 === w2) return true;
    
    let dist = levenshtein(w1, w2);
    let length = Math.min(w1.length, w2.length);
    if (length <= 4 && dist === 0) return true;
    if (length >= 5 && length <= 8 && dist <= 1) return true;
    if (length >= 9 && dist <= 2) return true;
    return false;
}

function connect() {
    myRoom = document.getElementById("roomCode").value.toUpperCase();
    myName = document.getElementById("playerName").value;
    if(!myName || !myRoom) return alert("Enter a name and room code!");
    establishWebSocket();
}

function reconnectSync() {
    document.getElementById("disconnectOverlay").style.display = "none";
    establishWebSocket();
}

function establishWebSocket() {
    const wsProtocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(`${wsProtocol}//${window.location.host}/ws/${myRoom}/${myName}`);
    
    ws.onopen = () => { 
        document.getElementById("connectPanel").style.display = "none"; 
        document.getElementById("disconnectOverlay").style.display = "none";
        
        if (pingInterval) clearInterval(pingInterval);
        pingInterval = setInterval(() => {
            if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({ action: "ping_probe", client_ts: Date.now() }));
            }
        }, 3000);
    };
    
    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        
        if (data.action === "rejected") {
            showToast(data.message, "error"); // Explicitly trigger red error toast
            document.getElementById("safeWord").value = ""; 
            if(data.message.includes("banned") || data.message.includes("kicked")) {
                ws.close();
                alert(data.message);
                location.reload();
            }
        } else if (data.action === "success") {
            // NEW: Actually tell the user when their action succeeded!
            showToast(data.message, "success"); 
        } else if (data.action === "fill_prompt") {
            document.getElementById("dealerPrompt").value = data.prompt;
            document.getElementById("dealerTrap").focus();
        } else if (data.action === "pong_probe" && data.client_ts) {
            let rtt = Date.now() - data.client_ts;
            ws.send(JSON.stringify({ action: "latency_update", latency_ms: Math.max(0, rtt / 2) }));
        } else if (data.action === "state_update") {
            renderState(data);
        }
    };

    ws.onclose = () => {
        if (pingInterval) clearInterval(pingInterval);
        if(document.getElementById("connectPanel").style.display === "none") {
            document.getElementById("disconnectOverlay").style.display = "flex";
        }
    };
}

function renderState(state) {
    lastKnownState = state;
    const isDealer = state.dealer === myName;
    const isHost = state.host === myName;
    const me = state.players.find(p => p.name === myName) || { role: "active", my_vetoes: [] };
    const activePlayers = state.players.filter(p => p.role === "active");
    const playerCount = activePlayers.length;

    document.getElementById("roomHeader").style.display = "block";
    document.getElementById("displayRoomCode").innerText = state.room_code;
    document.getElementById("hostBadge").style.display = isHost ? "inline" : "none";

    clearInterval(countdownInterval);
    let timeLeft = state.time_left;
    
    if ((state.phase === "trap_phase" || state.phase === "response_phase" || state.phase === "tribunal" || state.phase === "reveal") && timeLeft > 0) {
        document.getElementById("timerContainer").style.display = "block";
        
        let timerText = "";
        if (state.phase === "trap_phase") timerText = "Dealer's Time: ";
        if (state.phase === "response_phase") timerText = "Time to Respond! ";
        if (state.phase === "tribunal") timerText = "The Tribunal: ";
        if (state.phase === "reveal") timerText = "Next Round In: ";
        
        document.getElementById("timerLabel").innerText = timerText;
        document.getElementById("timeRemaining").innerText = timeLeft;
        document.getElementById("timeRemaining").style.color = timeLeft <= 5 ? "red" : "#2b6cb0";

        countdownInterval = setInterval(() => {
            timeLeft--;
            if (timeLeft < 0) timeLeft = 0;
            document.getElementById("timeRemaining").innerText = timeLeft;
            document.getElementById("timeRemaining").style.color = timeLeft <= 5 ? "red" : "#2b6cb0";
            if (timeLeft > 0 && timeLeft <= 5 && state.phase !== "reveal") playSound("sfxTick");
            if (timeLeft === 0) clearInterval(countdownInterval);
        }, 1000);
    } else {
        document.getElementById("timerContainer").style.display = "none";
    }

    if (state.phase === "lobby") {
        switchPanel("lobbyPanel");
        
        document.getElementById("lobbyPlayers").innerHTML = state.players.map(p => {
            let styleClass = p.streak >= 3 ? "gold-streak" : "";
            let tag = p.role === "audience" ? `<span class="audience-tag">(VIP Audience)</span>` : "";
            let acc = p.vote_accuracy_total > 0 ? ` • Tribunal IQ: ${Math.round((p.vote_accuracy_hits / p.vote_accuracy_total) * 100)}%` : "";
            let kickBtn = (isHost && p.name !== myName) ? `<button class="btn-kick" onclick="kickPlayer('${p.name}')">Kick</button>` : "";
            return `<div class="${styleClass}">${p.name} ${tag}${acc} ${kickBtn}</div>`;
        }).join('');
        
        const startBtn = document.getElementById("startGameBtn");
        const rulesetSelect = document.getElementById("rulesetSelect");
        const rulesetDescription = document.getElementById("rulesetDescription");
        const dealerTimeInput = document.getElementById("dealerTimeInput");
        const responseTimeInput = document.getElementById("responseTimeInput");
        const tribunalTimeInput = document.getElementById("tribunalTimeInput");
        const revealTimeInput = document.getElementById("revealTimeInput");
        const saveTimersBtn = document.getElementById("saveTimersBtn");
        
        rulesetSelect.value = state.ruleset || "classic";
        dealerTimeInput.value = state.dealer_time || 30;
        responseTimeInput.value = state.response_time || 10;
        tribunalTimeInput.value = state.tribunal_time || 10;
        revealTimeInput.value = state.reveal_time || 10;
        rulesetDescription.innerText = (state.ruleset === "competitive")
            ? "Competitive: one vote per player, dynamic elimination threshold, max 2 vetoed words."
            : "Classic: multi-vote tribunal with majority elimination.";
            
        if (playerCount < 3) {
            document.getElementById("lobbyWaitText").innerText = `Waiting for active players... (${playerCount}/3)`;
        } else {
            document.getElementById("lobbyWaitText").innerText = "Ready to start!";
        }
        
        if (isHost) {
            rulesetSelect.disabled = false;
            dealerTimeInput.disabled = false;
            responseTimeInput.disabled = false;
            tribunalTimeInput.disabled = false;
            revealTimeInput.disabled = false;
            saveTimersBtn.disabled = false;
            startBtn.style.display = "inline-block";
            startBtn.disabled = playerCount < 3;
            startBtn.innerText = playerCount < 3 ? "Need 3 Players" : "Start Game";
        } else {
            rulesetSelect.disabled = true;
            dealerTimeInput.disabled = true;
            responseTimeInput.disabled = true;
            tribunalTimeInput.disabled = true;
            revealTimeInput.disabled = true;
            saveTimersBtn.disabled = true;
            startBtn.style.display = "none";
        }
    
    } else if (state.phase === "trap_phase") {
        if (isDealer) {
            switchPanel("dealerTrapPanel");
            document.getElementById("dealerPrompt").value = "";
            document.getElementById("dealerTrap").value = "";
            document.getElementById("dealerDecoy").value = "";
            document.getElementById("dealerPrompt").focus();
        } else {
            switchPanel("responderWaitPanel");
            document.getElementById("currentDealerName").innerText = state.dealer;
        }
    
    } else if (state.phase === "response_phase") {
        switchPanel("playPanel");
        document.getElementById("displayPrompt").innerText = `Prompt: "${state.prompt}"`;
        
        if (isDealer) {
            document.getElementById("dealerWaitZone").style.display = "block";
            document.getElementById("responderPlayZone").style.display = "none";
            document.getElementById("midRoundDecoy").value = ""; 
        } else {
            document.getElementById("dealerWaitZone").style.display = "none";
            document.getElementById("responderPlayZone").style.display = "block";
            
            if (me.role === "audience") {
                document.getElementById("audienceNotice").style.display = "block";
                document.getElementById("safeWordZone").style.display = "none";
                if (!me.bounty_locked) {
                    document.getElementById("bountyZone").style.display = "block";
                    document.getElementById("bountyWord").value = "";
                } else {
                    document.getElementById("bountyZone").innerHTML = "<p>Bounty locked. Waiting for round to end...</p>";
                }
            } else {
                document.getElementById("audienceNotice").style.display = "none";
                if (!me.locked) {
                    document.getElementById("safeWordZone").style.display = "block";
                    document.getElementById("bountyZone").style.display = "none";
                    document.getElementById("safeWord").focus();
                } else if (!me.bounty_locked) {
                    document.getElementById("safeWordZone").style.display = "none";
                    document.getElementById("bountyZone").style.display = "block";
                    document.getElementById("bountyWord").value = "";
                    document.getElementById("bountyWord").focus();
                } else {
                    document.getElementById("bountyZone").innerHTML = "<p>Waiting for others to finish...</p>";
                }
            }
        }
    } else if (state.phase === "tribunal") {
        switchPanel("tribunalPanel");
        
        document.getElementById("tribunalPromptDisplay").innerText = `"${state.prompt}"`;
        
        let html = "";
        for (const word of state.words_to_vote) {
            let isSelected = me.my_vetoes.includes(word);
            let color = isSelected ? "background: #e53e3e; color: white;" : "background: #edf2f7; color: black;";
            html += `<button style="${color} border: 1px solid #cbd5e0; border-radius: 8px; padding: 15px; font-size: 1.1em;" onclick="toggleVeto('${word}')">${word}</button>`;
        }
        
        if (state.words_to_vote.length === 0) {
            html = "<p>No words to review!</p>";
        }
        document.getElementById("tribunalWords").innerHTML = html;

    } else if (state.phase === "reveal") {
        switchPanel("revealPanel");
        document.getElementById("revealTrapWord").innerText = state.trap_word;
        document.getElementById("revealDecoyWord").innerText = state.decoy_word || "None";
        
        let revealHTML = "";
        let dealerPoints = 0;
        let trappedCount = 0;
        let bountyFarmedCount = 0;

        for (const [pName, pWord] of Object.entries(state.revealed_words)) {
            let pData = state.players.find(p => p.name === pName);
            let pointsEarned = 0;
            let logs = [];
            let voteData = state.vote_accuracy_round && state.vote_accuracy_round[pName];

            let isMindReader = pData && pData.mind_reader;
            let isTrapped = isMatch(pWord, state.trap_word);
            let isVetoed = state.vetoed_words.some(v => isMatch(pWord, v));
            let hasZeroPoints = false;

            if (pData && pData.timed_out) {
                logs.push("<span style='color:#e53e3e'>🌽 AFK (0 pts)</span>");
                hasZeroPoints = true;
            } else if (isTrapped) {
                logs.push("<span style='color:#e53e3e'>💥 TRAPPED (0 pts)</span>");
                hasZeroPoints = true;
                trappedCount++;
                dealerPoints++; 
                if(pName === myName) playSound("sfxBuzzer");
            } else if (isMindReader) {
                pointsEarned += 1;
                logs.push("<span style='color:#805ad5'>🧠 MIND READER (+1 pt)</span>");
                if(pName === myName) playSound("sfxDing");
            } else if (isVetoed) {
                logs.push("<span style='color:#e53e3e'>🗑️ VETOED (0 pts)</span>");
                hasZeroPoints = true;
                if(pName === myName) playSound("sfxBuzzer");
            } else {
                pointsEarned += 1;
                logs.push("<span style='color:#38a169'>✅ SURVIVED (+1 pt)</span>");
                if(pName === myName) playSound("sfxDing");
            }

            let bounty = state.revealed_bounties[pName];
            if (bounty && isMatch(bounty, state.trap_word) && !hasZeroPoints) {
                pointsEarned += 1;
                logs.push("<span style='color:#38a169'>🎯 BOUNTY (+1 pt)</span>");
                if (bountyFarmedCount < 2) {
                    bountyFarmedCount++;
                    dealerPoints++;
                }
            }

            if (pData && pData.caught) {
                pointsEarned -= 1;
                logs.push("<span style='color:#dd6b20'>🍯 HONEYPOT (-1 pt)</span>");
            }
            
            if (voteData && voteData.total > 0) {
                logs.push(`<span style='color:#4a5568'>⚖️ ACCURACY (${voteData.hits}/${voteData.total})</span>`);
            }

            let rowStyle = isMindReader ? "background: #faf5ff; border-left: 4px solid #805ad5;" : "background: #fff;";
            revealHTML += `
                <div style="margin-bottom: 10px; padding: 10px; border-radius: 5px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); ${rowStyle}">
                    <div style="display: flex; justify-content: space-between;">
                        <strong>${pName}: <span style="color:#4a5568">"${pWord}"</span></strong>
                        <strong>Net: ${pointsEarned > 0 ? '+' : ''}${pointsEarned}</strong>
                    </div>
                    <div style="font-size: 0.85em; margin-top: 5px;">${logs.join(' | ')}</div>
                </div>`;
        }

        let dealerBanner = `
            <div style="margin-bottom: 20px; padding: 15px; border-radius: 6px; background: #ebf8ff; border: 2px solid #3182ce; text-align: left;">
                <div style="display: flex; justify-content: space-between; align-items: center;">
                    <h3 style="margin: 0; color: #2b6cb0;">🃏 Dealer: ${state.dealer}</h3>
                    <strong style="font-size: 1.2em; color: ${dealerPoints > 0 ? '#38a169' : '#718096'};">Net: +${dealerPoints}</strong>
                </div>
                <div style="font-size: 0.9em; margin-top: 5px; color: #4a5568;">
                    ${trappedCount} Trapped | ${bountyFarmedCount} Bounties Farmed (Max 2)
                </div>
            </div>`;

        document.getElementById("revealList").innerHTML = dealerBanner + revealHTML;
        
        state.players.sort((a,b) => b.score - a.score);
        document.getElementById("finalScoreboard").innerHTML = "<h3>Scoreboard</h3>" + 
            state.players.map(p => {
                let styleClass = p.streak >= 3 ? "gold-streak" : "";
                let acc = p.vote_accuracy_total > 0 ? ` • ${Math.round((p.vote_accuracy_hits / p.vote_accuracy_total) * 100)}% accurate` : "";
                return `<div class="${styleClass}">${p.name}: ${p.score} points ${p.streak >= 3 ? '🔥' : ''}${acc}</div>`;
            }).join('');

    } else if (state.phase === "game_over") {
        switchPanel("gameOverPanel");
        
        let sortedPlayers = [...state.players].sort((a,b) => b.score - a.score);
        let winner = sortedPlayers[0];
        document.getElementById("winnerAnnouncement").innerText = `🏆 ${winner.name} Wins! 🏆`;
        playSound("sfxDing");
        
        document.getElementById("finalStandings").innerHTML = sortedPlayers.map(p => `<div>${p.name}: <strong>${p.score}</strong> points</div>`).join('');
        
        if (isHost) {
            document.getElementById("gameOverControls").style.display = "block";
            document.getElementById("gameOverWaitText").style.display = "none";
        } else {
            document.getElementById("gameOverControls").style.display = "none";
            document.getElementById("gameOverWaitText").style.display = "block";
        }
    }
}

// --- 4. The Payload Sender ---
function sendTrap() { 
    let p = sanitizeInput("dealerPrompt", true); // Bypasses regex
    let w = sanitizeInput("dealerTrap");
    let d = sanitizeInput("dealerDecoy"); 
    
    if(p !== null && w !== null && p !== "" && w !== "") {
        ws.send(JSON.stringify({ action: "lock_trap", prompt: p, word: w, decoy: d || "" })); 
    }
}

function updateDecoy() {
    let d = sanitizeInput("midRoundDecoy");
    if(d) {
        ws.send(JSON.stringify({ action: "update_decoy", word: d }));
        document.getElementById("midRoundDecoy").value = "";
        showToast("Decoy updated!");
    }
}

function sendSafeWord() { 
    let w = sanitizeInput("safeWord");
    if(w) ws.send(JSON.stringify({ action: "lock_word", word: w })); 
}

function sendBounty() { 
    let w = sanitizeInput("bountyWord");
    if(w) ws.send(JSON.stringify({ action: "bounty_guess", word: w })); 
}

function toggleVeto(word) { ws.send(JSON.stringify({ action: "toggle_veto", word: word })); }
function setRuleset() {
    let ruleset = document.getElementById("rulesetSelect").value;
    ws.send(JSON.stringify({ action: "set_ruleset", ruleset: ruleset }));
}
function saveTimers() {
    let dealerTime = parseInt(document.getElementById("dealerTimeInput").value, 10);
    let responseTime = parseInt(document.getElementById("responseTimeInput").value, 10);
    let tribunalTime = parseInt(document.getElementById("tribunalTimeInput").value, 10);
    let revealTime = parseInt(document.getElementById("revealTimeInput").value, 10);
    
    if ([dealerTime, responseTime, tribunalTime, revealTime].some(v => Number.isNaN(v) || v < 5 || v > 120)) {
        showToast("Timer values must be between 5 and 120.");
        return;
    }
    
    ws.send(JSON.stringify({
        action: "set_timers",
        dealer_time: dealerTime,
        response_time: responseTime,
        tribunal_time: tribunalTime,
        reveal_time: revealTime
    }));
}
function requestRandomPrompt() { ws.send(JSON.stringify({ action: "random_prompt" })); }
function kickPlayer(name) { ws.send(JSON.stringify({ action: "kick_player", word: name })); }
function playAgain() { ws.send(JSON.stringify({ action: "play_again" })); }
function returnLobby() { ws.send(JSON.stringify({ action: "return_lobby" })); }
function startGame() { ws.send(JSON.stringify({ action: "start_game" })); }